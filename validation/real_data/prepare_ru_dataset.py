"""
Documents the provenance of validation/real_data/datasets/OpenRU_PII_raw.jsonl
and re-validates its structure. Mirrors prepare_fr_dataset.py's role (a
provenance record plus a rerunnable integrity check, NOT a downloader).

WHAT THIS DATASET IS, AND WHY IT WAS ADDED
-------------------------------------------
LANGUAGE_EXTENSION.md's earlier research pass found no Russian PII/
de-identification benchmark at all -- RuMedNER/RuDReC (drug-reaction
entity recognition) are clinical-CONCEPT benchmarks, the same
wrong-kind-of-annotation problem French's CAS/ESSAI/QUAERO candidates
turned out to have. A fresh literature search for this pass (same
discipline as the one that originally found MEDDOCAN) surfaced
`redmadrobot-rnd/pii_benchmark` (Hugging Face): MIT license, `gated:
false`, `private: false` (verified directly against the HF dataset API).
21 entity types across four families -- person names (including the
patronymic REDACT's other language layers don't have to handle),
address hierarchy, contacts, and Russian identity-document numbers
(passport, SNILS, INN, OMS, driver's license, military ID, birth
certificate).

A GENUINELY DIFFERENT PROVENANCE MIX FROM BOTH MEDDOCAN AND OPENPII
FRENCH: per the dataset's own README, "the test set combines real,
manually annotated examples from production logs -- where all real
personal data has been replaced with synthetic equivalents -- along
with synthetic document-style texts and hand-filtered hard negatives."
This is neither MEDDOCAN's real-carrier/real-PII pairing nor OpenPII
French's fully-synthetic-template pairing -- it's real carrier CONTEXT
(actual production log/query sentences) with synthetic PII VALUES
substituted in, the same "real context, injected synthetic PII" pattern
this project's own `inject_and_evaluate.py` methodology already uses
elsewhere in this repo. Worth stating plainly: this makes the carrier
sentences realistic in a way OpenPII French's templated text is not,
while still not being real, unredacted PII the way MEDDOCAN's SciELO
case reports are.

WHY NO DOWNLOADER SCRIPT
-------------------------
Same sandbox network restriction documented in prepare_meddocan_dataset.py
and prepare_fr_dataset.py: direct `curl`/bash access to huggingface.co
and datasets-server.huggingface.co returns HTTP 403 from this project's
outbound proxy. Data here was fetched through the same public row API
(`https://datasets-server.huggingface.co/rows?dataset=redmadrobot-rnd%2F
pii_benchmark&config=default&split=test&offset=<n>&length=<n>`), reached
via `web_fetch`. If you have real internet access:
    pip install datasets
    python3 -c "from datasets import load_dataset; \\
        load_dataset('redmadrobot-rnd/pii_benchmark')"

A REAL WRINKLE THIS DATASET HAS THAT MEDDOCAN/OPENPII FRENCH DID NOT:
no character offsets. Unlike MEDDOCAN's BRAT-derived `[start, end]` spans
and OpenPII's `privacy_mask` field, this dataset ships token-level BIO
tags (`tokens` + `ner_tags` columns) with no character offsets at all.
Character spans in `OpenRU_PII_raw.jsonl` were RECONSTRUCTED, not copied
verbatim: each token was located in `text` via sequential left-to-right
substring search starting from the previous token's end (so a token
string that happens to repeat earlier in the sentence is not
mis-anchored to the wrong occurrence), then adjacent same-type B-/I- tags
were merged into one span. Every reconstructed span was verified by
re-slicing `text[start:end]` and checking it equals the token
concatenation before being kept -- zero mismatches in the 26 rows staged
here (see check_coverage() below, which re-runs this same check any time
this file is loaded, not just at staging time).

COVERAGE -- DISCLOSED, NOT HIDDEN
-----------------------------------
26 rows, hand-selected from the dataset's `test` split (row_idx 0-89 of
2,841 total rows) for coverage of REDACT-mappable entity types (person
names, EMAIL, CREDIT_CARD, SNILS, OMS, IP_ADDRESS including IPv6) rather
than drawn as an unbiased random sample -- this is a curated pilot, not
a random probability sample, and should be described that way. Smaller
than MEDDOCAN (520 docs) but with a similar per-row entity density to
OpenPII French, and notably richer in genuinely Russian-specific
findings (patronymics, heavy inflection, mixed Cyrillic/Latin scripts,
IPv6 addresses) than either prior language pass surfaced. Anyone
resuming this: keep paging `datasets-server.huggingface.co/rows?...&
split=test&offset=<n>&length=15-40`, filtering for rows containing
FIRST_NAME/LAST_NAME/MIDDLE_NAME/EMAIL/CREDIT_CARD/SNILS/OMS/IP_ADDRESS
tags, and running the same token-to-offset reconstruction (see
`validation/real_data/` session notes or re-derive from this docstring)
before appending to the staged file.

FILE FORMAT
-----------
datasets/OpenRU_PII_raw.jsonl -- one JSON object per line, same shape as
MEDDOCAN_raw.jsonl/OpenPII_FR_raw.jsonl:
    {"split": "test",
     "id": "ru-pii-<row_idx>",
     "text": "<carrier sentence, real-log-derived or synthetic-document>",
     "ents": [{"t": "<pii_benchmark label, e.g. FIRST_NAME>",
               "s": "<surface text>",
               "o": [start_offset, end_offset]}, ...]}
"ents" offsets are RECONSTRUCTED character offsets (see "A REAL WRINKLE"
above), not copied from the source dataset, which has none.
"""
import json
import os
from collections import Counter

OUT_PATH = os.path.join("datasets", "OpenRU_PII_raw.jsonl")


def load_rows(path=OUT_PATH):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def check_coverage(rows):
    """Reports what's staged, and re-validates every reconstructed span's
    offsets against the actual text -- more important here than for
    MEDDOCAN/OpenPII French, since these offsets were computed, not
    copied from the source."""
    print(f"OpenRU_PII_raw.jsonl coverage: {len(rows)} documents "
          f"(curated pilot sample, not a random probability sample -- "
          f"see module docstring's 'COVERAGE' section).")

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
        "\nNext step: see RU_TYPE_MAPPING.md for the pii_benchmark-label -> "
        "REDACT-canonical-type mapping, then src/ru_detect.py and "
        "src/ru_ner.py for the detection layers, then evaluate_ru.py for "
        "the scoring harness."
    )


if __name__ == "__main__":
    main()
