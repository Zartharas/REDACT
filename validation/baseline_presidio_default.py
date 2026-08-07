"""
Off-the-shelf baseline: vanilla Microsoft Presidio, no REDACT-specific
customization, evaluated against the same fixed-seed synthetic corpus and
the same span-level precision/recall/F1 methodology used in evaluate.py.

This exists to answer a reviewer question the main evaluation harness
(evaluate.py) does not: how does REDACT's tuned three-layer ensemble compare
against the detector an organization would get by installing Presidio and
running it with its default recognizer registry, no custom MRN recognizer,
no REDACT regex layer, no entropy layer?

Differences from detect.py's scan_ner(), deliberately:
  - No custom MEDICAL_RECORD_NUMBER pattern recognizer is registered.
    Vanilla Presidio has no built-in MRN recognizer, so this baseline is
    expected to score 0 recall on that entity type. That is not a bug in
    this script; it is the honest baseline result and is reported as such.
  - No REDACT regex layer (src/detect.py's scan_regex) and no entropy layer
    are run. This measures Presidio alone, as a practitioner would get it
    out of the box, not REDACT's ensemble.
  - Same min_score=0.5 threshold as the paper's own scan_ner(), same
    overlap-based greedy one-to-one span matching (evaluate.py:match_spans),
    same 10,000-entry corpus, same canonical type vocabulary, so the
    resulting table is directly comparable to Table 1 / Table 2.

Run with: python validation/baseline_presidio_default.py --data data/synthetic_logs.jsonl
"""
import sys
import os
import json
import time
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from evaluate import load_dataset, match_spans, summarize  # noqa: E402

# Presidio's own built-in entity names -> this project's canonical vocabulary.
# No custom recognizers are added anywhere in this script.
_PRESIDIO_TO_CANONICAL = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL",
    "IP_ADDRESS": "IP",
    "US_SSN": "SSN",
    "CREDIT_CARD": "CREDIT_CARD",
    # Deliberately no MEDICAL_RECORD_NUMBER entry: vanilla Presidio has no
    # built-in recognizer for this type. MRN ground-truth spans will show up
    # as pure false negatives (0 TP, 0 FP, N FN) for this baseline.
}


def get_vanilla_analyzer():
    from presidio_analyzer import AnalyzerEngine
    # No registry.add_recognizer() call anywhere: default recognizer set only.
    return AnalyzerEngine()


def scan_vanilla_presidio(analyzer, text: str, min_score: float = 0.5) -> list[dict]:
    results = analyzer.analyze(
        text=text,
        language="en",
        entities=list(_PRESIDIO_TO_CANONICAL.keys()),
    )
    hits = []
    for r in results:
        if r.score < min_score:
            continue
        canonical = _PRESIDIO_TO_CANONICAL.get(r.entity_type)
        if canonical is None:
            continue
        hits.append({"type": canonical, "start": r.start, "end": r.end,
                     "method": "presidio_default", "score": round(r.score, 3)})
    return hits


def run(entries: list[dict], min_score: float = 0.5):
    analyzer = get_vanilla_analyzer()
    per_type = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    t0 = time.time()
    for entry in entries:
        text = entry["log"]
        gold = entry["pii"]
        pred = scan_vanilla_presidio(analyzer, text, min_score=min_score)

        by_type_gold = defaultdict(list)
        by_type_pred = defaultdict(list)
        for g in gold:
            by_type_gold[g["type"]].append(g)
        for p in pred:
            by_type_pred[p["type"]].append(p)

        all_types = set(by_type_gold) | set(by_type_pred)
        for t in all_types:
            tp, fp, fn = match_spans(by_type_gold[t], by_type_pred[t])
            per_type[t]["tp"] += tp
            per_type[t]["fp"] += fp
            per_type[t]["fn"] += fn
    elapsed = time.time() - t0
    return per_type, elapsed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, default="data/synthetic_logs.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=str, default="validation/baseline_presidio_default_results.json")
    args = parser.parse_args()

    entries = load_dataset(args.data)
    if args.limit:
        entries = entries[:args.limit]

    print(f"Loaded {len(entries)} entries ({sum(1 for e in entries if e['pii'])} with PII)")
    print("Running vanilla Presidio (default recognizer registry, no REDACT "
          "regex/entropy layers, no custom MRN recognizer)...")

    per_type, elapsed = run(entries)
    result = summarize(per_type, len(entries), elapsed, "Vanilla Presidio (off-the-shelf baseline)")

    with open(args.out, "w") as f:
        json.dump({
            "label": "vanilla_presidio_default",
            "per_type": {k: dict(v) for k, v in per_type.items()},
            "n_entries": len(entries),
            "elapsed_sec": elapsed,
            **result,
        }, f, indent=2)
    print(f"\nWrote results to {args.out}")
