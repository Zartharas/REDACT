"""
Field-level taxonomy drift detection (Section 3.3 of the chapter): tracks
what fraction of a field's values contain Critical-tier PII, compares a
current window against a baseline window, and flags any field whose
Critical-tier hit rate has moved by more than a threshold.

This exists to catch the specific failure mode Section 3.3 describes: a
field that used to carry no PII (a status code, a fixed reason string)
silently starts carrying it after an unrelated code change, and nobody
told the security team. Static field-name-to-taxonomy-tier mapping can't
catch that; re-scanning field content over time can.
"""
import sys
import os
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import detect   # noqa: E402
import fields   # noqa: E402

CRITICAL_TYPES = {"PERSON", "EMAIL", "SSN", "CREDIT_CARD", "MRN"}
SENSITIVE_TYPES = {"IP"}

# Fields need at least this many observations in BOTH windows before a rate
# change is trusted. Below this, a rate swing is as likely to be sampling
# noise as an actual drift -- flagging on tiny samples would make the
# weekly report noisy enough that people stop reading it, which defeats
# the purpose more thoroughly than missing a slow-building drift would.
MIN_SAMPLE_SIZE = 20


def field_stats(entries: list[dict], use_ner: bool = True) -> dict[tuple[str, str], dict]:
    """entries: list of {"log_type": str, "log": str} (ground truth not
    needed here -- drift detection runs the same way in production, against
    the detector's live output, not against labeled data).

    use_ner: defaults to True (matches detect.detect_all's own default),
    but can be set False to run without a spaCy/Presidio model available --
    useful for testing this module itself in an environment that can't
    reach the model download, same accommodation evaluate.py's --limit and
    run_evaluation(use_ner=...) already make elsewhere in this project.

    BUG FOUND AND FIXED 2026-08-07, while measuring the new syslog field
    extractor's usefulness (fields.py, ROADMAP item 8): this function
    previously only combined scan_regex() + scan_ner(), never
    scan_flattened() -- meaning drift detection was structurally blind to
    flattened-username PII (e.g. "donaldgarcia") in every field it
    monitors, across ALL log types, not just syslog. This is the exact
    detection gap Layer 4 (src/flattened_names.py) exists to close
    elsewhere in this project (detect.detect_all() has included it by
    default since it was added), and drift.py had simply never been
    updated to match. Confirmed via a live injection test mirroring
    validate.py's own drift-detection check (Section 5): injecting a
    flattened username into the syslog `sudo.USER` field (previously
    always the constant "root", so a 0% baseline critical-hit-rate) was
    silently NOT flagged before this fix, and correctly flagged after it.
    A production deployment relying on drift.py to catch a field that
    starts leaking PII would have missed exactly the format of PII this
    project's own headline finding says general-purpose NER already
    misses most of the time -- quietly making that gap worse rather than
    just repeating it."""
    stats: dict[tuple[str, str], dict] = defaultdict(lambda: {"total": 0, "critical_hits": 0})
    for e in entries:
        log_type = e["log_type"]
        extracted = fields.extract_fields(log_type, e["log"])
        for field_name, value in extracted.items():
            key = (log_type, field_name)
            stats[key]["total"] += 1
            hits = detect.scan_regex(value) + detect.scan_flattened(value)
            if use_ner:
                hits += detect.scan_ner(value)
            if any(h["type"] in CRITICAL_TYPES for h in hits):
                stats[key]["critical_hits"] += 1
    return dict(stats)


def compare(baseline: dict, current: dict, threshold: float = 0.05) -> list[dict]:
    """Returns one entry per field present in both windows with enough
    samples, sorted by the size of the rate change, largest first. Fields
    below MIN_SAMPLE_SIZE in either window are reported separately as
    'insufficient data' rather than silently dropped, so a reviewer can see
    what got skipped and not only what got flagged."""
    flagged = []
    insufficient = []
    all_keys = set(baseline) | set(current)
    for key in all_keys:
        b = baseline.get(key, {"total": 0, "critical_hits": 0})
        c = current.get(key, {"total": 0, "critical_hits": 0})
        if b["total"] < MIN_SAMPLE_SIZE or c["total"] < MIN_SAMPLE_SIZE:
            insufficient.append({"log_type": key[0], "field": key[1],
                                  "baseline_n": b["total"], "current_n": c["total"]})
            continue
        b_rate = b["critical_hits"] / b["total"]
        c_rate = c["critical_hits"] / c["total"]
        delta = c_rate - b_rate
        if abs(delta) >= threshold:
            flagged.append({
                "log_type": key[0], "field": key[1],
                "baseline_rate": round(b_rate, 4), "current_rate": round(c_rate, 4),
                "delta": round(delta, 4),
                "baseline_n": b["total"], "current_n": c["total"],
            })
    flagged.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return flagged, insufficient


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--threshold", type=float, default=0.05)
    args = parser.parse_args()

    with open(args.baseline) as f:
        baseline_entries = [json.loads(l) for l in f]
    with open(args.current) as f:
        current_entries = [json.loads(l) for l in f]

    baseline_stats = field_stats(baseline_entries)
    current_stats = field_stats(current_entries)
    flagged, insufficient = compare(baseline_stats, current_stats, args.threshold)

    print(f"Baseline: {len(baseline_entries)} entries, {len(baseline_stats)} distinct fields")
    print(f"Current:  {len(current_entries)} entries, {len(current_stats)} distinct fields")
    print()
    if flagged:
        print(f"=== {len(flagged)} field(s) flagged for drift (threshold {args.threshold}) ===")
        for f_ in flagged:
            print(f"  {f_['log_type']}.{f_['field']:<30} "
                  f"baseline={f_['baseline_rate']:.1%} (n={f_['baseline_n']}) -> "
                  f"current={f_['current_rate']:.1%} (n={f_['current_n']})  "
                  f"delta={f_['delta']:+.1%}")
    else:
        print("No fields flagged for drift.")
    print()
    print(f"({len(insufficient)} field(s) had fewer than {MIN_SAMPLE_SIZE} observations "
          f"in one window and were not checked)")
