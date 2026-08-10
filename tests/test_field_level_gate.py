"""
Tests the field-level NER gate (src/evaluate.py's _build_ner_candidate,
_remap_hit, and the use_field_gate branch of run_evaluation) -- the
engineering upgrade meant to close the PERSON recall gap the whole-line
tiered strategy has (0.113 tiered vs. 0.359 naive on this project's own
current corpus, see README.md's comparison table).

Cannot exercise this against the real spaCy/Presidio model in this
environment (no network access to fetch en_core_web_lg here, same
constraint noted throughout BUGS_AND_FIXES.md and tests/README.md).
Instead, monkeypatches detect.scan_ner to a stub that just records what
text it was called with, so these tests verify the actual claims that
matter mechanically: which characters get excised before NER runs, that
the candidate is actually SHORTER (not just internally altered) when
something is excised, that a hit's offsets correctly round-trip back to
the original text's coordinate space, and when the NER call is skipped
entirely vs. made. The real recall/throughput numbers this is meant to
improve need a live run of `python src/evaluate.py` in an environment
with the model available.

HISTORY: this file originally tested `_mask_regex_covered_fields`, a
same-length masking approach (regex-covered spans replaced with '#'
placeholders). Measured against the real model 2026-08-09: the recall
fix worked, the throughput claim did not -- same-length masking doesn't
reduce what spaCy has to process, so field-gated measured SLOWER than
naive. Replaced the same day with `_build_ner_candidate`, which excises
(removes, doesn't just mask) regex-covered spans so the candidate is
genuinely shorter, plus `_remap_hit` to translate resulting offsets back
to the original text. Re-measured: recall carried over almost exactly,
throughput improved substantially (16.1% slower than naive -> 4.3%
slower) but didn't fully close the gap. Same day, still chasing that
remaining gap: added a forward-advancing search cursor to
_build_ner_candidate's field-value lookup (previously a bare
text.find(value) per field, rescanning from position 0 every time) and
a `profile` dict on run_evaluation to measure candidate-build time vs.
NER-call time separately, so the next real-model run gives actual
evidence instead of more reasoning about which hypothesis is more
plausible. This file was rewritten/extended to match each change; see
src/evaluate.py's own docstring for the full history and reasoning.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import detect  # noqa: E402
import evaluate  # noqa: E402


def test_excise_shortens_candidate_and_keeps_unmasked_content():
    """A windows_event line with an SSN in one field and a name in
    another: the SSN field's characters should be REMOVED (not just
    replaced), the name should survive untouched, and the candidate
    should be strictly shorter than the original -- this length
    reduction is the whole point of excising instead of masking.

    Engineering upgrade, 2026-08-09 (Task #10's real-data validation):
    excision now removes the "key=" prefix along with the value when
    they're immediately adjacent ("TargetSSN=123-45-6789", not just
    "123-45-6789"), not only the value's own characters -- a dangling
    "key=" fragment left behind by the old value-only excision was
    getting misclassified as PERSON by the real spaCy model on real
    Loghub data (see detect.build_ner_candidate's own docstring for the
    full root-cause writeup). The length assertion below reflects that:
    the whole "TargetSSN=123-45-6789" key+value pair is gone now, not
    just the SSN digits."""
    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "SSN" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    assert candidate is not None
    assert "Timothy Wong" in candidate, "name field must survive unexcised"
    assert "123-45-6789" not in candidate, "regex-covered SSN field must be excised"
    assert "TargetSSN=" not in candidate, "the dangling key= prefix must be excised too"
    assert len(candidate) < len(text), "excising must actually shorten the candidate"
    assert len(candidate) == len(text) - len("TargetSSN=123-45-6789")
    assert segments is not None


def test_build_candidate_returns_none_when_nothing_alphabetic_remains():
    """A line that's entirely numeric/structured fields, all regex-covered
    -- nothing left for NER to plausibly find, so the caller should skip
    the NER call entirely."""
    text = "SRC=10.0.0.5 DST=10.0.0.9"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "IP" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    assert candidate is None
    assert segments is None


def test_build_candidate_falls_back_to_original_text_for_unrecognized_log_type():
    """No field structure known (e.g. log_type not in fields.py's coverage)
    -- must fall back to the original text, not silently skip NER. Falling
    back to the ORIGINAL text (not None) with segments=None is the
    conservative, recall-safe default when field boundaries aren't known,
    and needs no offset remapping since nothing was excised."""
    text = "some free text line with Timothy Wong in it, SSN 123-45-6789"
    regex_hits = detect.scan_regex(text)

    candidate, segments = evaluate._build_ner_candidate(text, "some_unknown_type", regex_hits)

    assert candidate == text
    assert segments is None


def test_build_candidate_returns_unchanged_text_when_nothing_is_excised():
    """Fields are recognized, but none of them happen to overlap a regex
    hit -- nothing to excise, so the candidate should equal the original
    text (not an empty-excise-ranges edge case that accidentally mangles
    anything), with segments=None since no remapping is needed."""
    text = "TargetUserName=Timothy Wong TargetDept=Engineering"
    regex_hits = detect.scan_regex(text)
    assert regex_hits == []

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    assert candidate == text
    assert segments is None


def test_excision_removes_dangling_key_equals_not_just_the_value():
    """Real-bug regression guard, found via Task #10's real-data
    validation (2026-08-09): the ORIGINAL excise-just-the-value
    implementation left a "key=" fragment dangling immediately before
    whitespace/end-of-string whenever the excised value was immediately
    preceded by a bare "key=" -- e.g. "rhost=218.188.2.4" became
    "rhost= " once the IP was removed. Measured against real Loghub
    OpenSSH/Linux data: spaCy consistently misclassified that orphaned
    "rhost=" fragment as PERSON (score 0.85), producing ~500 and ~320 new
    false positives respectively that naive never had -- a real
    precision regression, not a hypothetical one. Confirms the fix: the
    key+"=" is now excised along with the value, so no dangling
    fragment survives into the candidate at all."""
    text = "sshd[24200]: authentication failure; rhost=218.188.2.4 user=root"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "IP" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "syslog", regex_hits)

    assert candidate is not None
    assert "218.188.2.4" not in candidate
    assert "rhost=" not in candidate, (
        "the dangling key= fragment that spaCy misclassified as PERSON on "
        "real data must not survive into the NER candidate"
    )
    # user=root's value has no alpha content worth flagging as a name and
    # isn't regex-covered either -- unaffected either way, present here
    # just to confirm the fix doesn't over-excise neighboring fields.
    assert "user=root" in candidate


def test_remap_hit_translates_offsets_back_to_original_text():
    """The correctness-critical piece: a hit's [start, end) reported
    against the (shorter) candidate must map back to the exact matching
    substring in the ORIGINAL text, not an off-by-N position shifted by
    however much was excised before it."""
    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789 EventTime=2024-01-01"
    regex_hits = detect.scan_regex(text)
    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    idx = candidate.index("Timothy Wong")
    hit = {"type": "PERSON", "start": idx, "end": idx + len("Timothy Wong"), "method": "ner"}
    remapped = evaluate._remap_hit(hit, segments)

    assert text[remapped["start"]:remapped["end"]] == "Timothy Wong"


def test_remap_hit_after_excised_span_still_resolves_correctly():
    """A hit sitting AFTER the excised span in the candidate (so its
    candidate-relative offset is shifted left of where it'd be in the
    original text) must still remap to the correct original position --
    this is the case an off-by-a-constant-everywhere bug would miss,
    since it specifically requires per-segment offsets, not one global
    shift.

    Engineering upgrade, 2026-08-09: the original text here
    ("SSNTargetSSN=123-45-6789 TargetUserName=Timothy Wong") stopped
    genuinely exercising multiple segments once excision started
    removing the "key=" prefix along with the value (see
    test_excise_shortens_candidate_and_keeps_unmasked_content's own
    docstring) -- the excised range now starts at position 0 (the "SSN"
    text merges into "TargetSSN="'s own excision), leaving only ONE kept
    segment, so this test was silently passing without actually
    distinguishing per-segment offsets from a single global shift
    anymore. Replaced with a text where the excised span (an IP address)
    sits strictly BETWEEN two separate kept chunks -- both containing
    "Timothy Wong" -- to restore genuine multi-segment coverage: the
    SECOND occurrence lands in the second kept segment, which needs its
    own, different offset than the first."""
    text = "TargetUserName=Timothy Wong SRC=10.0.0.5 TargetDept=Timothy Wong"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "IP" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)
    assert segments is not None and len(segments) >= 2, (
        "this test needs a genuinely multi-segment candidate to be meaningful"
    )

    idx = candidate.rindex("Timothy Wong")  # the SECOND occurrence, after the excised IP
    hit = {"type": "PERSON", "start": idx, "end": idx + len("Timothy Wong"), "method": "ner"}
    remapped = evaluate._remap_hit(hit, segments)

    assert text[remapped["start"]:remapped["end"]] == "Timothy Wong"
    assert remapped["start"] == text.rindex("Timothy Wong")


def test_run_evaluation_field_gate_calls_ner_on_excised_text_and_remaps_correctly(monkeypatch):
    """End-to-end through run_evaluation: confirms the field-gated branch
    reaches detect.scan_ner with the excised (shorter, not the original,
    not skipped) text, and that the resulting hit's offsets -- reported
    relative to the excised candidate -- get correctly remapped back to
    the ORIGINAL log line's coordinates before being scored against gold
    spans."""
    calls = []

    def fake_scan_ner(text, min_score=0.5):
        calls.append(text)
        if "Timothy Wong" in text:
            start = text.index("Timothy Wong")
            return [{"type": "PERSON", "start": start, "end": start + len("Timothy Wong"),
                      "method": "ner", "score": 0.9}]
        return []

    monkeypatch.setattr(detect, "scan_ner", fake_scan_ner)

    entries = [{
        "log": "TargetUserName=Timothy Wong TargetSSN=123-45-6789",
        "log_type": "windows_event",
        "pii": [
            {"start": 15, "end": 28, "type": "PERSON"},
            {"start": 39, "end": 50, "type": "SSN"},
        ],
    }]

    per_type, _ = evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True)

    assert len(calls) == 1, "NER should be called exactly once, on the excised candidate"
    assert "123-45-6789" not in calls[0]
    assert "Timothy Wong" in calls[0]
    assert len(calls[0]) < len("TargetUserName=Timothy Wong TargetSSN=123-45-6789")
    # PERSON must be found at the ORIGINAL text's coordinates (gold span
    # is [15, 28) in "TargetUserName=Timothy Wong TargetSSN=..."), which
    # only works if _remap_hit's offset translation is correct -- a bug
    # there would silently produce a wrong-offset prediction that fails
    # to overlap the gold span, showing up as tp=0, fn=1 instead.
    assert per_type["PERSON"]["tp"] == 1
    assert per_type["PERSON"]["fn"] == 0


def test_run_evaluation_field_gate_skips_ner_when_fully_covered(monkeypatch):
    """The other half of the claim: when every alphabetic field value is
    regex-covered, run_evaluation must not call scan_ner at all -- this
    is the ONLY mechanism by which any throughput benefit over naive can
    come from this strategy (see src/evaluate.py's own docstring on why
    it's measured to fire rarely on lines that actually contain a
    PERSON, and often on lines that don't)."""
    calls = []
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: calls.append(text) or [])

    entries = [{
        "log": "SRC=10.0.0.5 DST=10.0.0.9",
        "log_type": "windows_event",
        "pii": [],
    }]

    evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True)

    assert calls == [], "NER must not be called when nothing unexcised remains"


def test_cursor_based_search_finds_correct_occurrence_past_a_decoy():
    """Engineering upgrade, 2026-08-09 (chasing the remaining 4.3%
    throughput gap): _build_ner_candidate's field-value search now
    advances a cursor across fields instead of restarting text.find()
    from position 0 for every field, since fields.py emits values in
    left-to-right source order. This is primarily a speed optimization
    (avoids O(len(text)) rescans), verified here on a case where an
    earlier field's processing has already advanced the cursor past a
    decoy occurrence of a LATER field's value -- confirming the
    forward search doesn't accidentally break correctness for the
    common case, not that it resolves the pre-existing "value isn't
    unique in the line" ambiguity in general (it doesn't, when the
    FIRST field processed is the one with an earlier decoy -- see
    _build_ner_candidate's own updated comment)."""
    # "Timothy Wong" appears twice: once as decoy free text at the very
    # start, once as TargetUserName's real value after TargetSSN (which
    # gets processed first and advances the cursor past the decoy).
    text = "Timothy Wong said hi TargetSSN=123-45-6789 TargetUserName=Timothy Wong"
    regex_hits = detect.scan_regex(text)
    assert any(h["type"] == "SSN" for h in regex_hits)

    candidate, segments = evaluate._build_ner_candidate(text, "windows_event", regex_hits)

    # "TargetSSN=" is gone too, not just "123-45-6789" -- see the
    # key+"=" excision fix in test_excise_shortens_candidate_and_keeps_unmasked_content's
    # own docstring; the two original spaces (one before "TargetSSN",
    # one after "123-45-6789") both survive, producing a double space
    # where the whole key=value pair used to be.
    assert candidate == "Timothy Wong said hi  TargetUserName=Timothy Wong"
    assert "123-45-6789" not in candidate
    assert "TargetSSN=" not in candidate
    # Both the decoy (untouched free text) and the real field occurrence
    # (never regex-covered, so never excised) must survive -- excision
    # only ever removes the SSN key=value pair.
    assert candidate.count("Timothy Wong") == 2


def test_run_evaluation_populates_profile_dict():
    """Engineering upgrade, 2026-08-09: run_evaluation's optional
    profile dict, added to give real, measurable evidence for where
    time goes in the field-gated path (candidate-building vs. the NER
    call itself) instead of continuing to guess. Confirms the dict gets
    populated with the expected keys and plausible values, using the
    same fake_scan_ner stub as other tests here (this only checks that
    the profiling plumbing works, not real timing numbers -- those need
    the real model)."""
    monkeypatch_calls = []

    def fake_scan_ner(text, min_score=0.5):
        monkeypatch_calls.append(text)
        return []

    entries = [
        {
            "log": "TargetUserName=Timothy Wong TargetSSN=123-45-6789",
            "log_type": "windows_event",
            "pii": [],
        },
        {
            "log": "SRC=10.0.0.5 DST=10.0.0.9",
            "log_type": "windows_event",
            "pii": [],
        },
    ]

    original_scan_ner = detect.scan_ner
    detect.scan_ner = fake_scan_ner
    try:
        profile: dict = {}
        evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True, profile=profile)
    finally:
        detect.scan_ner = original_scan_ner

    assert "candidate_build_seconds" in profile
    assert profile["candidate_build_seconds"] >= 0.0
    assert "ner_call_seconds" in profile
    assert profile["ner_call_seconds"] >= 0.0
    # First entry has an unmasked name -> one real NER call.
    # Second entry is fully regex-covered -> skipped entirely.
    assert profile.get("ner_calls_made", 0) == 1
    assert profile.get("ner_calls_skipped", 0) == 1


