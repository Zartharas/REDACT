"""
Layer (opt-in): Russian-language detection patterns, added specifically to
evaluate REDACT against the real-context/synthetic-value Russian PII pilot
sample (validation/real_data/datasets/OpenRU_PII_raw.jsonl -- see
validation/real_data/RU_TYPE_MAPPING.md for the entity-type mapping this
module targets).

DELIBERATELY SEPARATE FROM detect.py, NOT A MODIFICATION OF IT. Same
discipline as es_detect.py/fr_detect.py: never touch an existing,
already-measured pattern to fix or extend a different case -- add a new,
narrow, additive path instead. Nothing in detect_all()/
detect_all_field_gated()/detect_all_schema_aware() calls into this module
automatically -- it is called explicitly, only by evaluate_ru.py.

WHY THIS MODULE'S NAME LAYER IS STRUCTURALLY DIFFERENT FROM es_detect.py/
fr_detect.py'S -- THE FLAGGED GOTCHA, ADDRESSED FROM THE START:
LANGUAGE_EXTENSION.md explicitly warned that Russian is heavily inflected,
so the Spanish/French `_CAP_RUN_RE`-plus-exact-string-dictionary-lookup
approach "won't port cleanly." Confirmed directly before writing any code:
Russian surnames, patronymics, and place names inflect by case (e.g. a
sentence has "Курганской области" -- genitive case of "Курганская
область" -- not the nominative dictionary form), so an exact-string
dictionary match would silently miss every inflected occurrence even when
the dictionary genuinely contains the word. `scan_russian_names()` below
lemmatizes both the candidate word AND the dictionary (via `pymorphy2`,
MIT-licensed, and its bundled Russian dictionary data) before comparing,
not after a low-recall number was observed -- e.g. "Леонтьевич" (a
patronymic appearing already in its nominative form in several staged
sentences) lemmatizes via pymorphy2 to "леонтиевич" (a normalized
spelling variant), and comparing raw dictionary strings against that
lemma would silently fail even for an ALREADY-nominative word, which is
exactly why both sides of the comparison are lemmatized through the same
function rather than only the input.

A SEPARATE, GENUINE FINDING THIS SURFACED (NOT THE SAME PROBLEM AS
INFLECTION, DON'T CONFLATE THEM): after lemmatizing both sides, several
staged surnames ("Стрельцов", "Зубарева", "Терехов") still do not match
Faker's `ru_RU` dictionary at all -- confirmed directly this is a
DICTIONARY-COVERAGE gap (the raw lowercased surnames are simply absent
from Faker's list of 250 male / 250 female surnames), not a residual
lemmatization failure. This is the same class of result the French pass
found (0/11 dictionary-name-overlap on OpenPII's synthetic given-names) --
expect `evaluate_ru.py` to surface a real, non-trivial PERSON recall gap
from this even with the inflection problem correctly solved, and
`--diagnose` root-causes it the same way, not swept into a single bare
number.

ANOTHER GENUINE, SANDBOX-RELEVANT FINDING: unlike the NER model download
(network-blocked, Docker-only, same limitation as every other language in
this project), `pip install pymorphy2 pymorphy2-dicts-ru` succeeds
directly in this project's own sandbox -- PyPI is reachable even though
huggingface.co/github.com/spaCy's model-release infrastructure is not.
This means the lemmatization layer below can actually run and be measured
in this sandbox without the Docker handoff the NER layer still needs.

WHY THIS MODULE ALSO ADDS AN IPv6 PATTERN, EVEN THOUGH THAT IS NOT A
RUSSIAN-SPECIFIC PROBLEM: see RU_TYPE_MAPPING.md's IP_ADDRESS row. This
Russian sample's real IP_ADDRESS gold spans include full IPv6 addresses;
detect.py's existing IP pattern (`\\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b`) is
IPv4-only. Rather than leave every IPv6 gold span unscorable, an IPv6
regex is added HERE as a disclosed, general-format addition this dataset
happened to surface -- explicitly not because Russian text has anything
IPv6-specific about it, and explicitly not by modifying detect.py's own
IP pattern (same "never touch an existing, measured pattern" discipline).

SNILS / OMS REGEX PATTERNS (-> REDACT canonical SSN / MRN): both are
label-anchored (require "снилс"/"ОМС"/"полис" nearby), same design
principle as es_detect.py's NHC_ES/NASS_ES and REDACT's own existing MRN
pattern -- an unanchored 11- or 16-digit run is not distinctive enough to
safely match (the 16-digit OMS shape in particular is otherwise
indistinguishable from CREDIT_CARD). See RU_TYPE_MAPPING.md for the
observed real formats these patterns were built from.
"""
import re

from faker.providers.person.ru_RU import Provider as _RuRUPersonProvider

FIRST_NAMES_RU = {n.lower() for n in
                   (*_RuRUPersonProvider.first_names_male, *_RuRUPersonProvider.first_names_female)}
LAST_NAMES_RU = {n.lower() for n in
                  (*_RuRUPersonProvider.last_names_male, *_RuRUPersonProvider.last_names_female)}
MIDDLE_NAMES_RU = {n.lower() for n in
                    (*_RuRUPersonProvider.middle_names_male, *_RuRUPersonProvider.middle_names_female)}

_ALL_NAME_DICT = FIRST_NAMES_RU | LAST_NAMES_RU | MIDDLE_NAMES_RU

