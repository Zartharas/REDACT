"""
ROADMAP item 11. Measures src/detect.py's scan_entropy() against
secrets_corpus.jsonl (generate_secrets_corpus.py) -- the category entropy
detection is actually built for (API keys, tokens, opaque hashes) -- across
the same threshold sweep the main README already reports for the wrong
target (min length 12-20, entropy threshold 3.3-4.2), so the two results
are directly comparable.

Run: python validation/entropy_fair_test/generate_secrets_corpus.py
     python validation/entropy_fair_test/evaluate_entropy.py
"""
import sys
import json
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import detect  # noqa: E402


def overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def evaluate(entries: list[dict], min_len: int, threshold: float) -> dict:
    tp = fp = fn = 0
    clean_lines = sum(1 for e in entries if not e["pii"])
    clean_lines_flagged = 0

    for e in entries:
        text = e["log"]
        gold = e["pii"]
        hits = detect.scan_entropy(text, min_len=min_len, threshold=threshold)

        matched_gold = set()
        matched_hit = set()
        for hi, h in enumerate(hits):
            for gi, g in enumerate(gold):
                if gi in matched_gold:
                    continue
                if overlaps(h, g):
                    matched_gold.add(gi)
                    matched_hit.add(hi)
                    tp += 1
                    break
        fn += len(gold) - len(matched_gold)
        fp += len(hits) - len(matched_hit)

        if not gold and hits:
            clean_lines_flagged += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    false_alarm_rate = clean_lines_flagged / clean_lines if clean_lines else 0.0

    return {
        "min_len": min_len, "threshold": threshold,
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "false_alarm_rate": false_alarm_rate,
    }


def main(path: str):
    entries = [json.loads(l) for l in open(path)]
    n_dirty = sum(1 for e in entries if e["pii"])
    n_clean = len(entries) - n_dirty
    print(f"Loaded {len(entries)} entries ({n_dirty} with a real secret, {n_clean} clean)\n")

    print(f"{'min_len':>8}{'threshold':>10}{'TP':>6}{'FP':>6}{'FN':>6}"
          f"{'Precision':>11}{'Recall':>9}{'F1':>7}{'FalseAlarm%':>13}")
    best = None
    for min_len in (12, 16, 20):
        for threshold in (3.3, 3.6, 3.9, 4.2):
            r = evaluate(entries, min_len, threshold)
            print(f"{r['min_len']:>8}{r['threshold']:>10.1f}{r['tp']:>6}{r['fp']:>6}{r['fn']:>6}"
                  f"{r['precision']:>11.3f}{r['recall']:>9.3f}{r['f1']:>7.3f}"
                  f"{r['false_alarm_rate']:>12.1%}")
            if best is None or r["f1"] > best["f1"]:
                best = r

    print(f"\nBest F1 operating point: min_len={best['min_len']}, threshold={best['threshold']} "
          f"-> precision={best['precision']:.3f}, recall={best['recall']:.3f}, "
          f"f1={best['f1']:.3f}, false-alarm rate={best['false_alarm_rate']:.1%}")
    print(f"\nCompare to the main synthetic corpus's entropy numbers reported in README.md "
          f"(2.3% unique recall, 34.8% false-alarm rate at the most permissive threshold "
          f"tested there, min_len=12/threshold=3.3) -- that corpus doesn't contain this "
          f"category at all, so this is the fairer test of the same detection layer.")


if __name__ == "__main__":
    default_path = os.path.join(os.path.dirname(__file__), "secrets_corpus.jsonl")
    main(sys.argv[1] if len(sys.argv) > 1 else default_path)
