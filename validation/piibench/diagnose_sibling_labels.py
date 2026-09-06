"""
Follow-up diagnostic, 2026-09: after fixing evaluate_piibench.py to score
against the real `text` field (see that file's docstring for the full
story), PERSON's precision was still poor (24 TP / 161 FP on a 200-record
smoke test) and CREDIT_CARD showed 4 FPs against just 1 in-scope gold span.

Hypothesis, not yet confirmed: PIIBench's full 82-type taxonomy splits some
concepts REDACT treats as one type into several sibling labels --
PERSON/NAME/FIRST_NAME/LAST_NAME all name a specific individual;
CREDIT_CARD/CREDIT_CARD_NUMBER/CREDIT_DEBIT_CARD all name a payment card
number. evaluate_piibench.py's PIIBENCH_TO_REDACT only maps the single
canonical label (PERSON, CREDIT_CARD) -- if a real, correctly-detected name
or card number's gold label happens to be one of these excluded siblings
instead, it gets scored as a false positive purely because of the label
taxonomy mismatch, not because REDACT was wrong.

This script checks that hypothesis directly against the RAW BIO labels
(not the already-filtered PIIBENCH_TO_REDACT view), for every PERSON/
CREDIT_CARD prediction that evaluate_piibench.py would currently count as
a false positive. Report only -- doesn't change any mapping itself.

Run: python validation/piibench/diagnose_sibling_labels.py --test-file ~/pii-bench/data/test_5k.jsonl --max-records 200
"""
import argparse
import json
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect  # noqa: E402
from evaluate_piibench import build_span_regex, locate_gold_spans, PIIBENCH_TO_REDACT  # noqa: E402

PERSON_SIBLINGS = {"NAME", "FIRST_NAME", "LAST_NAME"}
CREDIT_CARD_SIBLINGS = {"CREDIT_CARD_NUMBER", "CREDIT_DEBIT_CARD"}


def raw_entities(tokens, labels):
    """Every BIO entity, regardless of PIIBENCH_TO_REDACT mapping -- the
    unfiltered view this diagnostic needs to see sibling labels at all."""
    entities = []
    cur_type = None
    cur_start = None
    for i, label in enumerate(labels):
        if label == "O":
            if cur_type is not None:
                entities.append((cur_type, cur_start, i - 1))
                cur_type = None
            continue
        tag, _, etype = label.partition("-")
        if tag == "B" or etype != cur_type:
            if cur_type is not None:
                entities.append((cur_type, cur_start, i - 1))
            cur_type = etype
            cur_start = i
    if cur_type is not None:
        entities.append((cur_type, cur_start, len(labels) - 1))
    return entities


def locate_raw_spans(tokens, labels, text, wanted_types):
    entities = [e for e in raw_entities(tokens, labels) if e[0] in wanted_types]
    spans = []
    cursor = 0
    for etype, tok_start, tok_end in entities:
        pattern = build_span_regex(tokens, tok_start, tok_end)
        m = pattern.search(text, cursor) if pattern else None
        if m is None and pattern is not None:
            m = pattern.search(text)
        if m is None:
            continue
        spans.append({"type": etype, "start": m.start(), "end": m.end()})
        cursor = m.end()
    return spans


def check(records, redact_type, siblings, label):
    total_fp = 0
    fp_matching_sibling = 0
    examples = []
    for rec in records:
        text = rec.get("text") or " ".join(rec["tokens"])
        gold, _ = locate_gold_spans(rec["tokens"], rec["labels"], text)
        gold_of_type = [g for g in gold if g["type"] == redact_type]
        sibling_spans = locate_raw_spans(rec["tokens"], rec["labels"], text, siblings)

        preds = [h for h in detect.detect_all(text) if h["type"] == redact_type]
        for p in preds:
            hits_gold = any(p["start"] < g["end"] and g["start"] < p["end"] for g in gold_of_type)
            if hits_gold:
                continue  # already a true positive, not a FP
            total_fp += 1
            hits_sibling = [s for s in sibling_spans if p["start"] < s["end"] and s["start"] < p["end"]]
            if hits_sibling:
                fp_matching_sibling += 1
                if len(examples) < 10:
                    examples.append((text[p["start"]:p["end"]], hits_sibling[0]["type"]))

    print(f"=== {label} ===")
    print(f"  Total false positives (current mapping): {total_fp}")
    print(f"  ...of which overlap an excluded sibling label ({sorted(siblings)}): {fp_matching_sibling}")
    for matched_text, sibling_type in examples:
        print(f"    matched_text={matched_text!r} gold_sibling_type={sibling_type}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-file", required=True)
    parser.add_argument("--max-records", type=int, default=200)
    args = parser.parse_args()

    records = []
    with open(args.test_file) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if args.max_records:
        records = records[: args.max_records]

    check(records, "PERSON", PERSON_SIBLINGS, "PERSON false positives vs. NAME/FIRST_NAME/LAST_NAME")
    check(records, "CREDIT_CARD", CREDIT_CARD_SIBLINGS,
          "CREDIT_CARD false positives vs. CREDIT_CARD_NUMBER/CREDIT_DEBIT_CARD")


if __name__ == "__main__":
    main()
