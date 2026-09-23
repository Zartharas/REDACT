"""
Layer 5 (opt-in): Spanish-language detection patterns, added specifically to
evaluate REDACT against the real MEDDOCAN corpus (validation/real_data/
datasets/MEDDOCAN_raw.jsonl -- see validation/real_data/MEDDOCAN_TYPE_MAPPING.md
for the entity-type mapping this module targets).

DELIBERATELY SEPARATE FROM detect.py, NOT A MODIFICATION OF IT. This
project's own discipline (see detect.py's inline history of the AWS
account-ID and Zookeeper thread-ID collisions) is: never touch an existing,
already-measured pattern to fix or extend a different case -- add a new,
narrow, additive path instead. detect.py's REGEX_PATTERNS, scan_regex(),
scan_ner(), and flattened_names.py's English name dictionary are UNCHANGED
by this file. Nothing in detect_all()/detect_all_field_gated()/
detect_all_schema_aware() calls into this module automatically -- it is
called explicitly, only by the MEDDOCAN evaluation harness (Task 6), so
existing English/US-corpus recall and precision numbers cannot regress by
this file's mere existence.

THREE PATTERNS, EACH WITH ITS OWN DISCLOSED LIMITATION:

1. NHC_ES (-> REDACT canonical MRN): Spanish patient-ID / "Número de
   Historia Clínica". Real observed formats, taken directly from staged
   MEDDOCAN documents: "NHC: 368503", "NHC:9764132 3", "CIPA: nhc-272226",
   "CIPA: nhc/976421", "CIPA: 1913043" (label with no "nhc-" infix at all).
   Unlike REDACT's existing MRN pattern (\\bMRN-\\d{7}\\b, a single fixed
   shape), NHC has no single canonical format across sources -- this
   pattern requires the NHC/CIPA label immediately before the value
   (mirroring MRN's own prefix-anchored design, not a weaker one) and
   accepts the variable digit-grouping/prefix shapes actually observed.
   KNOWN GAP: a bare NHC digit string appearing without its label nearby
   (e.g. copy-pasted into a different context) will not match -- same
   class of limitation as MRN's own label-anchored design already has.

2. NASS_ES (-> REDACT canonical SSN): Spanish Social Security affiliation
   number. Real observed formats: "14-63504522-08", "28 23748589 45",
   "87 565306 30", "26 63514095" (missing the trailing 2-digit check
   block). This pattern matches the 3-group shape (2 digits, 6-9 digits,
   2 digits) which is distinctive enough to need no label anchor, the same
   design REDACT's existing SSN pattern (\\d{3}-\\d{2}-\\d{4}) already uses.
   KNOWN GAP: the 2-group variant (missing the trailing check block, e.g.
   "26 63514095") is NOT matched by design -- a bare "2 digits + 6-9
   digits" shape is far too easy to collide with phone numbers, patient
   ages, or other incidental digit pairs to safely match without a label
   anchor, and adding one would just move the gap rather than close it.
   Measured impact of this decision belongs in the evaluation harness's
   results, not hidden here.

3. Spanish name dictionary (-> REDACT canonical PERSON), via
   scan_spanish_names(): MEDDOCAN's patient/clinician names
   (NOMBRE_SUJETO_ASISTENCIA / NOMBRE_PERSONAL_SANITARIO) are
   space-separated ("Gabriel", "Navarro Cabrera", "Rosa María Jiménez
   Rodríguez") -- the exact shape flattened_names.py's docstring says its
   own segmentation approach does NOT handle (it only catches
   whitespace-free compound tokens). Presidio NER is the layer that would
   normally catch space-separated names, and it is unavailable in this
   sandbox (network-blocked model download, documented project-wide). This
   module adds a THIRD, narrower approach instead of leaving PERSON
   unscored for MEDDOCAN entirely: a name-dictionary lookup (Faker's
   es_ES person provider, the same "vetted, licensed, public generator
   data" sourcing discipline flattened_names.py itself uses for en_US)
   over runs of capitalized words, requiring at least one word in the run
   to be a dictionary hit. KNOWN, MEASURED-CLASS LIMITATION (same shape as
   flattened_names.py's own documented ones): Faker's es_ES list is ~952
   first names / ~1,085 surnames -- a real, finite dictionary, not
   exhaustive coverage of Spanish/Latin American naming diversity. Expect
   a real, non-trivial miss rate on any name outside that list, and expect
   some false positives on capitalized non-name words that happen to
   collide with a dictionary entry (e.g. common Spanish surnames that are
   also common words) -- both should be measured and reported by the
   evaluation harness, not assumed away here.
"""
import re

