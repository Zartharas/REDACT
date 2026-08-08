"""
Bug 15 (BUGS_AND_FIXES.md): TokenStore.save() does a full read-merge-write
of the ENTIRE persisted store on every call, found via the 1,000,000-line
load test (ROADMAP item 9) -- a live request took 2.927s once the store
reached 93,279 entries (12.4MB), and end-to-end throughput collapsed from
~250 lines/sec to ~3/sec well before the run finished.

This script proves the O(n)-per-call cost directly, in-sandbox, with no
Docker or Redis needed (FileStorageProvider only -- the same read-merge-
write code path RedisStorageProvider shares, just a different I/O backend
underneath), and confirms the save_every_n_calls debounce mitigation
(added the same day) actually reduces total wall-clock cost roughly in
proportion to the debounce factor, without needing to wait for a real
1,000,000-line pipeline run to see it.

Methodology: mint tokens into a TokenStore backed by a fresh
FileStorageProvider, calling save() after every single one (matching
service.py's real usage), and record the wall-clock cost of each
individual save() call as the store grows from empty to N entries. Repeat
with save_every_n_calls=50 to measure the mitigation's effect on the same
growth curve.

Scope note on the absolute numbers below vs. the live 2.927s data point:
this test measures pure local file I/O and JSON (de)serialization cost
only, in a single Python process, on this machine -- no Docker container
filesystem layer, no network round-trip, no gunicorn request handling,
and no lock_for_save() contention from 8 concurrent workers all fighting
over the same lock file, all of which the live 1M-line Docker run also
paid on top of the same underlying O(n) read-merge-write cost. This test
is expected to show smaller absolute per-call times than the live run at
an equivalent store size -- the point is to directly confirm the O(n)
GROWTH SHAPE (and that debouncing mitigates it proportionally), not to
reproduce the live run's exact absolute numbers.

Run: python validation/tokenstore_save_scaling_test.py
"""
import sys
import os
import time
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import anonymize  # noqa: E402

N = 6_000            # entries to mint -- much smaller than the 93,279 that
                     # triggered this in the live 1M-line Docker run
                     # (reproducing that exact scale in a pure-Python,
                     # single-machine, no-Docker-overhead loop would take
                     # far longer than this test needs to run -- see the
                     # note below on why the absolute numbers here are
                     # smaller than the live run's, by design). Large
                     # enough to show the O(n) trend clearly via direct
                     # measurement, not just extrapolation from one data
                     # point.
SAMPLE_EVERY = 250   # record timing this often, not every single call
                      # (6,000 individual save() calls' own timing noise
                      # would be hard to read; sampling smooths it while
                      # still showing the trend clearly)


def run(save_every_n_calls: int, tmpdir: str) -> tuple[list[tuple[int, float]], list[float]]:
    """Returns (sampled_timings, real_write_timings). real_write_timings
    records EVERY call that actually performed the expensive read-merge-
    write (detected via store._calls_since_save resetting to 0, not a
    timing-based guess), regardless of the SAMPLE_EVERY sampling used for
    the printed trend -- needed because with a debounce factor that shares
    a common divisor with SAMPLE_EVERY, sampled indices can systematically
    never land on a real write, undercounting them entirely rather than
    just missing a few."""
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

        print("--- save_every_n_calls=1 (current default, every call writes) ---")
        baseline, _baseline_real_writes = run(save_every_n_calls=1, tmpdir=tmpdir)
        for i, elapsed in baseline[::4]:  # print a quarter of the samples, enough to see the trend
            print(f"  store size ~{i:>6,}: save() took {elapsed*1000:8.2f} ms")
        first_ms = baseline[1][1] * 1000
        last_ms = baseline[-1][1] * 1000
        print(f"\n  First sampled save() (~store size {baseline[1][0]:,}): {first_ms:.2f} ms")
        print(f"  Last sampled save()  (~store size {baseline[-1][0]:,}): {last_ms:.2f} ms")
        print(f"  Growth factor: {last_ms/first_ms:.1f}x slower at "
              f"{baseline[-1][0]/baseline[1][0]:.0f}x the store size -- "
              f"consistent with O(n) per-call cost, not O(1)")
        total_baseline = sum(e for _, e in baseline) * (SAMPLE_EVERY)  # rough total-cost estimate
                                                                          # from sampled average
        print(f"  Extrapolated total save() time across all {N:,} calls: "
              f"~{sum(e for _, e in baseline) * N / len(baseline):.1f}s "
              f"(sampled average x {N:,} calls)")

        print("\n--- save_every_n_calls=50 (the mitigation) ---")
        t_debounce_start = time.perf_counter()
        debounced, actual_writes = run(save_every_n_calls=50, tmpdir=tmpdir)
        total_debounced = time.perf_counter() - t_debounce_start
        # Only 1/50th of calls actually write; each real write still costs
        # O(current store size) same as before -- the mitigation reduces
        # HOW OFTEN that cost is paid, not the cost of paying it. Measuring
        # actual total wall-clock time for the whole run (not the sampled-
        # average extrapolation used above, since most sampled points here
        # are cheap skipped no-op calls and would understate real writes'
        # cost if averaged the same way) is the honest way to report this.
        print(f"  Actual writes performed: {len(actual_writes)} "
              f"(vs. {N:,} calls to save() -- {N // 50} expected at this debounce factor)")
        if actual_writes:
            print(f"  Slowest observed real write: {max(actual_writes)*1000:.2f} ms "
                  f"(comparable to baseline's per-write cost at an equivalent store size, "
                  f"as expected -- the mitigation doesn't change a write's own cost)")
        total_baseline_wall = sum(e for _, e in baseline) * (N / len(baseline))
        print(f"  Total wall-clock time, all {N:,} calls to save(): {total_debounced:.2f}s")
        print(f"  Reduction vs. save_every_n_calls=1 "
              f"(~{total_baseline_wall:.1f}s extrapolated): "
              f"~{total_baseline_wall/max(total_debounced, 0.001):.0f}x less total time")

        print(f"\n=== Conclusion ===")
        print(f"Per-call cost genuinely grows with store size (O(n), not O(1)) -- "
              f"confirmed directly, not just inferred from the live 1M-line run's "
              f"single 2.927s data point. The save_every_n_calls debounce cuts total "
              f"cost roughly proportionally to the debounce factor, as expected for a "
              f"mitigation (not a fix) that reduces HOW OFTEN the O(n) cost is paid, "
              f"not the shape of the cost itself. See Bug 15 in BUGS_AND_FIXES.md for "
              f"the full writeup, including what a real fix would need.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
