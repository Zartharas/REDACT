"""
Evaluation harness. Loads the synthetic dataset, runs the detection ensemble,
matches predictions against ground truth by overlap, and reports
precision/recall/F1 per entity type plus throughput.

Matching rule: a predicted span counts as a match for a ground-truth span if
they share the same canonical type and their character ranges overlap by at
least one character. Matching is greedy and one-to-one (each ground-truth
span can be matched by at most one prediction and vice versa), which is the
standard approach for span-level NER evaluation and avoids inflating recall
by letting one prediction "cover" multiple ground-truth spans.
"""
import sys
import json
import time
import argparse
from collections import defaultdict

sys.path.insert(0, "src")
import detect  # noqa: E402


def overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def match_spans(gold: list[dict], pred: list[dict]) -> tuple[int, int, int]:
    """Returns (tp, fp, fn) for a single document's spans, matched per type."""
    tp = 0
    matched_gold = set()
    matched_pred = set()
    for gi, g in enumerate(gold):
        for pi, p in enumerate(pred):
            if pi in matched_pred:
                continue
            if p["type"] == g["type"] and overlaps(g, p):
                matched_gold.add(gi)
                matched_pred.add(pi)
                tp += 1
                break
    fn = len(gold) - len(matched_gold)
    fp = len(pred) - len(matched_pred)
    return tp, fp, fn


def load_dataset(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f]


def run_evaluation(entries: list[dict], use_ner: bool, use_entropy_gate: bool = False,
                    use_flattened: bool = False):
    """use_entropy_gate: if True, only run NER on lines with no regex hit at all
    (the 'tiered' strategy described in the chapter). If False, run NER on
    every line regardless of regex results (the naive strategy).
    use_flattened: if True, also run the flattened-username name-dictionary
    layer (src/flattened_names.py), added specifically to address the
    documented 5.9% recall gap on concatenated name tokens."""
    per_type = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    t0 = time.time()
    for entry in entries:
        text = entry["log"]
        gold = entry["pii"]

        regex_hits = detect.scan_regex(text)
        pred = list(regex_hits)

        if use_ner:
            if use_entropy_gate and regex_hits:
                pass  # tiered strategy: skip NER if regex already found something
            else:
                pred += detect.scan_ner(text)

        if use_flattened:
            pred += detect.scan_flattened(text)

        # de-duplicate overlapping same-type hits from different layers
        # (keep first occurrence; layers agreeing is not a defect)
        dedup = []
        for h in pred:
            if not any(h["type"] == d["type"] and overlaps(h, d) for d in dedup):
                dedup.append(h)

        by_type_gold = defaultdict(list)
        by_type_pred = defaultdict(list)
        for g in gold:
            by_type_gold[g["type"]].append(g)
        for p in dedup:
            by_type_pred[p["type"]].append(p)

        all_types = set(by_type_gold) | set(by_type_pred)
        for t in all_types:
            tp, fp, fn = match_spans(by_type_gold[t], by_type_pred[t])
            per_type[t]["tp"] += tp
            per_type[t]["fp"] += fp
            per_type[t]["fn"] += fn

    elapsed = time.time() - t0
    return per_type, elapsed


def summarize(per_type: dict, n_entries: int, elapsed: float, label: str):
    print(f"\n=== {label} ===")
    print(f"{'Type':<14}{'TP':>6}{'FP':>6}{'FN':>6}{'Precision':>11}{'Recall':>9}{'F1':>7}")
    total_tp = total_fp = total_fn = 0
    for t, c in sorted(per_type.items()):
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        total_tp += tp
        total_fp += fp
        total_fn += fn
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        print(f"{t:<14}{tp:>6}{fp:>6}{fn:>6}{precision:>11.3f}{recall:>9.3f}{f1:>7.3f}")
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 0.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
    print(f"{'MICRO-AVG':<14}{total_tp:>6}{total_fp:>6}{total_fn:>6}{micro_p:>11.3f}{micro_r:>9.3f}{micro_f1:>7.3f}")
    print(f"Processed {n_entries} entries in {elapsed:.2f}s -> {n_entries/elapsed:.1f} events/sec")
    return {"micro_precision": micro_p, "micro_recall": micro_r, "micro_f1": micro_f1,
            "events_per_sec": n_entries / elapsed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, default="data/synthetic_logs.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    entries = load_dataset(args.data)
    if args.limit:
        entries = entries[:args.limit]

    print(f"Loaded {len(entries)} entries ({sum(1 for e in entries if e['pii'])} with PII)")

    per_type, elapsed = run_evaluation(entries, use_ner=False)
    summarize(per_type, len(entries), elapsed, "Regex only")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=True)
    summarize(per_type, len(entries), elapsed, "Regex + NER (tiered: NER only when regex found nothing)")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False)
    summarize(per_type, len(entries), elapsed, "Regex + NER (naive: NER on every line)")

    per_type, elapsed = run_evaluation(entries, use_ner=True, use_entropy_gate=False,
                                        use_flattened=True)
    summarize(per_type, len(entries), elapsed,
              "Regex + NER (naive) + flattened-username name dictionary")