from faker.providers.person.es_ES import Provider as _EsESPersonProvider

FIRST_NAMES_ES = {n.lower() for n in _EsESPersonProvider.first_names}
LAST_NAMES_ES = {n.lower() for n in _EsESPersonProvider.last_names}

# --------------------------------------------------------------------------
# NHC / CIPA (patient ID) -> MRN
# --------------------------------------------------------------------------
# Label, optional separator, optional "nhc-"/"nhc/" infix (seen when the
# label is CIPA but the value itself repeats "nhc-" before the digits),
# then 5-10 digits, optionally followed by a space- or slash-separated
# 1-2 digit suffix (e.g. "9764132 3", "8576744/322").
NHC_ES = re.compile(
    r"\b(?:NHC|CIPA)\s*[:.]?\s*(?:nhc[-/])?\d{5,10}(?:[ /]\d{1,3})?\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# NASS (Social Security affiliation number) -> SSN
# --------------------------------------------------------------------------
# 3-group shape only (2 digits / 6-9 digits / 2 digits), space- or
# hyphen-separated. See module docstring for why the 2-group variant is
# deliberately not matched.
NASS_ES = re.compile(r"\b\d{2}[- ]\d{6,9}[- ]\d{2}\b")


def scan_es_regex(text: str) -> list[dict]:
    """Regex layer: NHC_ES -> MRN, NASS_ES -> SSN."""
    hits = []
    for m in NHC_ES.finditer(text):
        hits.append({"type": "MRN", "start": m.start(), "end": m.end(), "method": "es_regex_nhc"})
    for m in NASS_ES.finditer(text):
        hits.append({"type": "SSN", "start": m.start(), "end": m.end(), "method": "es_regex_nass"})
    return hits


# --------------------------------------------------------------------------
# Spanish name dictionary -> PERSON (space-separated names)
# --------------------------------------------------------------------------
_CAPITALIZED_WORD_RE = re.compile(r"[A-ZÁÉÍÓÚÑÜ][a-zA-ZáéíóúñüÁÉÍÓÚÑÜ]+")
# A run of 1-4 consecutive capitalized words (allowing single-space gaps
# only, so it doesn't bridge across a line break or punctuation).
_CAP_RUN_RE = re.compile(
    r"[A-ZÁÉÍÓÚÑÜ][a-zA-ZáéíóúñüÁÉÍÓÚÑÜ]+(?: [A-ZÁÉÍÓÚÑÜ][a-zA-ZáéíóúñüÁÉÍÓÚÑÜ]+){0,3}"
)
MIN_WORD_LEN = 3


def scan_spanish_names(text: str) -> list[dict]:
    """PERSON hits for space-separated Spanish name runs, via dictionary
    lookup rather than NER. See module docstring for the disclosed
    dictionary-coverage and false-positive-rate limitations."""
    hits = []
    for run_m in _CAP_RUN_RE.finditer(text):
        run = run_m.group(0)
        words = run.split(" ")
        if not any(
            len(w) >= MIN_WORD_LEN and w.lower() in (FIRST_NAMES_ES | LAST_NAMES_ES)
            for w in words
        ):
            continue
        hits.append({
            "type": "PERSON",
            "start": run_m.start(),
            "end": run_m.end(),
            "method": "es_name_dict",
        })
    return hits


def scan_es(text: str) -> list[dict]:
    """Combined Spanish-aware layer: regex (NHC/NASS) + name dictionary.
    Same hit-dict shape as detect.py's scan_regex()/scan_ner(), so results
    from this module can be concatenated directly with detect.py's output
    by a harness that wants the union of both."""
    return scan_es_regex(text) + scan_spanish_names(text)
