"""
Layer (opt-in): Indonesian-language detection patterns, added for the
(currently blocked, see validation/real_data/prepare_id_dataset.py)
Indonesian PII pilot sample. Built and ready to run the moment real data
is staged, same "build the pipeline before the data arrives" discipline
this project already applies to Docker-only NER layers.

DELIBERATELY SEPARATE FROM detect.py, NOT A MODIFICATION OF IT. Same
discipline as es_detect.py/fr_detect.py/ru_detect.py: never touch an
existing, already-measured pattern -- add a new, narrow, additive path
instead. Nothing in detect_all()/detect_all_field_gated()/
detect_all_schema_aware() calls into this module automatically.

WHY THIS MODULE HAS NO NEW REGEX PATTERNS YET: RU_TYPE_MAPPING.md and
FR_TYPE_MAPPING.md's SSN/MRN regex patterns (SNILS, OMS, NHC_ES, NASS_ES)
were all built FROM real observed digit-string formats in real staged
data. ID_TYPE_MAPPING.md's proposed SSN mapping (via SOCIALNUM) has NOT
been verified against a single real Indonesian example (see that
document and prepare_id_dataset.py for why) -- writing a regex pattern
against a format nobody has actually seen in Indonesian text would be
guessing, not engineering, and this project's own discipline (see
es_detect.py's docstring: "measured impact... belongs in the evaluation
harness's results, not hidden here") is to build patterns from evidence,
not assumption. Once real Indonesian SOCIALNUM/IDCARDNUM examples are
staged, add the corresponding regex here following the same
label-anchored design as ru_detect.py's SNILS_RU/OMS_RU.

INDONESIAN NAME DICTIONARY (-> REDACT canonical PERSON), via
scan_indonesian_names(): same space-separated-name gap as every other
language pass so far -- Faker's `id_ID` person provider is used (716
first names / 175 last names, the richest Faker locale of any language
built in this project so far, per LANGUAGE_EXTENSION.md's own research).
Indonesian has no case-inflection problem the way Russian does (Bahasa
Indonesia is not a grammatically case-inflected language), so this
mirrors the Spanish/French exact-string `_CAP_RUN_RE` approach directly
rather than needing Russian's lemmatization workaround -- confirmed by
LANGUAGE_EXTENSION.md's own research pass ("None -- real whitespace word
boundaries" under Indonesian's script/tokenization complication column).
"""
import re

from faker.providers.person.id_ID import Provider as _IdIDPersonProvider

FIRST_NAMES_ID = {n.lower() for n in _IdIDPersonProvider.first_names}
LAST_NAMES_ID = {n.lower() for n in _IdIDPersonProvider.last_names}

_CAP_RUN_RE = re.compile(
    r"[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,3}"
)
MIN_WORD_LEN = 3


def scan_indonesian_names(text: str) -> list[dict]:
    """PERSON hits for space-separated Indonesian name runs, via exact-
    string dictionary lookup (no lemmatization needed -- Indonesian is not
    case-inflected, see module docstring)."""
    hits = []
    name_dict = FIRST_NAMES_ID | LAST_NAMES_ID
    for run_m in _CAP_RUN_RE.finditer(text):
        run = run_m.group(0)
        words = run.split(" ")
        if not any(
            len(w) >= MIN_WORD_LEN and w.lower() in name_dict
            for w in words
        ):
            continue
        hits.append({
            "type": "PERSON",
            "start": run_m.start(),
            "end": run_m.end(),
            "method": "id_name_dict",
        })
    return hits


def scan_id(text: str) -> list[dict]:
    """Indonesian-aware layer: currently just the name dictionary -- see
    module docstring for why no regex patterns are added yet (no real
    Indonesian ID-number example has been observed at all, see
    ID_TYPE_MAPPING.md). Same hit-dict shape as detect.py's scan_regex()/
    scan_ner() and the other language layers' scan_{lang}(), so results
    concatenate directly with a harness that wants the union."""
    return scan_indonesian_names(text)
