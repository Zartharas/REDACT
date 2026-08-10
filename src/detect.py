"""
Detection engine: three independent layers whose outputs are normalized to a
common type vocabulary (EMAIL, SSN, CREDIT_CARD, PERSON, IP, MRN) so they can
be compared against ground truth and against each other.
"""
import re
import math
from collections import Counter
from functools import lru_cache

# --------------------------------------------------------------------------
# Layer 1: regex
# --------------------------------------------------------------------------

REGEX_PATTERNS = {
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b"),
    "CREDIT_CARD": re.compile(r"\b\d{12,19}\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "MRN": re.compile(r"\bMRN-\d{7}\b"),
}

# Bug found via real-data validation, 2026-08-10 (task: extend real-data
# validation to windows_event and cloudtrail): running the naive/field-
# gated ensemble against 2,000 real flaws.cloud CloudTrail events measured
# P=0.310 (FP=4627) -- a huge, systematic precision collapse not seen on
# any of this project's other real-data conditions (OpenSSH FP=49, Linux
# FP=122). Root-caused via a dedicated diagnostic
# (validation/real_data/diagnose_cloudtrail_false_positives.py, no spaCy
# needed since this is a pure regex-layer bug): AWS account IDs are
# always exactly 12 digits -- the low end of CREDIT_CARD's \d{12,19}
# range -- and appear TWICE in a typical CloudTrail event (once as
# userIdentity.accountId, again embedded in the arn field, e.g.
# "arn:aws:iam::811596193553:user/Level6" -- the colons on both sides of
# the account ID satisfy \b same as whitespace would). Confirmed live:
# 3,775 of the 4,627 false positives (81.6%) were exact matches of a real
# accountId value appearing on the same line. This is a genuine
# collision between a real cloud-native identifier format and this
# project's CREDIT_CARD regex, not a diffuse NER weakness -- naive and
# field-gated measured almost identically (FP 4627 vs 4562), which
# already ruled out anything field-gating's excision logic could affect
# before this was even root-caused.
#
# NOT FIXED by narrowing the regex to \d{13,19}: confirmed via
# Faker.credit_card_number() (2,000 samples, seeded) that this project's
# OWN synthetic corpus generator produces real, legitimate 12-digit
# CREDIT_CARD values for some card network formats -- shrinking the
# range would silently regress this project's own already-measured
# synthetic-corpus CREDIT_CARD recall to fix a real-data problem, exactly
# the kind of "fix one number, quietly break another" mistake this
# project's own review discipline exists to catch. Fixed instead with a
# narrow, context-aware exclusion (same shape as scan_entropy's UUID
# exclusion below): a 12-digit match is excluded from CREDIT_CARD ONLY
# when it's immediately preceded by an AWS ARN's account-ID position
# (arn:<partition>:<service>:<region>:<12 digits>) or a JSON
# accountId/recipientAccountId key -- both are structurally specific
# enough that a real credit card number could not coincidentally match
# either shape. 13-19 digit matches are never affected (AWS account IDs
# are never that length), and a bare 12-digit number NOT in one of these
# two specific contexts is still reported as CREDIT_CARD exactly as
# before -- this does not touch the general case, only the exact
# collision shape found live.
_AWS_ARN_ACCOUNT_ID_PREFIX_RE = re.compile(r"arn:aws[a-z0-9-]*:[^:]*:[^:]*:$")
_AWS_ACCOUNT_ID_KEY_RE = re.compile(r'"(?:recipientA|a)ccountId"\s*:\s*"$')


def _is_aws_account_id_context(text: str, start: int) -> bool:
    prefix = text[:start]
    return bool(_AWS_ARN_ACCOUNT_ID_PREFIX_RE.search(prefix)
                or _AWS_ACCOUNT_ID_KEY_RE.search(prefix))


def scan_regex(text: str) -> list[dict]:
    hits = []
    for label, pattern in REGEX_PATTERNS.items():
        for m in pattern.finditer(text):
            if (label == "CREDIT_CARD" and m.end() - m.start() == 12
                    and _is_aws_account_id_context(text, m.start())):
                continue
            hits.append({"type": label, "start": m.start(), "end": m.end(), "method": "regex"})
    return hits


# --------------------------------------------------------------------------
# Layer 2: Presidio NER, with a custom MRN recognizer added
# --------------------------------------------------------------------------

_PRESIDIO_TO_CANONICAL = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL",
    "IP_ADDRESS": "IP",
    "US_SSN": "SSN",
    "CREDIT_CARD": "CREDIT_CARD",
    "MEDICAL_RECORD_NUMBER": "MRN",
}


