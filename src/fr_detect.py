"""
Layer (opt-in): French-language detection patterns, added specifically to
evaluate REDACT against the real-format/synthetic-content OpenPII French
sample (validation/real_data/datasets/OpenPII_FR_raw.jsonl -- see
validation/real_data/FR_TYPE_MAPPING.md for the entity-type mapping this
module targets).

DELIBERATELY SEPARATE FROM detect.py, NOT A MODIFICATION OF IT. Same
discipline as es_detect.py: never touch an existing, already-measured
pattern to fix or extend a different case -- add a new, narrow, additive
path instead. detect.py's REGEX_PATTERNS, scan_regex(), scan_ner(), and
flattened_names.py's English name dictionary are UNCHANGED by this file.
Nothing in detect_all()/detect_all_field_gated()/detect_all_schema_aware()
calls into this module automatically -- it is called explicitly, only by
evaluate_fr.py, so existing English/US-corpus recall and precision numbers
cannot regress by this file's mere existence.

WHY THIS FILE IS SMALLER THAN es_detect.py -- A REAL FINDING, NOT AN
OVERSIGHT: es_detect.py added two NEW regex patterns (NHC_ES -> MRN,
NASS_ES -> SSN) because MEDDOCAN's Spanish clinical text carries
Spanish-specific ID-number formats REDACT's existing English/US regex
doesn't match. The OpenPII French pilot sample has no such analogue --
see FR_TYPE_MAPPING.md's "labels present in ai4privacy's broader taxonomy
but NOT observed in this pilot sample" section: no SOCIALNUM (French
INSEE/NIR number) or IDCARDNUM examples were observed in the 11 documents
staged so far, so there is no real evidence yet to build an SSN- or
MRN-equivalent French pattern against. EMAIL and CREDIT_CARD in this
sample are both language-agnostic formats REDACT's existing detect.py
regex already catches with zero modification (confirmed in
FR_TYPE_MAPPING.md's mapped-types table). That leaves exactly ONE new
layer actually justified by the staged data: a French name dictionary for
PERSON, the same gap es_detect.py hit for Spanish.

FRENCH NAME DICTIONARY (-> REDACT canonical PERSON), via
scan_french_names(): OpenPII's GIVENNAME/SURNAME spans are space-separated
("Naphtali", "Mártin de Tavernier Bronchales", "Evica", "Shalaku
Tepedino") -- the exact shape flattened_names.py's own docstring says its
concatenated-token segmentation does NOT handle. Presidio NER (fr_ner.py)
is the layer that would normally catch space-separated names, but per
this project's documented network-blocked-model-download limitation
(same as es_ner.py), it can only run inside Docker. This module adds the
same THIRD, narrower approach es_detect.py used for Spanish instead of
leaving PERSON unscored for French entirely: a name-dictionary lookup
(Faker's fr_FR person provider, the same "vetted, licensed, public
generator data" sourcing discipline flattened_names.py and es_detect.py
both use) over runs of capitalized words, requiring at least one word in
the run to be a dictionary hit.

KNOWN, MEASURED-CLASS LIMITATION (same shape as es_detect.py's own
documented one): Faker's fr_FR list is a finite, real dictionary, not
exhaustive coverage of French naming diversity -- expect a real,
non-trivial miss rate on any name outside that list, and expect some
false positives on capitalized non-name words that happen to collide
with a dictionary entry. es_detect.py's own real, measured finding
(combining the Spanish dictionary layer with NER dropped PERSON
precision from 47.4% to 39.2% on MEDDOCAN, because the two layers' false
positives were ~97% disjoint rather than overlapping) is exactly the
kind of result evaluate_fr.py's --diagnose flag exists to re-check for
French, not assume will recur identically.
"""
import re

from faker.providers.person.fr_FR import Provider as _FrFRPersonProvider

FIRST_NAMES_FR = {n.lower() for n in _FrFRPersonProvider.first_names}
LAST_NAMES_FR = {n.lower() for n in _FrFRPersonProvider.last_names}

# --------------------------------------------------------------------------
# French name dictionary -> PERSON (space-separated names)
# --------------------------------------------------------------------------
# Same capitalized-word-run approach as es_detect.py's _CAP_RUN_RE, with
# French accented characters (é, è, ê, ë, à, â, ù, û, ü, ï, î, ô, ç)
# included in both the leading-letter and continuation classes so accented
# given/surnames (e.g. "Löhningen", "Mártin", "Théodoros") aren't silently
# truncated at the accent.
_CAP_RUN_RE = re.compile(
    r"[A-ZÀÂÄÉÈÊËÎÏÔÙÛÜÇÖ][a-zà-öù-ÿ]+(?: [A-ZÀÂÄÉÈÊËÎÏÔÙÛÜÇÖ][a-zà-öù-ÿ]+){0,3}"
)
MIN_WORD_LEN = 3


def scan_french_names(text: str) -> list[dict]:
    """PERSON hits for space-separated French name runs, via dictionary
    lookup rather than NER. See module docstring for the disclosed
    dictionary-coverage and false-positive-rate limitations."""
    hits = []
    for run_m in _CAP_RUN_RE.finditer(text):
        run = run_m.group(0)
        words = run.split(" ")
        if not any(
            len(w) >= MIN_WORD_LEN and w.lower() in (FIRST_NAMES_FR | LAST_NAMES_FR)
            for w in words
        ):
            continue
        hits.append({
            "type": "PERSON",
            "start": run_m.start(),
            "end": run_m.end(),
            "method": "fr_name_dict",
        })
    return hits


def scan_fr(text: str) -> list[dict]:
    """French-aware layer: currently just the name dictionary -- see module
    docstring for why no new regex patterns are added in this pass (no
    French-specific ID-number format has been observed yet in the staged
    sample; EMAIL/CREDIT_CARD already fall under detect.py's existing
    language-agnostic regex). Same hit-dict shape as detect.py's
    scan_regex()/scan_ner() and es_detect.py's scan_es(), so results
    concatenate directly with a harness that wants the union."""
    return scan_french_names(text)
