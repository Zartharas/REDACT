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
        if len(token) >= min_len and shannon_entropy(token) >= threshold:
            hits.append({
                "type": "HIGH_ENTROPY", "start": m.start(), "end": m.end(),
                "method": "entropy", "entropy": round(shannon_entropy(token), 2),
            })
    return hits


# --------------------------------------------------------------------------
# Ensemble
# --------------------------------------------------------------------------

def detect_all(text: str, use_ner: bool = True) -> list[dict]:
    hits = scan_regex(text)
    if use_ner:
        hits += scan_ner(text)
    hits += scan_entropy(text)
    return hits
