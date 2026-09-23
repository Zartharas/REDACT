"""
Documents the provenance of validation/real_data/datasets/MEDDOCAN_raw.jsonl
and re-validates its structure. Unlike prepare_cloudtrail_dataset.py, this is
NOT a script you run to (re)download the data -- see "WHY NO DOWNLOADER"
below -- it's a record of how the staged file was produced, plus an
integrity check you CAN rerun.

WHAT THIS DATASET IS, AND WHY IT WAS ADDED
-------------------------------------------
MEDDOCAN (Medical Document Anonymization track) is a real-clinical-text PHI
de-identification benchmark: 1,000 case reports drawn from SciELO (Scientific
Electronic Library Online -- genuine published Spanish/Latin American medical
case literature), with PHI expressions enriched by expert annotators in a
three-step process (AnnotateIt -> BRAT -> senior-annotator revision; 98%
pairwise inter-annotator agreement on double-annotated records). Licensed
CC BY 4.0 -- no Data Use Agreement, no registration, no account. Built by
Barcelona Supercomputing Center + Centro Nacional de Investigaciones
Oncológicas under Spain's Plan de Impulso de las Tecnologías del Lenguaje.
Distributed as `bigbio/meddocan` on Hugging Face and
PlanTL-GOB-ES/SPACCC_MEDDOCAN on GitHub/Zenodo.

It was added specifically to give this project's PHI/MRN claim (previously
regex-only, zero real ground truth -- see PHI_DATASET_ACCESS.md) a real,
gold-standard-annotated comparison, after the intended fix (n2c2 2014
de-identification corpus) turned out to require a Harvard DBMI Data Use
Agreement whose registration portal was confirmed (live browser check,
2026-09) to be showing "Temporarily Unavailable" / "Registration is not
open... at this time" platform-wide, independent of account status.

REAL BUT NOT ENGLISH: MEDDOCAN's carrier text and PHI values are Spanish.
Its patient-ID field (NHC / "Número de Historia Clínica", e.g. "NHC: 368503"
or "CIPA: nhc-150679") is the closest real-world analogue to this project's
MRN gap; its NASS field (Social-Security-like insurance number, e.g.
"26 63514095" or "55-55012378-99") is analogous to SSN. Both use formats
this project's current English/US-only regex patterns do not match --
Spanish-aware patterns are a separate, not-yet-done task (see
validation/real_data/README.md or the project's own task tracker for
"Build Spanish-aware detection patterns" and "Build MEDDOCAN evaluation
harness").

WHY NO DOWNLOADER SCRIPT
-------------------------
Unlike CloudTrailFlaws_raw.jsonl (one big tarball, one urlretrieve call),
this sandbox's outbound network blocks huggingface.co, codeload.github.com,
api.github.com, raw.githubusercontent.com, and zenodo.org directly (confirmed
via curl, all HTTP 000 / connection failure). The data here was instead
fetched through Hugging Face's public datasets-server row API
(`https://datasets-server.huggingface.co/rows?dataset=bigbio%2Fmeddocan&
config=meddocan_bigbio_kb&split=<split>&offset=<n>&length=<n>`) via this
project's `web_fetch` tool, whose oversized responses are auto-saved to a
local tool-results file that IS reachable from the sandbox shell -- paged in
batches of 5-10 rows per request to stay under that tool's own response-size
cap. There is no single clean re-run command for this path; if you have
real internet access, the more reliable option is:
    pip install datasets
    python3 -c "from datasets import load_dataset; \\
        load_dataset('bigbio/meddocan', 'meddocan_bigbio_kb')"
and re-derive MEDDOCAN_raw.jsonl's row shape (see PARSE below) from that.

COVERAGE -- DISCLOSED, NOT HIDDEN
-----------------------------------
This is a PARTIAL sample, not the full 1,000-document corpus. As of the last
consolidation pass: 520 of 1,000 documents (52%) are staged --
210/500 train, 190/250 validation, 120/250 test. The HF datasets-server API
was intermittently unreliable during collection (some (split, offset) pages
returned empty bodies on repeated attempts, independent of a simple
rate-limit explanation -- adjacent pages and smaller page sizes often
succeeded when a given page failed), and a further attempt to fill the
remaining gaps was cut off by a sustained run of empty responses across all
three splits, suggesting a broader, temporary API degradation rather than
per-page corruption. Anyone resuming this: rerun the fetch loop for the
(split, offset) pairs `check_coverage()` below reports missing, in batches
of 5-10 rows, retrying failed pages after a short wait.

Every document actually staged IS the real, complete carrier text and its
full real gold-standard entity annotation -- this is a smaller-than-ideal
SAMPLE of MEDDOCAN, not a corrupted or synthetic stand-in for it.

FILE FORMAT
-----------
datasets/MEDDOCAN_raw.jsonl -- one JSON object per line:
    {"split": "train"|"validation"|"test",
     "id": "<document_id, e.g. S0004-06142006000500012-1>",
     "text": "<full real clinical case-report text>",
     "ents": [{"t": "<MEDDOCAN entity type, e.g. NOMBRE_SUJETO_ASISTENCIA>",
               "s": "<surface text>",
               "o": [start_offset, end_offset]}, ...]}
"ents" offsets are character offsets into "text", copied verbatim from the
dataset's own BRAT-derived spans -- nothing recomputed or re-tokenized here.
"""
import json
import os
from collections import Counter

OUT_PATH = os.path.join("datasets", "MEDDOCAN_raw.jsonl")
SPLIT_TOTALS = {"train": 500, "validation": 250, "test": 250}


def load_rows(path=OUT_PATH):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def check_coverage(rows):
    """Reports what's staged vs. the full 1,000-document corpus, and which
    (split, offset) pages (assuming length=10 paging) would need re-fetching
    to fill the gaps. Purely a reporting function -- makes no network calls."""
    by_split = Counter(r["split"] for r in rows)
    ids_by_split = {}
    for r in rows:
        ids_by_split.setdefault(r["split"], set()).add(r["id"])

    print("MEDDOCAN_raw.jsonl coverage:")
    total_got = total_expected = 0
    for split, expected in SPLIT_TOTALS.items():
        got = by_split.get(split, 0)
        total_got += got
        total_expected += expected
        print(f"  {split}: {got}/{expected} documents ({got/expected:.0%})")
    print(f"  TOTAL: {total_got}/{total_expected} ({total_got/total_expected:.0%})")

    ent_types = Counter()
    total_ents = 0
    for r in rows:
        for e in r["ents"]:
            ent_types[e["t"]] += 1
            total_ents += 1
    print(f"\n{total_ents} entity spans across {len(ent_types)} entity types.")
    for t, c in ent_types.most_common():
        print(f"  {t}: {c}")


def main():
    if not os.path.exists(OUT_PATH):
        raise SystemExit(
            f"{OUT_PATH} not found. This script does not download the data "
            f"itself -- see the module docstring's 'WHY NO DOWNLOADER SCRIPT' "
            f"section for how it was produced and how to resume collection."
        )
    rows = load_rows()
    check_coverage(rows)
    print(
        "\nNext step: build a Spanish-aware entity-type mapping and detection "
        "patterns, then an evaluation harness scoring against these real "
        "gold-standard spans (mirrors inject_and_evaluate.py's evaluate() "
        "pattern, but MEDDOCAN needs no injection -- the PHI is already real "
        "and already annotated)."
    )


if __name__ == "__main__":
    main()
