"""
Diagnostic for the full 5,000-record PIIBench run (2026-09): CREDIT_CARD
recall came back low (0.166, 136 of 163 gold spans missed). Working
hypothesis, not yet confirmed here: REDACT's Luhn-checksum filter
(src/detect.py's _luhn_valid(), "Engineering upgrade 15" in
BUGS_AND_FIXES.md -- added to fix a real false-positive collision against
Zookeeper's real-data validation condition) rejects any digit run failing
the check. Faker's own synthetic card numbers (this project's corpus) are
always Luhn-valid by construction; PIIBench's constituent datasets may not
generate Luhn-valid numbers at all, in which case this recall drop is a
disclosed, deliberate precision/recall tradeoff this project already made,
not a new bug -- but that needs to be SEEN against the real missed spans,
not assumed.

Run: python validation/piibench/diagnose_credit_card_recall.py --test-file ~/pii-bench/data/test_5k.jsonl
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect  # noqa: E402
from evaluate_piibench import locate_gold_spans  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-file", required=True)
    parser.add_argument("--max-records", type=int, default=None)
    args = parser.parse_args()

    records = []
    with open(args.test_file) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if args.max_records:
        records = records[: args.max_records]

    total_missed = 0
    luhn_invalid_missed = 0
    wrong_length_missed = 0
    other_missed = 0
    examples = []

    for rec in records:
        text = rec.get("text") or " ".join(rec["tokens"])
        gold, _ = locate_gold_spans(rec["tokens"], rec["labels"], text)
        gold_cc = [g for g in gold if g["type"] == "CREDIT_CARD"]
        preds = [h for h in detect.detect_all(text) if h["type"] == "CREDIT_CARD"]

        for g in gold_cc:
            hit = any(p["start"] < g["end"] and g["start"] < p["end"] for p in preds)
            if hit:
                continue
            total_missed += 1
            span_text = text[g["start"]:g["end"]]
            digits = "".join(c for c in span_text if c.isdigit())
            if len(digits) < 12 or len(digits) > 19:
                wrong_length_missed += 1
                reason = f"digit-run length {len(digits)} outside 12-19"
            elif not detect._luhn_valid(digits):
                luhn_invalid_missed += 1
                reason = "fails Luhn checksum"
            else:
                other_missed += 1
                reason = "UNEXPLAINED -- passes length+Luhn, still missed"
            if len(examples) < 15:
                examples.append((span_text, reason))

    print(f"Total missed CREDIT_CARD gold spans: {total_missed}")
    print(f"  Wrong digit-run length (12-19 required): {wrong_length_missed}")
    print(f"  Right length, fails Luhn checksum: {luhn_invalid_missed}")
    print(f"  Unexplained (right length, passes Luhn, still missed): {other_missed}")
    print()
    for span_text, reason in examples:
        print(f"  span_text={span_text!r} reason={reason}")


if __name__ == "__main__":
    main()
