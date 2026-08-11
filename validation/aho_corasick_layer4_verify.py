"""
Verification for the Aho-Corasick rewrite of Layer 4's _segment_match()
(src/flattened_names.py, 2026-08-11).

Confirms two things directly, not by inspection:
1. Correctness: the Aho-Corasick implementation produces byte-identical
   hits to the original O(token_length) split-loop implementation, across
   the full 10,000-line canonical synthetic corpus (data/synthetic_logs.jsonl).
2. Throughput: measures wall-clock difference between the two
   implementations over the same corpus, so any speedup claim in
   BUGS_AND_FIXES.md is a measured number, not an assumed one.

Run: python3 validation/aho_corasick_layer4_verify.py
"""
import json
import re
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import flattened_names as new_impl  # the Aho-Corasick version, current src/

FIRST_NAMES = new_impl.FIRST_NAMES
LAST_NAMES = new_impl.LAST_NAMES
MIN_PART_LEN = new_impl.MIN_PART_LEN
MIN_TOKEN_LEN = new_impl.MIN_TOKEN_LEN
MAX_TOKEN_LEN = new_impl.MAX_TOKEN_LEN
_TOKEN_RE = new_impl._TOKEN_RE
_TRAILING_DIGITS_RE = new_impl._TRAILING_DIGITS_RE


def _old_segment_match(token: str) -> bool:
    """The original split-loop implementation, reproduced exactly as it
    was before the Aho-Corasick rewrite, for direct comparison."""
    lower = token.lower()
    n = len(lower)
    for split in range(MIN_PART_LEN, n - MIN_PART_LEN + 1):
        left, right = lower[:split], lower[split:]
        if (left in FIRST_NAMES and right in LAST_NAMES) or \
           (left in LAST_NAMES and right in FIRST_NAMES):
            return True
    return False


def _old_separator_match(token: str) -> bool:
    parts = re.split(r"[._-]", token.lower())
    if len(parts) != 2:
        return False
    a, b = parts
    if len(a) < MIN_PART_LEN or len(b) < MIN_PART_LEN:
        return False
    return (a in FIRST_NAMES and b in LAST_NAMES) or (a in LAST_NAMES and b in FIRST_NAMES)


def old_scan_flattened_names(text: str) -> list[dict]:
    hits = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        if not (MIN_TOKEN_LEN <= len(raw) <= MAX_TOKEN_LEN):
            continue
        if text[m.end():m.end() + 1] == "@":
            continue
        if "." in raw or "_" in raw or "-" in raw:
            matched = _old_separator_match(raw)
        else:
            core = _TRAILING_DIGITS_RE.sub("", raw)
            if len(core) < MIN_TOKEN_LEN:
                continue
            matched = _old_segment_match(core)
            if matched:
                end = m.start() + len(core)
                hits.append({"type": "PERSON", "start": m.start(), "end": end,
                             "method": "flattened_name_dict"})
                continue
        if matched:
            hits.append({"type": "PERSON", "start": m.start(), "end": m.end(),
                         "method": "flattened_name_dict"})
    return hits


def main():
    corpus_path = Path(__file__).resolve().parent.parent / "data" / "synthetic_logs.jsonl"
    lines = []
    with open(corpus_path) as f:
        for raw_line in f:
            entry = json.loads(raw_line)
            lines.append(entry.get("log") or entry.get("message") or entry.get("text") or json.dumps(entry))

    print(f"Loaded {len(lines)} lines from {corpus_path.name}")

    # --- Correctness check ---
    mismatches = 0
    total_old_hits = 0
    total_new_hits = 0
    for i, line in enumerate(lines):
        old_hits = old_scan_flattened_names(line)
        new_hits = new_impl.scan_flattened_names(line)
        total_old_hits += len(old_hits)
        total_new_hits += len(new_hits)
        if old_hits != new_hits:
            mismatches += 1
            if mismatches <= 5:
                print(f"MISMATCH at line {i}: old={old_hits} new={new_hits}")
                print(f"  text: {line!r}")

    print(f"\nOld total hits: {total_old_hits}")
    print(f"New total hits: {total_new_hits}")
    print(f"Mismatched lines: {mismatches} / {len(lines)}")
    if mismatches == 0:
        print("CORRECTNESS: PASS -- byte-identical output across full corpus")
    else:
        print("CORRECTNESS: FAIL -- see mismatches above")

    # --- Throughput check ---
    n_reps = 3
    t0 = time.perf_counter()
    for _ in range(n_reps):
        for line in lines:
            old_scan_flattened_names(line)
    old_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(n_reps):
        for line in lines:
            new_impl.scan_flattened_names(line)
    new_elapsed = time.perf_counter() - t0

    old_rate = (len(lines) * n_reps) / old_elapsed
    new_rate = (len(lines) * n_reps) / new_elapsed

    print(f"\nOld implementation: {old_elapsed:.3f}s for {n_reps}x{len(lines)} lines "
          f"({old_rate:.0f} lines/sec)")
    print(f"New implementation: {new_elapsed:.3f}s for {n_reps}x{len(lines)} lines "
          f"({new_rate:.0f} lines/sec)")
    print(f"Speedup: {old_elapsed / new_elapsed:.2f}x")


if __name__ == "__main__":
    main()
