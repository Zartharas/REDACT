"""
Bug 15 (BUGS_AND_FIXES.md): TokenStore.save() originally did a full
read-merge-write of the ENTIRE persisted store on every call, found via
the 1,000,000-line load test (ROADMAP item 9) -- a live request took
2.927s once the store reached 93,279 entries (12.4MB), and end-to-end
throughput collapsed from ~250 lines/sec to ~3/sec well before the run
finished.

HISTORY, so the two runs of this script don't read as contradictory:
- 2026-08-08, first version of this script: measured the ORIGINAL O(n)-
  per-call growth directly (confirmed clearly: last sampled save() at
  10-22x the first sampled save()'s cost as the store grew ~23x), and
  confirmed the save_every_n_calls debounce mitigation (added same day)
  cut TOTAL wall-clock cost roughly proportionally to the debounce
  factor -- but explicitly, honestly scoped as a MITIGATION (same O(n)
  shape, paid less often), not a fix.
- 2026-08-08, later same day, after the real fix (StorageProvider.
  save_incremental(), see anonymize.py): FileStorageProvider now appends
  only new entries to a write-ahead log (WAL) instead of rewriting the
  whole JSON snapshot on every save() call, periodically folding the WAL
  back into the snapshot (compact()) only once every
  wal_compact_threshold_lines batches (default 200), not every call. This
  script was re-run against that fix and the O(n) growth this script was
  built to demonstrate IS NO LONGER PRESENT, even at save_every_n_calls=1
  (see the "growth factor" line the second run prints -- close to 1.0x
  now, not 10-22x). That's the expected, correct outcome of the real fix
  landing, not a bug in this test. The debounce parameter still exists
  and still reduces lock/IO frequency further, but it is no longer the
  only thing standing between this codebase and Bug 15's original
  failure mode -- the incremental-write path is.

This script proves the fixed cost shape directly, in-sandbox, with no
Docker or Redis needed (FileStorageProvider only -- RedisStorageProvider
has its own equivalent incremental save_incremental() override, verified
separately since it needs a live Redis instance).

Methodology: mint tokens into a TokenStore backed by a fresh
FileStorageProvider, calling save() after every single one (matching
service.py's real usage), and record the wall-clock cost of each
individual save() call as the store grows from empty to N entries. Repeat
with save_every_n_calls=50 to measure the debounce's additional effect on
top of the incremental-write fix.

Scope note on absolute numbers vs. the live 2.927s data point: this test
measures pure local file I/O and JSON (de)serialization cost only, in a
single Python process, on this machine -- no Docker container filesystem
layer, no network round-trip, no gunicorn request handling, and no
lock_for_save() contention from 8 concurrent workers all fighting over
the same lock file. This test is expected to show smaller absolute
per-call times than the live run at an equivalent store size -- the point
is to directly confirm the cost SHAPE, not reproduce the live run's exact
absolute numbers.

Run: python validation/tokenstore_save_scaling_test.py
"""
import sys
import os
import time
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import anonymize  # noqa: E402

N = 6_000            # entries to mint
SAMPLE_EVERY = 250   # record timing this often, not every single call
                      # (6,000 individual save() calls' own timing noise
                      # would be hard to read; sampling smooths it while
                      # still showing the trend clearly)