# --------------------------------------------------------------------------
# Lemmatization (pymorphy2) -- both the dictionary and each candidate word
# are normalized through the SAME function before comparison, so an
# already-nominative dictionary spelling and pymorphy2's own normalized
# spelling variant of it are compared on equal footing. See module
# docstring for why this matters (the "Леонтьевич" example).
# --------------------------------------------------------------------------
_morph = None
_LEMMA_DICT_CACHE = None


def _get_morph():
    global _morph
    if _morph is None:
        # Imported lazily: pymorphy2's dictionary load is the expensive
        # part of startup, same reasoning as detect.py's/es_ner.py's lazy
        # NER-engine imports -- callers that don't need name detection
        # shouldn't pay for it.
        import pymorphy2
        _morph = pymorphy2.MorphAnalyzer()
    return _morph


def _lemma(word: str) -> str:
    return _get_morph().parse(word.lower())[0].normal_form


def _lemmatized_dict() -> set:
    global _LEMMA_DICT_CACHE
    if _LEMMA_DICT_CACHE is None:
        _LEMMA_DICT_CACHE = {_lemma(w) for w in _ALL_NAME_DICT}
    return _LEMMA_DICT_CACHE


# --------------------------------------------------------------------------
# Name runs -> PERSON (space-separated, Cyrillic + occasional Latin-script
# runs -- several staged sentences mix scripts within one name, e.g.
# "Шестакова Josephine вениаминович" or "Desmond Рябова", so the character
# class below accepts both rather than assuming Cyrillic-only.)
# --------------------------------------------------------------------------
_CAP_RUN_RE = re.compile(
    r"[A-ZА-ЯЁ][a-zа-яёA-ZА-ЯЁ]+(?: [A-ZА-ЯЁ][a-zа-яёA-ZА-ЯЁ]+){0,3}",
    re.UNICODE,
)
MIN_WORD_LEN = 3


def scan_russian_names(text: str) -> list[dict]:
    """PERSON hits for space-separated Russian name runs, via
    lemmatized dictionary lookup (see module docstring for why exact-string
    matching, the Spanish/French approach, does not work for Russian)."""
    hits = []
    lemma_dict = _lemmatized_dict()
    for run_m in _CAP_RUN_RE.finditer(text):
        run = run_m.group(0)
        words = run.split(" ")
        if not any(
            len(w) >= MIN_WORD_LEN and _lemma(w) in lemma_dict
            for w in words
        ):
            continue
        hits.append({
            "type": "PERSON",
            "start": run_m.start(),
            "end": run_m.end(),
            "method": "ru_name_dict",
        })
    return hits


# --------------------------------------------------------------------------
# SNILS (individual insurance account number) -> SSN
# --------------------------------------------------------------------------
# Label ("снилс", case-insensitive), then an 11-digit run in one of the
# observed groupings: bare ("11223344556") or dot/space/hyphen-separated
# in a 3-3-3-2 pattern ("123.456.789.01"). See RU_TYPE_MAPPING.md for the
# staged examples this was built from.
SNILS_RU = re.compile(
    r"\bснилс\s*[:\-]?\s*(?:\d{11}|\d{3}[.\-\s]\d{3}[.\-\s]\d{3}[.\-\s]\d{2})\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# OMS (mandatory medical insurance policy number) -> MRN
# --------------------------------------------------------------------------
# Label ("ОМС" or "полис", case-insensitive) within ~20 characters before
# a 16-digit run in any of the observed separator styles (bare, space,
# "=", or "|"). Deliberately label-anchored, not just a bare \d{16} --
# an unanchored 16-digit run is indistinguishable from CREDIT_CARD.
OMS_RU = re.compile(
    r"(?:ОМС|полис)[^\d]{0,20}(\d{4}[=|\s]?\d{4}[=|\s]?\d{4}[=|\s]?\d{4}|\d{16})",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# IPv6 -> IP (general-format addition, not Russian-specific -- see module
# docstring's "WHY THIS MODULE ALSO ADDS AN IPv6 PATTERN" section)
# --------------------------------------------------------------------------
IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
)


def scan_ru_regex(text: str) -> list[dict]:
    """Regex layer: SNILS -> SSN, OMS -> MRN, IPv6 -> IP."""
    hits = []
    for m in SNILS_RU.finditer(text):
        hits.append({"type": "SSN", "start": m.start(), "end": m.end(), "method": "ru_regex_snils"})
    for m in OMS_RU.finditer(text):
        # Anchor the hit span to the matched digit group (group 1), not
        # the label text, so it aligns with the gold span's boundaries.
        hits.append({"type": "MRN", "start": m.start(1), "end": m.end(1), "method": "ru_regex_oms"})
    for m in IPV6_RE.finditer(text):
        hits.append({"type": "IP", "start": m.start(), "end": m.end(), "method": "ru_regex_ipv6"})
    return hits


def scan_ru(text: str) -> list[dict]:
    """Combined Russian-aware layer: regex (SNILS/OMS/IPv6) + lemmatized
    name dictionary. Same hit-dict shape as detect.py's scan_regex()/
    scan_ner() and es_detect.py's/fr_detect.py's scan_es()/scan_fr(), so
    results concatenate directly with a harness that wants the union."""
    return scan_ru_regex(text) + scan_russian_names(text)
