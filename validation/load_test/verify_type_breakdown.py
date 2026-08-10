"""
Task #12 follow-up, 2026-08-10: verify the 893,150-audit-count coincidence
between the 2026-08-08 (naive-detection) and 2026-08-10 (field-gated-
detection) 1,000,000-line load test runs by directly comparing per-type
detection counts, not just trusting the aggregate match.

The production OpenSearch aggregation for the 2026-08-10 run (queried
against redact-audit-trail-*, field audit_event.field_type.keyword) came
back:

    IP:           500,631
    PERSON:       267,018
    EMAIL:         50,575
    CREDIT_CARD:   25,176
    SSN:           24,980
    MRN:           24,770
    -------------------------
    total:        893,150   (exact match to the 2026-08-08 run's total)

Regex-covered types (IP, EMAIL, CREDIT_CARD, SSN, MRN) are byte-identical
between naive and field-gated by construction -- both call the same
scan_regex(). Only PERSON, which comes entirely from NER, can differ. This
script reruns both detect_all (naive) and detect_all_field_gated on a
sample of the SAME seeded raw corpus used for the 1,000,000-line run, and
reports each strategy's PERSON share -- if field-gated's local PERSON
count on this sample lands close to naive's (matching the recall-parity
finding from this session's real-data validation), that's direct evidence
the 893,150 match is a real consequence of recall parity, not evidence
that field-gating silently failed to engage in production.

Usage (run locally, NOT in this sandbox -- needs the en_core_web_lg model
already installed for every other validation script this session):

    python3 validation/load_test/verify_type_breakdown.py [--sample-per-type N]

Requires data/raw/{windows_events.log,syslog,cloudtrail.json} to still
exist from the 1,000,000-line run (export_raw_logs.py's output -- these
are host files, untouched by `docker compose down -v`).
"""
import argparse
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import detect  # noqa: E402
import anonymize  # noqa: E402

RAW_FILES = {
    "windows_event": "data/raw/windows_events.log",
    "syslog": "data/raw/syslog",
    "cloudtrail": "data/raw/cloudtrail.json",
}


def sample_lines(path: str, n: int, seed: int = 42) -> list[str]:
    with open(path) as f:
        lines = [line.rstrip("\n") for line in f if line.strip()]
    rng = random.Random(seed)
    if len(lines) <= n:
        return lines
    return rng.sample(lines, n)


def tally(lines: list[str], log_type: str, field_gated: bool) -> Counter:
    """Mirrors src/service.py's exact pipeline (line 257-259), not raw
    ensemble output: HIGH_ENTROPY is filtered out, then dedup_spans()
    collapses same-type overlapping spans -- required here specifically
    because scan_ner()'s Presidio analyzer requests entities=list(
    _PRESIDIO_TO_CANONICAL.keys()) (src/detect.py:66), which includes
    EMAIL_ADDRESS/IP_ADDRESS/US_SSN/CREDIT_CARD, not just PERSON --
    Presidio's own built-in recognizers for those types independently
    re-detect the same substrings this project's own scan_regex() already
    caught, whenever NER runs on text that still contains them. naive
    calls scan_ner on the full original line every time, so every
    regex-covered value gets a same-type overlapping duplicate hit from
    Presidio's built-in recognizer on top of scan_regex's own hit;
    field-gated's build_ner_candidate excises exactly those spans before
    calling scan_ner, so Presidio's built-in recognizers never see that
    text there and never produce the duplicate. Without dedup_spans(),
    naive's regex-type counts come out ~2x field-gated's here -- not a
    real detection-strategy difference, an artifact of comparing raw
    ensemble output instead of what's actually anonymized/audited."""
    counts = Counter()
    for line in lines:
        if field_gated:
            hits = detect.detect_all_field_gated(line, log_type=log_type)
        else:
            hits = detect.detect_all(line, use_ner=True)
        typed_spans = [h for h in hits if h["type"] != "HIGH_ENTROPY"]
        typed_spans = anonymize.dedup_spans(typed_spans)
        for h in typed_spans:
            counts[h["type"]] += 1
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-per-type", type=int, default=3000)
    args = parser.parse_args()

    naive_total = Counter()
    field_gated_total = Counter()
    sampled_counts = {}

    for log_type, path in RAW_FILES.items():
        if not os.path.exists(path):
            print(f"SKIP {log_type}: {path} not found -- was data/raw/ cleaned up?")
            continue
        lines = sample_lines(path, args.sample_per_type)
        sampled_counts[log_type] = len(lines)
        print(f"--- {log_type}: sampling {len(lines)} of the full corpus ---")

        naive_counts = tally(lines, log_type, field_gated=False)
        fg_counts = tally(lines, log_type, field_gated=True)

        for t in sorted(set(naive_counts) | set(fg_counts)):
            print(f"    {t:12s} naive={naive_counts[t]:6d}  field_gated={fg_counts[t]:6d}")

        naive_total.update(naive_counts)
        field_gated_total.update(fg_counts)

    print()
    print("=== Aggregate across all sampled lines ===")
    all_types = sorted(set(naive_total) | set(field_gated_total))
    for t in all_types:
        print(f"    {t:12s} naive={naive_total[t]:6d}  field_gated={field_gated_total[t]:6d}")

    naive_grand = sum(naive_total.values())
    fg_grand = sum(field_gated_total.values())
    naive_person_share = naive_total["PERSON"] / naive_grand if naive_grand else 0
    fg_person_share = field_gated_total["PERSON"] / fg_grand if fg_grand else 0

    print()
    print(f"naive PERSON share of all detections:        {naive_person_share:.4f}")
    print(f"field-gated PERSON share of all detections:   {fg_person_share:.4f}")
    print(f"production (2026-08-10, 1M-line run) PERSON share: {267018/893150:.4f}")
    print()
    for t in all_types:
        if naive_total[t] == 0:
            continue
        regex_covered = t != "PERSON"
        label = "(regex-covered, expected identical by construction)" if regex_covered else "(NER-derived, the actual question)"
        delta = field_gated_total[t] - naive_total[t]
        pct = (delta / naive_total[t]) * 100 if naive_total[t] else float("nan")
        print(f"    {t:12s} field_gated - naive = {delta:+6d}  ({pct:+.1f}%)  {label}")


if __name__ == "__main__":
    main()