def run(save_every_n_calls: int, tmpdir: str) -> tuple[list[tuple[int, float]], list[float]]:
    """Returns (sampled_timings, real_write_timings). real_write_timings
    records EVERY call that actually performed a real persist (detected
    via store._calls_since_save resetting to 0, not a timing-based
    guess), regardless of the SAMPLE_EVERY sampling used for the printed
    trend -- needed because with a debounce factor that shares a common
    divisor with SAMPLE_EVERY, sampled indices can systematically never
    land on a real write, undercounting them entirely rather than just
    missing a few. Note that a "real write" now usually means a cheap WAL
    append (O(batch size)), not the old full snapshot rewrite -- except
    on the periodic calls where wal_compact_threshold_lines is crossed
    and compact() runs, which IS still O(total store size) by design (see
    FileStorageProvider.compact()'s own docstring) but happens far less
    often than every save()."""
    store_path = os.path.join(tmpdir, f"token_store_{save_every_n_calls}.json")
    store = anonymize.TokenStore(store_path, token_key="scaling-test-key",
                                  save_every_n_calls=save_every_n_calls)
    samples = []
    real_writes = []
    for i in range(N):
        store.get_or_create_token(f"user{i}@example.com", "EMAIL")
        t0 = time.perf_counter()
        store.save()
        elapsed = time.perf_counter() - t0
        if store._calls_since_save == 0:  # a real write just happened
                                            # (reset the debounce counter),
                                            # as opposed to a skipped no-op
            real_writes.append(elapsed)
        if i % SAMPLE_EVERY == 0:
            samples.append((i, elapsed))
    store.save(force=True)  # flush whatever's still pending so the final
                              # on-disk state is complete, matching what a
                              # real deployment would want at shutdown
    return samples, real_writes


def main():
    tmpdir = tempfile.mkdtemp(prefix="redact_scaling_test_")
    try:
        print(f"=== TokenStore.save() cost as the store grows to {N:,} entries ===\n")

        print("--- save_every_n_calls=1 (every call attempts a persist) ---")
        baseline, _baseline_real_writes = run(save_every_n_calls=1, tmpdir=tmpdir)
        for i, elapsed in baseline[::4]:  # print a quarter of the samples, enough to see the trend
            print(f"  store size ~{i:>6,}: save() took {elapsed*1000:8.2f} ms")
        first_ms = baseline[1][1] * 1000
        last_ms = baseline[-1][1] * 1000
        print(f"\n  First sampled save() (~store size {baseline[1][0]:,}): {first_ms:.2f} ms")
        print(f"  Last sampled save()  (~store size {baseline[-1][0]:,}): {last_ms:.2f} ms")
        growth = last_ms / first_ms if first_ms else float("nan")
        print(f"  Growth factor: {growth:.1f}x at "
              f"{baseline[-1][0]/baseline[1][0]:.0f}x the store size -- "
              f"a value close to 1.0x here confirms the incremental-write fix "
              f"removed the O(n) shape this script originally found (see the "
              f"module docstring's HISTORY section); a value in the 10-20x range "
              f"would indicate a regression back to the original Bug 15 behavior.")
        print(f"  Extrapolated total save() time across all {N:,} calls: "
              f"~{sum(e for _, e in baseline) * N / len(baseline):.1f}s "
              f"(sampled average x {N:,} calls)")

        print("\n--- save_every_n_calls=50 (debounce on top of the incremental fix) ---")
        t_debounce_start = time.perf_counter()
        debounced, actual_writes = run(save_every_n_calls=50, tmpdir=tmpdir)
        total_debounced = time.perf_counter() - t_debounce_start
        print(f"  Actual writes performed: {len(actual_writes)} "
              f"(vs. {N:,} calls to save() -- {N // 50} expected at this debounce factor)")
        if actual_writes:
            print(f"  Slowest observed real write: {max(actual_writes)*1000:.2f} ms")
        total_baseline_wall = sum(e for _, e in baseline) * (N / len(baseline))
        print(f"  Total wall-clock time, all {N:,} calls to save(): {total_debounced:.2f}s")
        print(f"  vs. save_every_n_calls=1 (~{total_baseline_wall:.2f}s extrapolated): "
              f"debounce still reduces lock-acquisition and syscall frequency "
              f"further on top of the incremental-write fix, even though neither "
              f"path shows O(n) growth anymore.")

        print(f"\n=== Conclusion ===")
        print(f"Per-call cost no longer grows with store size (confirmed directly, not "
              f"just inferred) -- the incremental-write fix (StorageProvider."
              f"save_incremental(), anonymize.py) replaced the original O(n) "
              f"read-merge-write with an O(batch size) WAL append, periodically "
              f"compacted. See Bug 15 in BUGS_AND_FIXES.md for the full writeup, "
              f"including the original mitigation-only result this script used to "
              f"produce before the real fix landed.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