@lru_cache(maxsize=1)
def _get_analyzer():
    # Imported lazily: loading the spaCy model is the expensive part of
    # startup and callers that only need regex/entropy shouldn't pay for it.
    from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern

    mrn_pattern = Pattern(name="mrn_pattern", regex=r"\bMRN-\d{7}\b", score=0.9)
    mrn_recognizer = PatternRecognizer(
        supported_entity="MEDICAL_RECORD_NUMBER", patterns=[mrn_pattern]
    )
    analyzer = AnalyzerEngine()
    analyzer.registry.add_recognizer(mrn_recognizer)
    return analyzer


def scan_ner(text: str, min_score: float = 0.5) -> list[dict]:
    analyzer = _get_analyzer()
    results = analyzer.analyze(
        text=text,
        language="en",
        entities=list(_PRESIDIO_TO_CANONICAL.keys()),
    )
    hits = []
    for r in results:
        if r.score < min_score:
            continue
        canonical = _PRESIDIO_TO_CANONICAL.get(r.entity_type)
        if canonical is None:
            continue
        hits.append({
            "type": canonical, "start": r.start, "end": r.end,
            "method": "ner", "score": round(r.score, 3),
        })
    return hits


# --------------------------------------------------------------------------
# Layer 3: entropy fallback for unstructured high-entropy tokens
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_.-]{8,}")

# UUID hyphens sit at fixed positions, and the version/variant nibbles are
# constrained (version is one hex digit 1-5, variant is one of 8/9/a/b), so
# a UUID is structurally lower entropy per character than a truly random
# token of the same length even though it "looks" random at a glance.
# validation/entropy_fair_test/ was built specifically to include this hard
# negative case, and inspecting that test's remaining false positives (see
# README.md in that directory) showed they were concentrated almost
# entirely in UUIDs embedded in URLs and params. Matching the fixed UUID
# shape and excluding it, rather than relying on entropy or length alone
# to tell them apart, fixes the false-positive source at its structural
# root.
#
# Limitation worth flagging: a small number of real-world services issue
# UUID-shaped API keys or tokens (validation/entropy_fair_test/README.md's
# "What this test doesn't establish" section calls out this exact
# collision). Excluding the UUID shape trades a little recall on that
# specific format for a large precision gain on the much more common case
# of UUIDs used as request or resource identifiers rather than secrets --
# a deliberate, documented tradeoff rather than an oversight.
#
# The regex is not anchored to the whole token (no ^...$) because
# _TOKEN_RE's own character class includes "/", "=", and "_", the same
# separators that show up around a UUID in real log text (a URL path
# segment like "/api/v1/orders/<uuid>", a query param like
# "request_id=<uuid>"). A UUID almost never appears as an isolated token
# on its own; it shows up as a substring of a longer token that swallowed
# its prefix. re.search() checks for a UUID-shaped run of characters
# anywhere in the token instead. A genuine secret coincidentally
# containing a substring matching this fixed hyphen-position,
# constrained-nibble shape isn't realistically possible at random.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


def shannon_entropy(token: str) -> float:
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def scan_entropy(text: str, min_len: int = 12, threshold: float = 3.3) -> list[dict]:
    hits = []
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        if _UUID_RE.search(token):
            continue
        if len(token) >= min_len and shannon_entropy(token) >= threshold:
            hits.append({
                "type": "HIGH_ENTROPY", "start": m.start(), "end": m.end(),
                "method": "entropy", "entropy": round(shannon_entropy(token), 2),
            })
    return hits


