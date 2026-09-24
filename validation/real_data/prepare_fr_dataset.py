"""
Documents the provenance of validation/real_data/datasets/OpenPII_FR_raw.jsonl
and re-validates its structure. Mirrors prepare_meddocan_dataset.py's role
(a provenance record plus a rerunnable integrity check, NOT a downloader --
see "WHY NO DOWNLOADER" below) -- but the underlying dataset, and therefore
the disclosures that matter, are different in kind from MEDDOCAN's, not just
in language. Read this whole docstring before trusting any number derived
from this file.

WHAT THIS DATASET IS, AND WHY IT WAS ADDED
-------------------------------------------
LANGUAGE_EXTENSION.md flagged three French clinical-NLP benchmarks as
candidates needing license verification: CAS, ESSAI, QUAERO. All three
were investigated and REJECTED -- not on license grounds (QUAERO is
GFDL-licensed, CAS's paper is CC BY 4.0; license was resolvable) but on a
more fundamental one: none of them annotate PII/de-identification spans.
Their NER label sets (confirmed directly against DrBenchmark/QUAERO's
published schema: LIVB, PROC, ANAT, DEVI, CHEM, GEOG, PHYS, PHEN, DISO,
OBJC) are UMLS clinical-concept categories -- living beings, procedures,
anatomy, devices, chemicals -- not PERSON/EMAIL/ID-number spans. A
benchmark can be well-licensed and well-documented and still be unusable
for this project's purpose if it was never annotated for the right kind
of span. This is a real, worth-recording finding in its own right, not
just a stepping stone to the dataset actually used.

The dataset actually staged here is `ai4privacy/open-pii-masking-500k-ai4privacy`
(Hugging Face), the smaller sibling of ai4privacy's flagship
`pii-masking-openpii-1.5m`. CC BY 4.0, `gated: false`, `private: false`
(verified directly against the HF dataset API, not taken from a search
summary). It is a genuinely multilingual PII-masking corpus -- English,
French, German, Italian, Spanish, Hindi, Telugu, and more -- with a
`privacy_mask` field giving exact character-offset spans and a fine-grained
label taxonomy (GIVENNAME, SURNAME, EMAIL, TELEPHONENUM, CREDITCARDNUMBER,
SOCIALNUM, IDCARDNUM, DATE, TIME, AGE, SEX/GENDER, CITY, ZIPCODE, STREET,
BUILDINGNUM, TITLE, DRIVERLICENSENUM, PASSPORTNUM, and a template artifact
label, LANGUAGEPLACEHOLDER, that is not a real PII type and is excluded
outright -- see FR_TYPE_MAPPING.md).

A REAL, DISCLOSED DIFFERENCE FROM MEDDOCAN: SYNTHETIC, NOT NATURAL TEXT
-------------------------------------------------------------------------
MEDDOCAN's carrier text is real, human-authored clinical narrative (SciELO
case reports) with real PHI expert-annotated onto it. ai4privacy's OpenPII
family is template/LLM-generated synthetic text with synthetic PII values
substituted in -- sentences like "Ajljin : 'Je suis tres excite de
commencer ce projet...'" or "Dafni Votime Merucci Gassmann Revertera a
cree un modele de costume...", clearly synthetic in register and content,
not excerpts of real French documents. This is stated plainly, not glossed
over: results from this dataset measure REDACT's French detection layer
against realistic-FORMAT, synthetic-CONTENT PII in short single-sentence
contexts -- a meaningfully different and easier evaluation setting than
MEDDOCAN's full real clinical narratives, and any precision/recall numbers
drawn from it should be read with that caveat attached, not presented
alongside MEDDOCAN's numbers as if directly comparable.

WHY NO DOWNLOADER SCRIPT
-------------------------
Same sandbox network restriction documented in prepare_meddocan_dataset.py:
direct `curl`/bash access to huggingface.co and datasets-server.huggingface.co
returns HTTP 403 from this project's outbound proxy (confirmed live). Data
here was fetched through the same public row API
(`https://datasets-server.huggingface.co/rows?dataset=ai4privacy%2F
open-pii-masking-500k-ai4privacy&config=default&split=validation&
offset=<n>&length=<n>`), reached via this project's `web_fetch` tool (which
has a different network path than the sandbox shell), paged in batches of
15-40 rows and manually filtered for `"language":"fr"`. If you have real
internet access, the more reliable option is:
    pip install datasets
    python3 -c "from datasets import load_dataset; \\
        load_dataset('ai4privacy/open-pii-masking-500k-ai4privacy')"
and filter for language == 'fr' directly.

COVERAGE -- DISCLOSED, NOT HIDDEN
-----------------------------------
This is a SMALL PILOT SAMPLE, not a systematic corpus. 11 French-language
rows, hand-verified (every gold span's character offsets checked against
the actual staged text -- see check_coverage()'s validation pass), drawn
from the dataset's `validation` split, `row_idx` 0-114 (roughly 116,000
rows total in that split alone; French appears to be a well-represented
but minority language, observed at roughly 10-15% density in the rows
paged through). This sample is comparable in scale to, or smaller than,
this project's own windows_event real-data condition (n=33), which this
project has already precedented as small-but-usable-for-a-directional-read
rather than a confident final number. Treat this evaluation the same way:
useful to check the pipeline is wired correctly and to get a first-order
sense of precision/recall, not as a publication-grade sample size. Anyone
resuming this: keep paging `datasets-server.huggingface.co/rows?...&
split=validation&offset=<n>&length=15` (length=15-40 stays within this
project's `web_fetch` tool's inline response-size limit; larger requests
overflow to a tool-results file reachable by Read/Grep but not by this
project's bash sandbox -- see BUGS_AND_FIXES.md-style notes elsewhere in
this validation set for that filesystem-namespace split) past offset=115,
filtering for `"language":"fr"`, to grow this sample.

FILE FORMAT
-----------
datasets/OpenPII_FR_raw.jsonl -- one JSON object per line, same shape as
MEDDOCAN_raw.jsonl so the two are handled uniformly by the same class of
evaluation code:
    {"split": "validation",
     "id": "openpii-fr-<uid>",
     "text": "<synthetic French sentence>",
     "ents": [{"t": "<ai4privacy privacy_mask label, e.g. GIVENNAME>",
               "s": "<surface text>",
               "o": [start_offset, end_offset]}, ...]}
"ents" offsets are character offsets into "text", copied verbatim from the
dataset's own `privacy_mask` spans -- nothing recomputed or re-tokenized.
"""
import json
import os
from collections import Counter