def test_profile_dict_covers_the_no_regex_hit_fallthrough_branch(monkeypatch):
    """Engineering upgrade, 2026-08-09: the first version of this
    profiling only measured the field-gated candidate path, silently
    missing every line that falls through to the plain
    `else: scan_ner(text)` branch because it has NO regex hit at all --
    on the real corpus this was 5,394 of 10,000 lines, more than half
    the run's NER cost invisible to the measurement. Confirms a
    no-regex-hit line's scan_ner call now shows up under
    'no_regex_hit_ner_seconds'/'no_regex_hit_ner_calls', for the
    field-gated condition specifically (this is the fall-through branch
    a plain PERSON-only free-text line with no SSN/EMAIL/IP/etc. would
    take even with use_field_gate=True)."""
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: [])

    entries = [{
        "log": "a free text line mentioning Timothy Wong, nothing regex-shaped",
        "log_type": "windows_event",
        "pii": [],
    }]

    profile: dict = {}
    evaluate.run_evaluation(entries, use_ner=True, use_field_gate=True, profile=profile)

    assert profile.get("no_regex_hit_ner_calls", 0) == 1
    assert profile.get("no_regex_hit_ner_seconds", 0.0) >= 0.0
    # This line never reached the field-gated candidate path at all.
    assert profile.get("ner_calls_made", 0) == 0
    assert profile.get("ner_calls_skipped", 0) == 0


