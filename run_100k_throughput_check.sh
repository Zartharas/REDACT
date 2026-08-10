#!/usr/bin/env bash
# Task #11: larger synthetic sample to resolve the naive-vs-field-gated
# throughput question. The 10K-line run measured a 1.8% field-gated "win"
# that's smaller than the 4.6% run-to-run noise floor measured on the same
# identical-code control subset -- not enough evidence either way. A 10x
# corpus gives real statistical power on the same controlled comparison.
#
# Run from the repo root:
#   chmod +x run_100k_throughput_check.sh
#   ./run_100k_throughput_check.sh
set -euo pipefail

git push origin main   # publish the field-gated production-wiring commit first

# Same fixed-seed generator already used for the 10K corpus (src/generate_logs.py
# hasn't changed -- --n already supported arbitrary sizes). Same seed means
# the first 10,000 entries here are identical to the existing 10K corpus;
# this just extends the same deterministic sequence further.
python src/generate_logs.py --n 100000 --out data/synthetic_logs_100k.jsonl
wc -l data/synthetic_logs_100k.jsonl

# Runs all five conditions (regex-only, tiered, field-gated, naive,
# naive+flattened) -- same script, same logic already run three times at
# 10K lines, just against the larger corpus. Expect roughly 10x the 10K
# run's wall-clock time (that was a few minutes total; this could be
# 20-40+ minutes depending on your machine, since four of the five
# conditions call the real spaCy/Presidio model).
python src/evaluate.py --data data/synthetic_logs_100k.jsonl

# Follow-up, added after seeing the 100K run's own result: the identical-
# code "noise floor" (measured on lines where field-gated and naive run
# the exact same scan_ner(text) call) should shrink by roughly sqrt(10)
# going from 10K to 100K lines if it's ordinary sampling noise. It didn't
# (stayed ~4.2-4.6% both times) -- pointing at a run-order effect instead
# (evaluate.py always profiles field-gated before naive in the same
# process), which would also call the "field-gated is faster" result on
# the regex-hit subset into question. This alternates which condition
# runs first across repetitions on a fixed 2,000-line sample, in the same
# process, to separate a real algorithmic effect from a run-order
# artifact. Takes a few minutes (isolates just the NER call, not a full
# evaluate.py pass).
python validation/field_gate_throughput_ab_test.py --data data/synthetic_logs_100k.jsonl --sample-size 2000 --repeats 6
