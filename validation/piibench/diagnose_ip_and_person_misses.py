"""
Diagnostic for the 2026-09 smoke-test run of evaluate_piibench.py: IP came
back 0/15 recall (every single gold IP_ADDRESS span missed) and PERSON came
back very low precision (20 TP / 97 FP). Before treating either number as a
real finding, check whether it's a genuine detection gap or a reconstruction
artifact -- same "diagnose before fixing" discipline as every other bug in
this project's BUGS_AND_FIXES.md.

CONFIRMED, 2026-09 (a live 200-record run): every single gold IP span
showed internal spaces once reconstructed via `" ".join(tokens)` -- e.g. a
real IPv4 gold span rendered as `'140 . 115 . 236 . 150'`, which no IPv4
regex matches. Worse, the token stream itself turned out to be lowercased
and accent-stripped relative to the real source text in several records
(French "chere" for the real "chère"), corrupting NER input too. Neither
was a REDACT detection bug -- the reconstructed text fed to it was already
broken. evaluate_piibench.py has since been rewritten to run REDACT against
each record's real `text` field (present when the PIIBench pipeline was run
with `--include-text`) and re-anchor gold spans into that text via a small
per-token regex (see build_span_regex's docstring there). This diagnostic
now exercises that same corrected path, so its own findings stay consistent
with what evaluate_piibench.py actually scores.

Run: python validation/piibench/diagnose_ip_and_person_misses.py --test-file ~/pii-bench/data/test_5k.jsonl --max-records 200
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect  # noqa: E402
from evaluate_piibench import locate_gold_spans, PIIBENCH_TO_REDACT  # noqa: E402


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

    print(f"=== IP_ADDRESS gold spans in first {len(records)} records ===")
    ip_shown = 0
    for rec in records:
        text = rec.get("text") or " ".join(rec["tokens"])
        gold, _ = locate_gold_spans(rec["tokens"], rec["labels"], text)
        for g in gold:
            if g["type"] != "IP":
                continue
            span_text = text[g["start"]:g["end"]]
            has_internal_space = " " in span_text
            print(f"  span_text={span_text!r} has_internal_space={has_internal_space}")
            ip_shown += 1
            if ip_shown >= 20:
                break
        if ip_shown >= 20:
            break
    if ip_shown == 0:
        print("  (none found in this many records)")

    print(f"\n=== First 15 PERSON false positives (predicted, no matching gold PERSON span) ===")
    shown = 0
    for rec in records:
        text = rec.get("text") or " ".join(rec["tokens"])
        gold, _ = locate_gold_spans(rec["tokens"], rec["labels"], text)
        gold_person = [g for g in gold if g["type"] == "PERSON"]
        preds = [h for h in detect.detect_all(text) if h["type"] == "PERSON"]
        for p in preds:
            hit = any(p["start"] < g["end"] and g["start"] < p["end"] for g in gold_person)
            if not hit:
                snippet_start = max(0, p["start"] - 30)
                snippet_end = min(len(text), p["end"] + 30)
                print(f"  matched_text={text[p['start']:p['end']]!r} "
                      f"context={text[snippet_start:snippet_end]!r}")
                shown += 1
        if shown >= 15:
            break


if __name__ == "__main__":
    main()