OUT_PATH = os.path.join("datasets", "OpenPII_FR_raw.jsonl")


def load_rows(path=OUT_PATH):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def check_coverage(rows):
    """Reports what's staged, and re-validates every gold span's offsets
    against the actual text (unlike prepare_meddocan_dataset.py's version,
    this also re-checks span integrity, since this file was assembled by
    hand from paged API responses rather than a single bulk export)."""
    print(f"OpenPII_FR_raw.jsonl coverage: {len(rows)} documents "
          f"(SMALL PILOT SAMPLE -- see module docstring's 'COVERAGE' "
          f"section; not a systematic or complete corpus).")

    ent_types = Counter()
    total_ents = 0
    bad = 0
    for r in rows:
        for e in r["ents"]:
            ent_types[e["t"]] += 1
            total_ents += 1
            start, end = e["o"]
            if r["text"][start:end] != e["s"]:
                bad += 1
                print(f"  OFFSET MISMATCH in {r['id']}: {e}")

    print(f"\n{total_ents} entity spans across {len(ent_types)} entity types "
          f"({bad} offset mismatches found).")
    for t, c in ent_types.most_common():
        print(f"  {t}: {c}")


def main():
    if not os.path.exists(OUT_PATH):
        raise SystemExit(
            f"{OUT_PATH} not found. This script does not download the data "
            f"itself -- see the module docstring's 'WHY NO DOWNLOADER "
            f"SCRIPT' section for how it was produced and how to grow it."
        )
    rows = load_rows()
    check_coverage(rows)
    print(
        "\nNext step: see FR_TYPE_MAPPING.md for the ai4privacy-label -> "
        "REDACT-canonical-type mapping, then src/fr_detect.py and "
        "src/fr_ner.py for the detection layers, then evaluate_fr.py for "
        "the scoring harness (mirrors evaluate_meddocan.py's structure)."
    )


if __name__ == "__main__":
    main()
