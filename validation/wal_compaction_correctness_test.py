"""
Correctness check for FileStorageProvider's WAL-based incremental-write
path (the real Bug 15 fix, BUGS_AND_FIXES.md, added 2026-08-08) --
specifically across compact() boundaries, which is the one part of the
new design NOT exercised by validation/multiprocess_tokenstore_test.py
(that test runs 8 processes x 50 tokens each = 400 total, well under the
default wal_compact_threshold_lines=200 batches needed to trigger even
one compaction there) or validation/tokenstore_save_scaling_test.py
(measures timing, doesn't specifically assert correctness across a
compaction).

What this proves, single-process, no Docker needed:
1. Every token minted across many compaction cycles is still resolvable
   via detokenize()/resolve() after the fact -- compact() folding the WAL
   into the snapshot and truncating it must not lose any entry.
2. A FRESH TokenStore pointed at the same path (simulating a process
   restart, or a second gunicorn worker reading state a sibling wrote)
   sees the exact same complete set of tokens, whether they came from the
   compacted snapshot or an uncompacted tail still sitting in the WAL --
   proving FileStorageProvider.load()'s snapshot+WAL-replay logic is
   correct, not just that data isn't visibly lost within one process's
   own lifetime.
3. The WAL file itself never grows past wal_compact_threshold_lines lines
   for long, confirming compact() is actually firing on the configured
   cadence rather than silently never running (which would just move
   Bug 15's O(n) problem from the snapshot to an ever-growing WAL instead
   of fixing it).

Run: python validation/wal_compaction_correctness_test.py
"""
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import anonymize  # noqa: E402

N = 5_000
COMPACT_THRESHOLD = 50  # small on purpose -- forces many compactions
                          # within N=5,000 tokens (~100 of them, one every
                          # COMPACT_THRESHOLD save() calls at
                          # save_every_n_calls=1), instead of the
                          # production default of 200, so this test
                          # actually exercises compact() repeatedly rather
                          # than relying on getting lucky with N large
                          # enough to cross the default threshold a
                          # handful of times.


def main():
    tmpdir = tempfile.mkdtemp(prefix="redact_wal_correctness_test_")
    try:
        store_path = os.path.join(tmpdir, "token_store.json")
        provider = anonymize.FileStorageProvider(
            store_path, wal_compact_threshold_lines=COMPACT_THRESHOLD
        )
        store = anonymize.TokenStore(provider, token_key="wal-correctness-test-key")

        minted = {}  # original -> token, ground truth
        max_wal_lines_seen = 0
        for i in range(N):
            original = f"user{i}@example.com"
            token = store.get_or_create_token(original, "EMAIL")
            minted[original] = token
            store.save()
            wal_lines = provider._wal_line_count()
            max_wal_lines_seen = max(max_wal_lines_seen, wal_lines)
        store.save(force=True)

        print(f"=== WAL compaction correctness test: {N:,} tokens, "
              f"wal_compact_threshold_lines={COMPACT_THRESHOLD} ===\n")

        # Check 1: every minted token still resolves within the SAME process.
        lost_same_process = 0
        for original, token in minted.items():
            if store.resolve(token) != original:
                lost_same_process += 1
        print(f"Check 1 (same-process resolve()): {len(minted) - lost_same_process:,}/"
              f"{len(minted):,} tokens resolve correctly.")

        # Check 2: a FRESH TokenStore reading the same path/provider sees
        # everything -- proves load()'s snapshot+WAL-replay is correct,
        # not just that the original in-memory dicts happened to still be
        # right.
        fresh_provider = anonymize.FileStorageProvider(
            store_path, wal_compact_threshold_lines=COMPACT_THRESHOLD
        )
        fresh_store = anonymize.TokenStore(fresh_provider, token_key="wal-correctness-test-key")
        lost_fresh = 0
        for original, token in minted.items():
            if fresh_store.resolve(token) != original:
                lost_fresh += 1
        print(f"Check 2 (fresh-process resolve() after simulated restart): "
              f"{len(minted) - lost_fresh:,}/{len(minted):,} tokens resolve correctly.")

        # Check 3: WAL was actually being compacted, not left to grow
        # unboundedly -- confirms compact() fired on the configured
        # cadence rather than this test accidentally never crossing the
        # threshold.
        final_wal_lines = provider._wal_line_count()
        print(f"Check 3 (WAL bounded): max WAL line count observed during the run: "
              f"{max_wal_lines_seen} (threshold: {COMPACT_THRESHOLD}); "
              f"final WAL line count after the run: {final_wal_lines}.")
        wal_bounded = max_wal_lines_seen <= COMPACT_THRESHOLD

        print()
        overall_pass = (lost_same_process == 0 and lost_fresh == 0 and wal_bounded)
        if overall_pass:
            print("RESULT: PASS. Zero tokens lost across same-process and "
                  "fresh-process resolution, and the WAL never grew past its "
                  "configured compaction threshold -- compact() is firing "
                  "correctly and no data is lost when it does.")
        else:
            print("RESULT: FAIL.")
            if lost_same_process:
                print(f"  - {lost_same_process} tokens lost within the same process.")
            if lost_fresh:
                print(f"  - {lost_fresh} tokens lost when read by a fresh TokenStore/provider.")
            if not wal_bounded:
                print(f"  - WAL grew to {max_wal_lines_seen} lines, past its "
                      f"{COMPACT_THRESHOLD}-line threshold -- compact() did not fire "
                      f"as expected.")
        sys.exit(0 if overall_pass else 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