# --------------------------------------------------------------------------
# Layer 4: flattened-username name detection via dictionary segmentation.
# See src/flattened_names.py for the full rationale and stated limitations
# (name-dictionary coverage, synthetic-corpus circularity).
# --------------------------------------------------------------------------

def scan_flattened(text: str) -> list[dict]:
    from flattened_names import scan_flattened_names
    return scan_flattened_names(text)


# --------------------------------------------------------------------------
# Layer 5: field-level NER gate. Moved here from src/evaluate.py on
# 2026-08-09 (engineering upgrade: field-gated NER had been proven out in
# the evaluation harness across three measured iterations -- see
# build_ner_candidate's own docstring below and README.md's comparison
# table -- but was never reachable from production; src/service.py and
# src/pipeline.py both still called plain detect_all(), the naive/always-
# NER path). Living here instead of staying evaluate.py-only means
# service.py and pipeline.py can call detect_all_field_gated() directly
# instead of importing a validation/research script, and evaluate.py now
# imports these same functions rather than keeping a second, divergeable
# copy -- exactly the "one implementation, not two" principle this
# project's own README cites for why service.py wraps detect.py instead of
# reimplementing it in Ruby.
# --------------------------------------------------------------------------


def build_ner_candidate(text: str, log_type: str, regex_hits: list[dict]):
    """Field-level counterpart to a whole-line entropy/regex gate.

    A whole-line gate that skips NER for the entire line the moment ANY
    regex hit exists anywhere in it is cheap and fast, but is exactly the
    mechanism behind this project's documented PERSON recall collapse
    (0.113 tiered vs. 0.359 naive, measured live 2026-08-09 -- see
    README.md's comparison table and Known Limitations section): an SSN
    regex hit in one field silently suppresses NER for a PERSON name
    sitting in a completely different field on the same line. This
    function instead identifies which SPECIFIC field values a regex hit
    actually covers (via fields.py's structured extraction) and excludes
    only those from what NER sees, leaving every other field's content --
    including any PERSON name elsewhere on the line -- fully visible to
    NER.

    VERSION HISTORY, kept rather than deleted, because the mistake this
    corrects is itself informative: the original implementation of this
    function (`_mask_regex_covered_fields`, replaced 2026-08-09 the same
    day its numbers came back) MASKED regex-covered field spans by
    replacing them with same-length '#' placeholders, keeping the
    candidate text's total length unchanged. Measured against the real
    spaCy model that day: the recall fix worked (PERSON recall 0.113 ->
    0.356, nearly matching naive's 0.359, with better precision besides),
    but the throughput claim ("preserves some of the original throughput
    advantage") did NOT hold up -- field-gated measured ~100 events/sec,
    SLOWER than naive's ~119. Root cause, diagnosed after the fact: same-
    length masking doesn't reduce what spaCy actually has to tokenize and
    tag -- the candidate string NER received was exactly as long as the
    original line, so its cost was exactly naive's cost, plus this
    function's own extract_fields()/masking overhead on top, for a line
    that (correctly, for recall) still needed a real NER call almost
    every time a PERSON was actually present.

    This version fixes that by EXCISING regex-covered field spans instead
    of masking them -- physically removing those characters and splicing
    the surrounding text back together, so the candidate passed to NER is
    actually SHORTER than the original line, not just internally altered.
    Since NER cost scales with input length/token count, this is the
    correct lever for a real throughput improvement, not a same-length
    swap that only changes content.

    RE-MEASURED against the real model, same day (2026-08-09), after
    this rewrite: the recall prediction held almost exactly -- PERSON
    recall 0.360 (vs. the masking version's 0.356), precision 0.658 (vs.
    0.657), confirming excision and masking hide the identical
    characters from NER, just structured differently. Throughput
    improved substantially but the top-line numbers still looked like a
    small loss: ~110 events/sec, cutting the shortfall from 16.1% slower
    than naive down to 4.3% slower.

    THE REMAINING 4.3% TURNED OUT TO BE A MEASUREMENT ARTIFACT, found by
    fixing the profiling rather than optimizing further (still the same
    day). A first profiling pass covered only this function's own
    candidate path (the 4,606 of 10,000 lines with a regex hit); the
    other 5,394 lines fall through to run_evaluation's plain
    `scan_ner(text)` branch -- the exact same call naive makes for every
    line -- which the profiling never timed. Over half the run's NER
    cost was invisible to the comparison. Fixed: run_evaluation's
    `profile` parameter now also times that fallthrough branch, bucketed
    by whether the line had a regex hit, so the identical instrumentation
    run against naive gives a true controlled comparison on the SAME
    4,606-line subset the two strategies actually differ on. Result:
    field-gated measured 11.34ms/line vs. naive's 11.55ms/line on that
    subset. The shared 5,394-line subset (provably identical code in both
    conditions -- neither one treats a no-regex-hit line any differently)
    itself showed a 4.6% run-to-run difference despite running the same
    code on the same data, establishing that as the real noise floor on
    the test machine -- LARGER than the 1.8% "win" this measured, meaning
    that specific throughput-win claim is not actually distinguishable
    from noise at this sample size (10,000 lines / 4,606 regex-hit
    lines).

    RESOLVED (as unresolvable at this precision), same day, via a 10x
    (100,000-line) rerun plus a dedicated order-controlled A/B test
    (validation/field_gate_throughput_ab_test.py). The 100K rerun's
    regex-hit-subset gap came back smaller (1.24%), and its identical-
    code control subset's noise floor barely shrank versus 10K (4.2% on
    53,329 calls vs. 4.6% on 5,394) -- if that were ordinary sampling
    noise it should have shrunk by roughly sqrt(10). That pointed at a
    possible run-order confound (evaluate.py always profiles field-gated
    before naive, in the same process), so the A/B script alternated
    which condition ran first across 6 repetitions on a fixed 2,000-line
    sample. Result: per-repetition swings of -23.7% to +15.2%, a 95%
    confidence interval on the mean of [-23.7%, +5.5%] -- spanning zero
    by a wide margin, not reliably containing either single-pass
    estimate. Resolving a true ~1-2% effect against this much per-
    measurement noise would take roughly 400 repetitions, impractical on
    shared hardware and not worth it for an effect this size. The order-
    effect check also ruled out simple monotonic warmup specifically:
    field-gated got faster running 2nd, naive got SLOWER running 2nd --
    opposite directions, so it's ordinary system noise, not a warmup
    artifact. See tests/README.md and README.md's comparison table for
    the full numbers.

    HONEST CONCLUSION (SYNTHETIC DATA ONLY, SEE CORRECTION BELOW):
    field-gated matches or exceeds naive's recall, and shows a real,
    larger-sample-confirmed precision edge (0.651 vs. 0.629 at 100K
    lines) -- but its throughput is genuinely STATISTICALLY
    INDISTINGUISHABLE from naive's on this hardware, not confirmed
    faster and not confirmed slower. It is NOT a faster replacement for
    the whole-line tiered strategy, which remains the only genuinely
    fast option (~261-264 events/sec across runs) at tiered's own
    documented recall cost.

    CORRECTION, 2026-08-09, from real-data validation
    (validation/real_data/inject_and_evaluate.py, Task #10) -- this
    changes the recommendation above and should NOT be skipped when
    reading this docstring: the precision-edge claim above was only ever
    measured against this project's own 3 fixed synthetic syslog
    templates. Run for real against real, unmodified Loghub OpenSSH/Linux
    log text (with fields.py's syslog header-prefix gap simulated as
    fixed, i.e. field-gating actually engaging rather than falling back),
    the result FLIPPED: OpenSSH precision 0.974 -> 0.778 (FP 49 -> 523)
    for +1 TP; Linux precision 0.920 -> 0.797 (FP 122 -> 357) for +0 TP.
    Precision loss scaled directly with how often field-gating engaged;
    recall gain was ~0 in both. Wherever field-gating actually excises
    something on real, structurally diverse syslog text, it is
    manufacturing new false positives, not finding new true positives --
    the opposite of the synthetic result. Root cause not yet confirmed --
    see validation/real_data/diagnose_field_gate_false_positives.py,
    written to distinguish an accidental-new-adjacency effect (already
    disclosed as a risk above) from a context-loss effect (removing the
    excised span may reduce NER's confidence in classifying nearby
    tokens) -- run, and it found a specific, mechanical, fixable cause,
    not a diffuse one: virtually every new false positive on BOTH real
    datasets was the literal fragment "rhost=" (a dangling "key="
    left behind once its IP value was excised, with nothing meaningful
    following it) misclassified as PERSON by the real model at a
    suspiciously consistent 0.85 confidence -- ~500/2,000 lines on
    OpenSSH, ~320/2,000 on Linux. FIXED, same day: excision now removes
    the key+"=" along with the value whenever they're immediately
    adjacent (the exact shape fields.py's KV extractors produce), not
    just the value's own characters -- see the excision loop below for
    the fix itself and its own comment for why this is scoped narrowly
    enough not to touch CloudTrail's JSON shape at all. A small number
    (2/15 shown examples on OpenSSH) of a DIFFERENT artifact -- a
    fragment of the syslog TAG itself (e.g. "sshd[24239") misclassified
    as PERSON on a line where field-gating excised nothing at all --
    remains unexplained; it did not reproduce when field-gating changed
    nothing about the line, so it's more likely tied to the header-
    stripping SIMULATION itself (removing the timestamp/hostname context
    spaCy would otherwise see) than to excision, but this is not yet
    confirmed and is a smaller, separate open item, not blocking the fix
    above. RE-CONFIRMED against real data, same day: OpenSSH precision
    0.778 -> 0.987 (FP 523 -> 24, TP +1); Linux precision 0.797 -> 0.974
    (FP 357 -> 37, TP unchanged). Both now show FEWER false positives
    than the NAIVE baseline itself (OpenSSH 24 vs. naive's own 49; Linux
    37 vs. naive's own 122) -- not just closing the regression, a real
    precision improvement over naive on real, unmodified log text, with
    recall unchanged or better. This confirms the excision approach
    itself was sound; the bug was exactly the dangling-key-fragment
    mechanism above, nothing deeper.

    windows_event and cloudtrail have NOT been checked against any real
    (non-synthetic) data at all -- Loghub's OpenSSH/Linux/Thunderbird
    datasets are syslog-shaped, and no equivalent real dataset for those
    two log types has been sourced yet. Whether the same regression
    applies there is an open question, not confirmed absent. Both use
    more rigidly delimited extraction (JSON quote/comma boundaries,
    KV `=`/`;` boundaries) than syslog's free-text-heavy KV fallback, which
    is a plausible reason the effect could be smaller there -- a
    hypothesis, not evidence.

    PRODUCTION IMPACT: src/service.py and src/pipeline.py call this
    function as the default detection path for EVERY log_type, not just
    syslog. UPDATED after the re-confirmed fix above: syslog-header
    stripping is no longer blocked by an open, unquantified risk -- the
    specific mechanism that made it risky (the dangling key= fragment)
    is fixed and re-verified against real data, with field-gated now
    showing FEWER false positives than naive's own baseline once
    engaged. Adding header-stripping to logstash/redact-pipeline.conf is
    a reasonable next step, though still untested against a live
    Logstash instance specifically and not yet done. windows_event/
    cloudtrail traffic is NOT protected by fields.py's syslog-specific
    timestamp-prefix gap (they were never affected by it) and DOES
    actually engage field-gating today; no real (non-synthetic) dataset
    has been sourced for either type yet, so their real-data behavior
    remains an open question -- lower-probability given the fix above
    addressed a KV-syntax-specific artifact and both those log types use
    more rigidly delimited extraction, but not confirmed either way.

    A hit's [start, end) reported against the (shorter) candidate string
    is remapped back to the ORIGINAL text's coordinate space via
    `remap_hit` below and the `segments` this function returns, since
    callers need offsets into the real line, not the candidate.

    Returns one of three shapes:
      - (candidate, segments): NER should run on `candidate` (shorter
        than `text`, or equal to it if nothing was excised); `segments`
        is a list of (candidate_start, original_start) pairs, sorted by
        candidate_start, used by `remap_hit` to translate a hit's
        offsets back to `text`'s coordinate space. `segments` is `None`
        specifically when candidate == text unchanged (nothing excised,
        or fields.py didn't recognize this log_type/extracted nothing --
        the conservative, recall-safe fallback), since no remapping is
        needed in that case.
      - (None, None): every recognized field on this line was already
        regex-covered and nothing alphabetic remains outside those spans
        -- the one case where skipping the NER call entirely is safe.

    Known, disclosed limitation: field boundaries are located via
    text.find(value), since fields.py returns values, not offsets. This
    is correct except when a field's value string is not unique within
    the line (e.g. the same short value coincidentally appears earlier
    as a substring of something else) -- an edge case, not something
    this implementation claims to have ruled out, and one a real
    per-field extractor returning offsets directly would close properly.

    A second, disclosed limitation from excising rather than masking:
    splicing two previously-non-adjacent chunks directly together could,
    in principle, create an accidental new adjacency NER might
    misinterpret (e.g. two excised spans separated by nothing at all).
    In practice this is low-risk for the field shapes this project's
    fields.py extracts: KV-style values are always bounded by a
    delimiter (`=`, `;`, `,`, a space) that stays in the candidate on
    both sides of an excision, and CloudTrail's flattened JSON string
    values are always bounded by quote/punctuation characters for the
    same reason -- but it is a real, structurally different risk than
    same-length masking had, and is disclosed here rather than assumed
    away.

    The "safe to skip NER entirely" decision below only looks at
    extracted FIELD VALUES, not the whole line -- field names (`SRC=`,
    `PWD=`, the syslog tag prefix, KV separators) are structural, not
    PII-bearing, and checking the whole line's alphabetic content for
    the skip decision would make it almost never trigger, since field
    names are alphabetic too and appear on nearly every structured line.
    """
    import fields

    extracted = fields.extract_fields(log_type, text) if log_type else {}
    if not extracted:
        return text, None

    # search-cursor optimization, 2026-08-09: fields.py's own extractors
    # emit values in left-to-right source order, so a single forward-
    # advancing search cursor covers the same ground once across all
    # fields instead of once per field (each starting from position 0).
    # `text.find(value, search_cursor)` falls back to a from-the-start
    # search only if the forward search comes up empty, so correctness
    # never regresses versus always-from-0 in the worst case. This is a
    # SPEED optimization, not a fix for the "value isn't unique in the
    # line" ambiguity disclosed above -- it only helps when an earlier-
    # processed field has already advanced the cursor past a decoy
    # occurrence.
    search_cursor = 0
    excise_ranges = []
    any_alpha_value_unmasked = False
    for value in extracted.values():
        if not value:
            continue
        idx = text.find(value, search_cursor)
        if idx == -1:
            idx = text.find(value)
        if idx == -1:
            continue
        end = idx + len(value)
        search_cursor = end
        if any(h["start"] < end and idx < h["end"] for h in regex_hits):
            # Bug found via Task #10's real-data validation, 2026-08-09:
            # excising ONLY the value (as this used to do) leaves a
            # "key=" fragment dangling immediately before whitespace or
            # another excised span -- e.g. "rhost=218.188.2.4" becomes
            # "rhost= " once the IP is removed. Checked against real
            # Loghub OpenSSH/Linux data specifically (not hypothesized):
            # spaCy consistently misclassifies that orphaned "rhost="
            # fragment as PERSON (score 0.85, the overwhelming majority
            # of ~500 and ~320 new false positives measured on those two
            # datasets respectively, once field-gating actually engaged).
            # A short, out-of-context "word=" token with nothing
            # meaningful following it doesn't read as normal English,
            # and the model's own name-detection heuristic (unusual
            # token shape in a position a proper noun could occupy)
            # fires on it. Fix: if the value is immediately preceded by
            # a bare `key=` (no other separator between them -- the
            # exact shape fields.py's KV extractors produce), extend the
            # excision to remove that key+"=" too, not just the value.
            # The key name itself was never PII-bearing (field names are
            # structural, per this function's own docstring below), so
            # this loses nothing NER needed to see -- it just removes
            # the specific dangling-fragment shape that was causing the
            # misclassification. Scoped narrowly (requires a literal "="
            # immediately before the value, nothing else): CloudTrail's
            # JSON `"key": "value"` shape never matches this pattern (no
            # "=" separator), so this fix only ever engages for
            # windows_event/syslog's `=`-delimited KV fields, exactly
            # where the bug was found.
            key_match = re.search(r"[\w.-]+=$", text[:idx])
            excise_start = key_match.start() if key_match else idx
            excise_ranges.append((excise_start, end))
        elif any(c.isalpha() for c in value):
            any_alpha_value_unmasked = True

    if not excise_ranges:
        return text, None
    if not any_alpha_value_unmasked:
        return None, None

    excise_ranges.sort()
    merged = []
    for s, e in excise_ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    candidate_parts = []
    segments = []  # (candidate_start, original_start) per kept chunk
    cursor = 0
    candidate_len = 0
    for s, e in merged:
        if cursor < s:
            chunk = text[cursor:s]
            segments.append((candidate_len, cursor))
            candidate_parts.append(chunk)
            candidate_len += len(chunk)
        cursor = e
    if cursor < len(text):
        chunk = text[cursor:]
        segments.append((candidate_len, cursor))
        candidate_parts.append(chunk)

    return "".join(candidate_parts), segments