def test_detect_all_field_gated_excises_regex_covered_field_before_ner(monkeypatch):
    """Engineering upgrade, 2026-08-09: detect.detect_all_field_gated() is
    the production-facing counterpart to run_evaluation(...,
    use_field_gate=True) -- src/service.py and src/pipeline.py now call
    this directly instead of detect.detect_all() (naive). Confirms the
    ensemble-level function reaches scan_ner with the excised candidate
    and correctly remaps offsets, the same claims tested against
    _build_ner_candidate/_remap_hit directly above, but through the
    actual function production code calls."""
    calls = []

    def fake_scan_ner(text, min_score=0.5):
        calls.append(text)
        if "Timothy Wong" in text:
            start = text.index("Timothy Wong")
            return [{"type": "PERSON", "start": start, "end": start + len("Timothy Wong"),
                      "method": "ner", "score": 0.9}]
        return []

    monkeypatch.setattr(detect, "scan_ner", fake_scan_ner)

    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789"
    hits = detect.detect_all_field_gated(text, log_type="windows_event", use_flattened=False)

    assert len(calls) == 1
    assert "123-45-6789" not in calls[0]
    assert "Timothy Wong" in calls[0]

    person_hits = [h for h in hits if h["type"] == "PERSON"]
    assert len(person_hits) == 1
    assert text[person_hits[0]["start"]:person_hits[0]["end"]] == "Timothy Wong"
    ssn_hits = [h for h in hits if h["type"] == "SSN"]
    assert len(ssn_hits) == 1  # still caught by scan_regex, independent of the NER gate


