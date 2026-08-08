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


def scan_regex(text: str) -> list[dict]:
    hits = []
    for label, pattern in REGEX_PATTERNS.items():
        for m in pattern.finditer(text):
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
