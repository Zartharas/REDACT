"""
Runs REDACT's real, production detection ensemble (src/detect.py's
detect_all_field_gated -- the function src/pipeline.py and src/service.py
actually call, not a research-only variant) against every evasion variant
generate_evasion_variants.py produced, and reports the evasion rate per
(type, technique).

A technique "succeeds" (evades detection) for a given record if NONE of
the hits detect_all_field_gated() returns overlap the substituted span
with the CORRECT type. A hit of the wrong type overlapping the same span
counts as evasion too -- the point is whether the sensitive value gets
anonymized at all, not whether the ensemble produced any output near it.

Baseline control: for every (type, technique) pair, also runs the ORIGINAL
(un-evaded) value through the same detector, so the evasion rate can be
read against "how often does REDACT catch this exact value normally,"
not an assumed 100%.
"""
import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(__file__)
REPO_ROOT = Path(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))

VARIANTS_PATH = Path(_HERE) / "evasion_corpus.jsonl"
RESULTS_PATH = Path(_HERE) / "results.json"


def overlaps(hit, span):
    return hit["start"] < span["end"] and hit["end"] > span["start"]


def detected(hits, span, expected_type):
    return any(overlaps(h, span) and h["type"] == expected_type for h in hits)


def main():
    import detect  # src/detect.py

    if not VARIANTS_PATH.exists():
        print(f"ERROR: {VARIANTS_PATH} not found -- run generate_evasion_variants.py first", file=sys.stderr)
        sys.exit(1)

    variants = [json.loads(l) for l in open(VARIANTS_PATH)]
    print(f"Loaded {len(variants)} evasion-variant records")

    from collections import defaultdict
    stats = defaultdict(lambda: {"variant_total": 0, "variant_evaded": 0,
                                  "baseline_total": 0, "baseline_evaded": 0})

    baseline_cache = {}  # (log_type, log_text) -> hits, avoid re-running NER redundantly

    for v in variants:
        key = (v["pii_type"], v["technique"])
        s = stats[key]

        # --- Variant (evaded) text ---
        variant_hits = detect.detect_all_field_gated(v["log"], log_type=v["log_type"])
        s["variant_total"] += 1
        if not detected(variant_hits, v["span"], v["pii_type"]):
            s["variant_evaded"] += 1

        # --- Baseline: does REDACT catch the record's ORIGINAL, un-evaded
        # value in the ORIGINAL log line? Reconstruct the original line by
        # substituting the variant span back to the original value -- this
        # is exact (same surrounding text, same field context) and avoids
        # needing to re-index the source corpus by record_id, since
        # generate_evasion_variants.py's record_id is the evasion corpus's
        # own sequential id, not the source corpus's.
        original_text = v["log"][:v["span"]["start"]] + v["original_value"] + \
            v["log"][v["span"]["start"] + len(v["variant_value"]):]
        cache_key = (v["log_type"], original_text)
        if cache_key not in baseline_cache:
            baseline_cache[cache_key] = detect.detect_all_field_gated(original_text, log_type=v["log_type"])
        orig_hits = baseline_cache[cache_key]
        orig_span = {"start": v["span"]["start"],
                     "end": v["span"]["start"] + len(v["original_value"]),
                     "type": v["pii_type"]}
        s["baseline_total"] += 1
        if not detected(orig_hits, orig_span, v["pii_type"]):
            s["baseline_evaded"] += 1

    # --- Report ---
    report = {}
    print(f"\n{'Type':<13}{'Technique':<28}{'Baseline miss':<16}{'Variant miss':<16}{'Evasion rate':<14}")
    print("-" * 87)
    for (ptype, technique), s in sorted(stats.items()):
        vt, ve = s["variant_total"], s["variant_evaded"]
        bt, be = s["baseline_total"], s["baseline_evaded"]
        evasion_rate = ve / vt if vt else 0.0
        baseline_miss_rate = be / bt if bt else 0.0
        report[f"{ptype}:{technique}"] = {
            "n": vt,
            "baseline_miss_rate": round(baseline_miss_rate, 4),
            "variant_miss_rate": round(evasion_rate, 4),
            "baseline_missed": be,
            "variant_missed": ve,
        }
        print(f"{ptype:<13}{technique:<28}{f'{be}/{bt} ({baseline_miss_rate:.1%})':<16}"
              f"{f'{ve}/{vt} ({evasion_rate:.1%})':<16}{evasion_rate - baseline_miss_rate:+.1%}")

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