def test_detect_all_field_gated_falls_back_to_naive_when_log_type_missing(monkeypatch):
    """No log_type (e.g. a caller that hasn't wired it through yet, like
    logstash/redact-pipeline.conf before its own log_type-forwarding
    change) must still run NER on the full text -- the same conservative,
    recall-safe fallback build_ner_candidate already guarantees, exercised
    here through the ensemble function itself."""
    calls = []
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: calls.append(text) or [])

    text = "TargetUserName=Timothy Wong TargetSSN=123-45-6789"
    detect.detect_all_field_gated(text, log_type=None, use_flattened=False)

    assert calls == [text], "missing log_type must fall back to running NER on the full text"


def test_detect_all_field_gated_skips_ner_when_no_regex_hit(monkeypatch):
    """A line with no regex hit at all takes the else-branch straight to
    scan_ner(text) -- same behavior as naive for that line, confirmed
    here at the ensemble level."""
    calls = []
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: calls.append(text) or [])

    text = "a free text line mentioning Timothy Wong, nothing regex-shaped"
    detect.detect_all_field_gated(text, log_type="windows_event", use_flattened=False)

    assert calls == [text]


def test_profile_dict_naive_run_buckets_by_regex_hit_presence(monkeypatch):
    """Confirms the SAME profiling, run against a plain naive
    configuration (use_field_gate=False, use_entropy_gate=False), splits
    calls into the 'regex_hit' and 'no_regex_hit' buckets correctly --
    this is what makes the controlled, same-subset comparison in
    evaluate.py's __main__ possible: naive's regex_hit_ner_seconds on
    the has-a-regex-hit lines is directly comparable to field-gated's
    candidate_build_seconds + ner_call_seconds on the identical subset,
    since both are populated by profiling the SAME lines."""
    monkeypatch.setattr(detect, "scan_ner", lambda text, min_score=0.5: [])

    entries = [
        {  # has a regex hit (SSN)
            "log": "TargetUserName=Timothy Wong TargetSSN=123-45-6789",
            "log_type": "windows_event",
            "pii": [],
        },
        {  # no regex hit at all
            "log": "a free text line mentioning Timothy Wong, nothing regex-shaped",
            "log_type": "windows_event",
            "pii": [],
        },
    ]

    profile: dict = {}
    evaluate.run_evaluation(entries, use_ner=True, use_entropy_gate=False, profile=profile)

    assert profile.get("regex_hit_ner_calls", 0) == 1
    assert profile.get("no_regex_hit_ner_calls", 0) == 1
    # Naive never touches the field-gated-specific keys at all.
    assert "candidate_build_seconds" not in profile
    assert "ner_calls_made" not in profile
