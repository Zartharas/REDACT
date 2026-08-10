"""
Order-controlled A/B throughput comparison: field-gated vs. naive NER cost,
on the SAME sample of regex-hit lines, run in the SAME process.

Why this exists: evaluate.py's __main__ profiles field-gated (condition 3)
and then naive (condition 4) once each, in that fixed order, and reports
a single throughput gap. The 10,000-line run measured field-gated 1.8%
faster than naive on the lines where the two strategies actually differ,
with the identical-code no-regex-hit subset showing a 4.6% run-to-run
difference despite running the same code -- treated at the time as the
"noise floor" this 1.8% gap needed to clear (it didn't quite).

The 100,000-line rerun (10x the sample) should have shrunk that noise
floor by roughly sqrt(10) =~ 3.15x if it were ordinary statistical
sampling noise -- down to roughly 1.5%. It didn't: still 4.2% on
53,329 calls. A gap that doesn't shrink with 10x more independent
samples is not sampling noise; it's a systematic effect, and the leading
suspect is RUN ORDER -- evaluate.py always profiles field-gated before
naive in the same continuous process, and in both runs the
later-running condition (naive) measured faster on IDENTICAL code. That
pattern is consistent with something in the process/OS/CPU state
(frequency scaling ramp-up, memory allocator warmup, page-cache effects)
getting cheaper over the course of a long-running script, independent of
which detection strategy is active.

If that's real, it also undermines the regex-hit-subset "field-gated is
faster" result, since that comparison has the exact same fixed order
(field-gated always measured first).

This script controls for that directly: it isolates JUST the scan_ner()
call cost (no candidate-build overhead, no matching/scoring, none of
evaluate.py's other per-line work) on one fixed sample of regex-hit
lines, and alternates which condition runs first across N repetitions
within the SAME process. If field-gated is genuinely faster, it should
win regardless of which position it runs in. If the earlier result was
an order artifact, "whichever condition ran second" should consistently
look faster, not "field-gated" specifically.

Needs the real spaCy/Presidio model (en_core_web_lg) -- same environment
constraint as everything else in this project that calls detect.scan_ner,
so this is meant to be run locally, not in CI (see tests/README.md's
"Not covered here, deliberately" section for the standing reasons).

Run:
    python validation/field_gate_throughput_ab_test.py --data data/synthetic_logs_100k.jsonl
"""
import argparse
import json
import random
import sys
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import detect  # noqa: E402


def load_regex_hit_sample(path: str, limit: int, seed: int = 42):
    """Loads entries from `path`, shuffles them (fixed seed, for a
    reproducible sample independent of the corpus's own generation order),
    and keeps the first `limit` that have at least one regex hit -- the
    only lines where field-gated and naive can possibly differ at all."""
    with open(path) as f:
        entries = [json.loads(line) for line in f]
    random.Random(seed).shuffle(entries)

    sample = []
    for e in entries:
        text = e["log"]
        hits = detect.scan_regex(text)
        if hits:
            sample.append((text, e.get("log_type"), hits))
        if len(sample) >= limit:
            break
    return sample


def time_field_gated(sample) -> float:
    t0 = time.perf_counter()
    for text, log_type, hits in sample:
        ner_text, segments = detect.build_ner_candidate(text, log_type, hits)
        if ner_text is not None:
            detect.scan_ner(ner_text)
        # else: safe-to-skip case: no call to time, matching production
        # behavior (detect_all_field_gated skips scan_ner entirely here too)
    return time.perf_counter() - t0


def time_naive(sample) -> float:
    t0 = time.perf_counter()
    for text, log_type, hits in sample:
        detect.scan_ner(text)
    return time.perf_counter() - t0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/synthetic_logs_100k.jsonl")
    parser.add_argument("--sample-size", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args()

    sample = load_regex_hit_sample(args.data, limit=args.sample_size)
    print(f"Loaded {len(sample)} regex-hit lines for the A/B comparison "
          f"(requested {args.sample_size})")
    if len(sample) < args.sample_size:
        print("  (fewer regex-hit lines available in this dataset than requested)")

    fg_times = []
    naive_times = []
    orders = []
    for i in range(args.repeats):
        if i % 2 == 0:
            fg = time_field_gated(sample)
            nv = time_naive(sample)
            order = "field-gated first"
        else:
            nv = time_naive(sample)
            fg = time_field_gated(sample)
            order = "naive first"
        fg_times.append(fg)
        naive_times.append(nv)
        orders.append(order)

        fg_ms = fg / len(sample) * 1000
        nv_ms = nv / len(sample) * 1000
        pct = (nv_ms - fg_ms) / nv_ms * 100
        print(f"rep {i + 1} ({order}): field-gated={fg_ms:.4f}ms/line, "
              f"naive={nv_ms:.4f}ms/line, field-gated {pct:+.2f}% vs naive")

    avg_fg = sum(fg_times) / len(fg_times) / len(sample) * 1000
    avg_nv = sum(naive_times) / len(naive_times) / len(sample) * 1000
    overall_pct = (avg_nv - avg_fg) / avg_nv * 100
    print(f"\nOverall average across {args.repeats} reps: "
          f"field-gated={avg_fg:.4f}ms/line, naive={avg_nv:.4f}ms/line, "
          f"field-gated {overall_pct:+.2f}% vs naive")

    # The actual diagnostic this script exists to produce: does "ran
    # first" or "ran second" predict the measured time better than which
    # ALGORITHM was used? If yes, order is confounding the result.
    first_slot_fg = [fg_times[i] for i in range(args.repeats) if i % 2 == 0]
    second_slot_fg = [fg_times[i] for i in range(args.repeats) if i % 2 == 1]
    first_slot_nv = [naive_times[i] for i in range(args.repeats) if i % 2 == 1]
    second_slot_nv = [naive_times[i] for i in range(args.repeats) if i % 2 == 0]

    print("\nOrder-effect check (compares each ALGORITHM's own time "
          "depending on whether it ran 1st or 2nd in its repetition):")
    if first_slot_fg:
        print(f"  field-gated, ran 1st:  avg "
              f"{sum(first_slot_fg) / len(first_slot_fg) / len(sample) * 1000:.4f}ms/line")
    if second_slot_fg:
        print(f"  field-gated, ran 2nd:  avg "
              f"{sum(second_slot_fg) / len(second_slot_fg) / len(sample) * 1000:.4f}ms/line")
    if first_slot_nv:
        print(f"  naive,       ran 1st:  avg "
              f"{sum(first_slot_nv) / len(first_slot_nv) / len(sample) * 1000:.4f}ms/line")
    if second_slot_nv:
        print(f"  naive,       ran 2nd:  avg "
              f"{sum(second_slot_nv) / len(second_slot_nv) / len(sample) * 1000:.4f}ms/line")
    print("\nIf '2nd' is consistently faster than '1st' for BOTH algorithms "
          "above, that's the order/warmup effect, not a real field-gated "
          "advantage -- the earlier single-pass evaluate.py numbers would "
          "be confounded by always running field-gated first. If field-"
          "gated is faster in BOTH the 'ran 1st' and 'ran 2nd' rows above, "
          "that's a real, order-independent effect.")


if __name__ == "__main__":
    main()
