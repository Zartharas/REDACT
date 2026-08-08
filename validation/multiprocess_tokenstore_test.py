"""
ROADMAP item 6 follow-on. RedisStorageProvider was verified for
single-process thread safety (validation/redis_storage_provider_test.py)
but never for the actual production topology it exists for: multiple
SEPARATE OS processes (redact-service replicas, or -- as this test found --
gunicorn's own default multi-worker model within ONE container) sharing one
persistence backend concurrently. TokenStore's own docstring already names
this gap explicitly: "The lock only fixes in-process thread-safety -- it
says nothing about a multi-instance production deployment."

This test proves, empirically and without needing Docker or Redis, that the
gap was real: TokenStore.save() (src/anonymize.py) loaded persisted state
ONCE at construction and, on every save(), blindly overwrote the backend
with whatever it had accumulated in its own local memory -- a "last writer
wins, full clobber" pattern. Under service.py's real usage (_store.save()
called after EVERY /anonymize request, one TokenStore instance per gunicorn
worker process, all workers sharing one token_store.json file via
docker-compose.yml's redact-output volume) this isn't a rare race -- it's
close to guaranteed data loss under any real concurrent load, since a
worker's save() after processing request N would silently erase any
reverse-map entries a sibling worker had written in between.

The practical harm: tokenize()'s core promise ("Exact original value can be
recovered by anyone with access to `store`" -- anonymize.py's own module
docstring) silently fails for any reverse-map entry a concurrent worker's
save() clobbered away. This is the same "silently wrong, no crash, no
error" shape as bugs 1, 4, 5, 7, and 12 in BUGS_AND_FIXES.md, just never
exercised by any test in this project before now -- validate.py's own
tokenize/detokenize check (Section 3) only ever runs single-process,
sequentially.

FileStorageProvider is used here deliberately, not RedisStorageProvider --
the defect being tested is in TokenStore.save()'s own load-once/overwrite
logic, which is provider-agnostic (Redis has exactly the same defect, see
RedisStorageProvider.save()'s delete-then-hset pattern). Testing against a
shared JSON file lets this run entirely in a plain Python environment, no
Docker or Redis needed; validation/redis_storage_provider_test.py remains
the place to confirm the same fix holds against the real intended
production backend.

Run: python validation/multiprocess_tokenstore_test.py
"""
import sys
import os
import json
import multiprocessing
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import anonymize  # noqa: E402


N_WORKERS = 8          # matches this project's own gunicorn --workers $(nproc)
                        # convention on an 8-core-ish machine
TOKENS_PER_WORKER = 50  # each worker mints this many NEW, worker-unique
                         # tokens, saving after every single one -- mirroring
                         # service.py's real "save() after every request"
                         # pattern exactly, not a synthetic stress pattern


def worker(store_path: str, worker_id: int, n_tokens: int, result_queue):
    """Mirrors service.py's real usage: one TokenStore per process (module-
    level in service.py, here just constructed once at the top of the
    worker function), .save() called after every single minted token, no
    coordination with sibling workers beyond the shared file itself.

    Wrapped in a broad try/except and always puts SOMETHING on the queue
    (a result tuple or an error marker), never lets an exception silently
    kill the worker without the parent knowing -- found necessary while
    building this test: the pre-fix version of FileStorageProvider.save()
    (see BUGS_AND_FIXES.md) could crash a worker with json.JSONDecodeError
    on a torn concurrent read, and the parent's queue.get() would then
    hang forever waiting for a result that was never coming, with no
    indication of why. Whatever this test's own bugs turn out to be in
    the future, they should fail loudly, not hang silently -- the same
    principle this entire project applies to the pipeline it's testing.
    """
    try:
        store = anonymize.TokenStore(store_path, token_key="test-key")
        minted = []
        for i in range(n_tokens):
            original = f"worker{worker_id}-value{i}@example.com"
            token = store.get_or_create_token(original, "EMAIL")
            store.save()
            minted.append((original, token))
        result_queue.put(("ok", minted))
    except BaseException as e:  # noqa: BLE001
        result_queue.put(("error", f"worker {worker_id}: {type(e).__name__}: {e}"))


def run_test(store_path: str) -> tuple[int, list]:
    """Returns (total_minted, lost_entries)."""
    if os.path.exists(store_path):
        os.remove(store_path)

    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    procs = []
    for w in range(N_WORKERS):
        p = ctx.Process(target=worker, args=(store_path, w, TOKENS_PER_WORKER, result_queue))
        procs.append(p)
        p.start()

    all_minted = []
    errors = []
    for _ in procs:
        # A hard timeout here, not an unbounded block: if a worker dies
        # without ever reaching its own try/except's put() call (e.g.
        # killed by a signal), this must still fail loudly and fast
        # rather than hang the whole test the way the original version of
        # this script did when it first found the pre-fix crash bug.
        try:
            kind, payload = result_queue.get(timeout=30)
        except Exception:
            errors.append("timed out waiting for a worker result (worker may have "
                           "died without reporting -- check for a killed/segfaulted "
                           "process)")
            continue
        if kind == "error":
            errors.append(payload)
        else:
            all_minted.extend(payload)
    for p in procs:
        p.join(timeout=10)
        if p.is_alive():
            errors.append(f"a worker process did not exit within 10s after "
                           f"reporting its result -- terminating it")
            p.terminate()

    if errors:
        print("WORKER ERRORS:")
        for e in errors:
            print(f"  {e}")
        print()

    # Fresh read of whatever survived on disk after all workers finished.
    final_store = anonymize.TokenStore(store_path, token_key="test-key")
    lost = []
    for original, token in all_minted:
        recovered = final_store.resolve(token)
        if recovered != original:
            lost.append((original, token, recovered))

    return len(all_minted), lost


def main():
    tmpdir = tempfile.mkdtemp(prefix="redact_mp_test_")
    store_path = os.path.join(tmpdir, "token_store.json")
    try:
        print(f"=== Multi-process TokenStore test: {N_WORKERS} processes x "
              f"{TOKENS_PER_WORKER} tokens each, save() after every token ===\n")
        total, lost = run_test(store_path)
        lost_count = len(lost)
        print(f"Total tokens minted across all workers: {total}")
        print(f"Reverse-map entries lost (unrecoverable via resolve()): {lost_count}")
        if lost_count:
            print(f"Loss rate: {lost_count/total:.1%}")
            print("\nExample lost entries (original value that should be "
                  "detokenize()-able but isn't):")
            for original, token, recovered in lost[:5]:
                print(f"  {original!r} -> token {token[:20]}... -> resolve() "
                      f"returned {recovered!r} (expected {original!r})")
            print("\nRESULT: DATA LOSS CONFIRMED. TokenStore.save()'s "
                  "blind-overwrite pattern loses reverse-map entries under "
                  "real concurrent multi-process load -- see this script's "
                  "own docstring and BUGS_AND_FIXES.md for the fix.")
            sys.exit(1)
        else:
            print("\nRESULT: PASS. Zero reverse-map entries lost -- every "
                  "token minted by any worker resolves correctly after all "
                  "workers finished.")
            sys.exit(0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