def remap_hit(hit: dict, segments: list[tuple[int, int]]) -> dict:
    """Translates a span reported against build_ner_candidate's (shorter)
    candidate text back into the ORIGINAL text's coordinate space, using
    `segments` (sorted (candidate_start, original_start) pairs). Every
    character kept in the candidate came from exactly one contiguous
    original chunk, so a hit's start position always falls inside exactly
    one segment; find it and apply that segment's constant offset to both
    start and end."""
    import bisect
    starts = [s[0] for s in segments]
    i = bisect.bisect_right(starts, hit["start"]) - 1
    candidate_start, original_start = segments[i]
    offset = original_start - candidate_start
    return {**hit, "start": hit["start"] + offset, "end": hit["end"] + offset}


# --------------------------------------------------------------------------
# Ensemble
# --------------------------------------------------------------------------

def detect_all(text: str, use_ner: bool = True, use_flattened: bool = True) -> list[dict]:
    hits = scan_regex(text)
    if use_ner:
        hits += scan_ner(text)
    hits += scan_entropy(text)
    if use_flattened:
        hits += scan_flattened(text)
    return hits


def detect_all_field_gated(text: str, log_type: str | None = None,
                            use_flattened: bool = True) -> list[dict]:
    """Field-gated counterpart to detect_all(): NER is skipped only for
    the specific regex-covered field values on a line (via
    build_ner_candidate above), not the whole line, and only when a
    log_type is given and fields.py recognizes it. When log_type is None
    or unrecognized, build_ner_candidate's own conservative fallback
    (candidate == text, unchanged) means this degrades to exactly
    detect_all(use_ner=True)'s behavior for that line -- safe to call
    even before a caller has log_type wired through (see src/service.py's
    /anonymize endpoint, which accepts an optional "log_type" field for
    exactly this reason).

    This is the production-facing counterpart to
    src/evaluate.py's run_evaluation(..., use_field_gate=True), which
    exists to compare this strategy's recall/precision/throughput against
    naive/tiered on labeled data. This function does the equivalent
    detection work without the evaluation harness's profiling/comparison
    machinery, which is research-only.
    """
    hits = scan_regex(text)
    if hits:
        ner_text, segments = build_ner_candidate(text, log_type, hits)
        if ner_text is not None:
            raw_hits = scan_ner(ner_text)
            if segments is not None:
                raw_hits = [remap_hit(h, segments) for h in raw_hits]
            hits += raw_hits
        # else: every regex-coverable field was excised and nothing
        # alphabetic remained outside those spans -- safe to skip the NER
        # call entirely (see build_ner_candidate's docstring).
    else:
        hits += scan_ner(text)
    hits += scan_entropy(text)
    if use_flattened:
        hits += scan_flattened(text)
    return hits
