"""
Documents the provenance of (and current blocker on) an Indonesian PII
pilot sample for `validation/real_data/datasets/OpenPII_ID_raw.jsonl`.

UNLIKE prepare_fr_dataset.py / prepare_ru_dataset.py / prepare_meddocan_
dataset.py, THAT FILE DOES NOT EXIST YET. This is not a silent gap --
read on for exactly why, and exactly what unblocks it.

WHAT WAS FOUND (license/benchmark research, done BEFORE any staging
attempt, per this project's own checklist discipline)
-------------------------------------------------------------------------
LANGUAGE_EXTENSION.md's original research found no Indonesian PII/
de-identification benchmark. A fresh literature search this pass
confirmed that finding still holds for dedicated Indonesian NER corpora:
IndoNLU/IndoLEM (general NER: PERSON/ORGANISATION/PLACE, MIT-licensed but
not PII-de-identification-annotated), IndQNER (Quran-translation named
entities, religious domain, 18 entity classes, none PII-shaped), IndoLER
(legal-document entity recognition), and NERSkill.Id (skill-entity
recognition) are all real, license-clear, but the SAME wrong-kind-of-
annotation problem this project's French and Russian passes both hit and
resolved by finding a different dataset entirely (see
`prepare_fr_dataset.py`'s CAS/ESSAI/QUAERO finding and
`prepare_ru_dataset.py`'s RuMedNER/RuDReC finding) -- these Indonesian
corpora label the wrong KIND of span, not just the wrong domain.

The one dataset that DOES have the right kind of annotation and DOES
include Indonesian is confirmed: `ai4privacy/pii-masking-openpii-1.5m`
(Hugging Face) -- the SAME dataset family used for French (see
`prepare_fr_dataset.py`), just the larger flagship rather than the
smaller `open-pii-masking-500k-ai4privacy` sibling (which does NOT
include Indonesian -- confirmed via its language-code list, which is why
French used the smaller dataset and Indonesian cannot). CC BY 4.0,
`gated: false`, `private: false` (verified via the HF dataset API).
`language: id` is present in this flagship dataset's coverage.

THE ACTUAL BLOCKER: A CONFIRMED, REPRODUCIBLE DATA-ACCESS RELIABILITY
PROBLEM, NOT A LICENSE OR ANNOTATION PROBLEM
-------------------------------------------------------------------------
This is a genuinely different kind of blocker than French/Russian hit --
worth stating plainly rather than glossing over. Hugging Face's public
`datasets-server` row API (the same API and same paginated `web_fetch`
technique that successfully staged both MEDDOCAN and OpenPII French)
returns EMPTY response bodies for this specific 1.5M-row/4.6GB dataset,
even for its `train` split, at every offset tried except (unreliably)
`offset=0` with a small `length`. Reproduced directly, this session:
  - `split=validation`: every request tried (offset 0, length 1/10)
    returned empty.
  - `split=train`, `offset=0, length=5`: SUCCEEDED once (5 real rows
    returned, languages ko/vi/ja/ja/ja -- no Indonesian in that
    particular page).
  - `split=train`, same exact query re-run ~60 seconds later: returned
    empty.
  - `split=train`, offset=5/10/1000/50000, length=5/10/40: ALL returned
    empty, tried multiple times each.
This matches a limitation already documented elsewhere in this project's
own history for this exact dataset (see the note in an earlier session
about the flagship dataset's row-viewer not being reliably indexed,
unlike its smaller sibling) -- not a new problem, but now re-confirmed
directly rather than assumed to still hold.

Per this project's own checklist ("if nothing license-clear turns up for
a language, document that finding and move to the next language rather
than stalling"): the spirit of that rule is followed here even though the
literal trigger (no license-clear candidate) did not occur -- a
license-clear, correctly-annotated candidate WAS found, but is not
PRACTICALLY STAGEABLE from this sandbox. Rather than burn further budget
retrying an API already confirmed unreliable across ~10 attempts, or
fabricating placeholder data, this is disclosed as an honest, concrete
blocker with a specific unblock path below, and the REST of the
Indonesian pipeline (type mapping, detection layers, IndoBERT-NER wiring,
Docker harness, evaluation harness) is built and ready to run the moment
real data is staged -- see MEDDOCAN_VALIDATION_REVIEW_SUMMARY.md Section
9 for the full write-up of what IS done versus what is blocked.

HOW TO UNBLOCK THIS (concrete, not hand-waved)
-------------------------------------------------------------------------
Anyone with real, unrestricted internet access (this project's sandbox
blocks huggingface.co directly at the network level -- see every other
`prepare_*_dataset.py` in this directory for the same documented
restriction) can stage this in minutes:
    pip install datasets
    python3 -c "
from datasets import load_dataset
ds = load_dataset('ai4privacy/pii-masking-openpii-1.5m', split='train')
id_rows = ds.filter(lambda r: r['language'] == 'id')
id_rows.to_json('validation/real_data/datasets/OpenPII_ID_raw_source.jsonl')
"
Then convert each row's `privacy_mask` field (same shape documented in
`prepare_fr_dataset.py`: `[{"label":..., "start":..., "end":..., "value":...}]`)
into this project's `{"split", "id", "text", "ents":[{"t","s","o"}]}`
shape (see `prepare_fr_dataset.py`'s FILE FORMAT section -- identical
schema, this dataset family is consistent across its French/Indonesian/
other-language rows) and save as
`validation/real_data/datasets/OpenPII_ID_raw.jsonl`. Once that file
exists, `evaluate_id.py` (already built, see below) runs immediately with
no other changes needed.

FILE FORMAT (once staged -- identical to prepare_fr_dataset.py's, since
this is the same source dataset)
-------------------------------------------------------------------------
datasets/OpenPII_ID_raw.jsonl -- one JSON object per line:
    {"split": "train"|"validation",
     "id": "openpii-id-<uid>",
     "text": "<Indonesian sentence>",
     "ents": [{"t": "<ai4privacy privacy_mask label, e.g. GIVENNAME>",
               "s": "<surface text>",
               "o": [start_offset, end_offset]}, ...]}
"""
import json
import os
from collections import Counter

OUT_PATH = os.path.join("datasets", "OpenPII_ID_raw.jsonl")


def load_rows(path=OUT_PATH):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def check_coverage(rows):
    print(f"OpenPII_ID_raw.jsonl coverage: {len(rows)} documents.")
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
            f"{OUT_PATH} not found -- this is a KNOWN, DISCLOSED blocker, "
            f"not a bug. See this module's docstring's 'THE ACTUAL "
            f"BLOCKER' and 'HOW TO UNBLOCK THIS' sections: this project's "
            f"sandbox cannot reliably stage rows from "
            f"ai4privacy/pii-masking-openpii-1.5m's datasets-server API "
            f"(confirmed empty across ~10 attempts across both splits and "
            f"multiple offsets), but a license-clear, correctly-annotated "
            f"Indonesian source WAS identified -- staging it just needs "
            f"real (non-sandboxed) internet access and the `datasets` "
            f"library, per the exact commands in this docstring."
        )
    rows = load_rows()
    check_coverage(rows)


if __name__ == "__main__":
    main()
