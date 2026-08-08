"""
ROADMAP item 6 follow-on -- the Redis counterpart to
validation/multiprocess_tokenstore_test.py. That script proved (entirely in
a plain Python environment, no Docker needed) that TokenStore's
FileStorageProvider backend lost reverse-map entries under real
multi-process concurrent load, then confirmed the fix (read-merge-write in
TokenStore.save() plus a real cross-process lock,
FileStorageProvider.lock_for_save() using fcntl.flock) brought that down to
zero. RedisStorageProvider.lock_for_save() applies the same fix using a
single-node Redis distributed lock (SET NX PX to acquire, a Lua
delete-if-still-owner script to release) instead of fcntl -- this script
confirms that holds against a REAL Redis instance and REAL separate OS
processes, not just the file-backend logic.

validation/redis_storage_provider_test.py (written earlier, ROADMAP item 6)
only ever tested concurrency with multiple THREADS in one process. That
never exercised the actual production topology this provider exists for --
multiple separate redact-service processes (gunicorn workers, or separate
replicas) sharing one Redis backend -- which is exactly the gap this script
closes, mirroring multiprocess_tokenstore_test.py's real multi-OS-process
structure instead.

Requires a reachable Redis and the `redis` package:
    docker run -d --rm -p 6379:6379 --name redact-test-redis redis:7
    pip install -r requirements-redis.txt
    python validation/multiprocess_redis_test.py
    docker stop redact-test-redis
"""
import sys
import os
import multiprocessing

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import anonymize  # noqa: E402

N_WORKERS = 8
TOKENS_PER_WORKER = 50
REDIS_URL = "redis://localhost:6379/0"
KEY_PREFIX = "redact:mp-test"


def worker(worker_id: int, n_tokens: int, result_queue):
    try:
        provider = anonymize.RedisStorageProvider(redis_url=REDIS_URL, key_prefix=KEY_PREFIX)
        store = anonymize.TokenStore(provider, token_key="test-key")
        minted = []
        for i in range(n_tokens):
            original = f"worker{worker_id}-value{i}@example.com"
            token = store.get_or_create_token(original, "EMAIL")
            store.save()
            minted.append((original, token))
        result_queue.put(("ok", minted))
    except BaseException as e:  # noqa: BLE001
        result_queue.put(("error", f"worker {worker_id}: {type(e).__name__}: {e}"))


def clear_redis_state():
    provider = anonymize.RedisStorageProvider(redis_url=REDIS_URL, key_prefix=KEY_PREFIX)
    provider._client.delete(provider._forward_key, provider._reverse_key, provider._lock_key)


def main():
    print(f"=== Multi-process Redis TokenStore test: {N_WORKERS} processes x "
          f"{TOKENS_PER_WORKER} tokens each, save() after every token ===\n")
    print(f"Connecting to {REDIS_URL}, key prefix {KEY_PREFIX!r}...")
    clear_redis_state()
    print("Cleared any prior test state.\n")

    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(w, TOKENS_PER_WORKER, result_queue))
             for w in range(N_WORKERS)]
    for p in procs:
        p.start()

    all_minted = []
    errors = []
    for _ in procs:
        try:
            kind, payload = result_queue.get(timeout=60)
        except Exception:
            errors.append("timed out waiting for a worker result")
            continue
        if kind == "error":
            errors.append(payload)
        else:
            all_minted.extend(payload)
    for p in procs:
        p.join(timeout=15)
        if p.is_alive():
            errors.append("a worker process did not exit within 15s -- terminating it")
            p.terminate()

    if errors:
        print("WORKER ERRORS:")
        for e in errors:
            print(f"  {e}")
        print()

    provider = anonymize.RedisStorageProvider(redis_url=REDIS_URL, key_prefix=KEY_PREFIX)
    final_store = anonymize.TokenStore(provider, token_key="test-key")
    lost = []
    for original, token in all_minted:
        recovered = final_store.resolve(token)
        if recovered != original:
            lost.append((original, token, recovered))

    print(f"Total tokens minted across all workers: {len(all_minted)}")
    print(f"Reverse-map entries lost (unrecoverable via resolve()): {len(lost)}")
    if lost:
        print(f"Loss rate: {len(lost)/len(all_minted):.1%}")
        for original, token, recovered in lost[:5]:
            print(f"  {original!r} -> resolve() returned {recovered!r}")
        print("\nRESULT: DATA LOSS against real Redis -- lock_for_save()'s "
              "Redis lock did not close the race the way the file-backend "
              "test showed it should. Needs investigation before trusting "
              "RedisStorageProvider under concurrent load.")
        sys.exit(1)
    else:
        print("\nRESULT: PASS. Zero reverse-map entries lost against a real "
              "Redis instance under real multi-process concurrent load.")
        sys.exit(0)


if __name__ == "__main__":
    main()
