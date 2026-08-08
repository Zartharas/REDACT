"""
Verifies RedisStorageProvider (src/anonymize.py) against a live Redis
instance -- the verification ROADMAP.md item 6 explicitly says hasn't been
done yet. Requires a reachable Redis (see the docker run command in this
script's own comment below) and the `redis` package (pip install -r
requirements-redis.txt).

Run:
    docker run -d --rm -p 6379:6379 --name redact-test-redis redis:7
    pip install -r requirements-redis.txt
    python validation/redis_storage_provider_test.py
    docker stop redact-test-redis
"""
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import anonymize  # noqa: E402


def main():
    provider = anonymize.RedisStorageProvider(redis_url="redis://localhost:6379/0",
                                               key_prefix="redact:test")
    store = anonymize.TokenStore(provider, token_key="testkey")

    print("=== Basic round trip ===")
    tok1 = store.get_or_create_token("Jane Doe", "PERSON")
    tok2 = store.get_or_create_token("Jane Doe", "PERSON")
    assert tok1 == tok2, "same input should yield same token"
    assert store.resolve(tok1) == "Jane Doe"
    store.save()
    print("PASS: token generation and resolve")

    print("\n=== Persistence across a fresh TokenStore/provider instance ===")
    provider2 = anonymize.RedisStorageProvider(redis_url="redis://localhost:6379/0",
                                                key_prefix="redact:test")
    store2 = anonymize.TokenStore(provider2, token_key="testkey")
    assert store2.resolve(tok1) == "Jane Doe", "reload from Redis should recover mapping"
    print("PASS: persistence round-trip through Redis")

    print("\n=== tokenize/detokenize round trip ===")
    text = "user Jane Doe logged in"
    spans = [{"start": 5, "end": 13, "type": "PERSON"}]
    anon = anonymize.tokenize(text, spans, store)
    assert "Jane Doe" not in anon
    back = anonymize.detokenize(anon, store)
    assert back == text, f"round trip failed: {back!r}"
    print("PASS: tokenize/detokenize round trip")

    print("\n=== Concurrent access (the same class of test that originally found "
          "the TokenStore race condition, Bug 6) ===")
    errors = []

    def worker(n):
        try:
            for i in range(200):
                store.get_or_create_token(f"concurrent-user-{n}-{i}", "PERSON")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        print(f"FAIL: {len(errors)} errors during concurrent access, first: {errors[0]!r}")
        sys.exit(1)
    print("PASS: 8 threads x 200 get_or_create_token() calls, zero errors")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
