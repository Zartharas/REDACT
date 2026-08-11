"""
Anonymization actions applied to spans found by the detection ensemble
(src/detect.py). Three actions, matching the decision matrix in the chapter:

- redact(text, spans):        irreversible, fixed placeholder. No key, no map,
                               nothing to reverse. Use for fields with no
                               analytic value.
- pseudonymize(text, spans, key): ONE-WAY. HMAC-SHA256 is a cryptographic
                               hash function, not a cipher -- there is no
                               operation that recovers the original value
                               from the token, with or without the key. What
                               the key buys you is determinism: the same
                               input always produces the same output within
                               a key epoch, so correlation across events
                               survives, and an investigator holding the key
                               can VERIFY whether a candidate value matches a
                               given token (recompute the HMAC of the
                               candidate and compare), which is only useful
                               if the candidate space is small enough to
                               search. This is NOT the same as reversibility.
                               Do not use this for any field where an
                               investigator will need to recover the actual
                               original value later -- use tokenize() for
                               that instead.
- tokenize(text, spans, store): genuinely reversible, via a lookup table
                               rather than a key. Exact original value can
                               be recovered by anyone with access to `store`.
                               This is the only one of the three actions
                               below that supports true reversal.

All three operate on the same span format the detector produces:
{"start": int, "end": int, "type": str}. Spans are applied right-to-left so
earlier offsets in the same string stay valid as the string is edited.
"""
import hashlib
import hmac
import json
import os
import tempfile
import threading
from abc import ABC, abstractmethod

try:
    import fcntl  # POSIX only -- this project's only supported runtime
                   # target is Linux (Docker), see docker-compose.yml and
                   # BUGS_AND_FIXES.md throughout. Guarded with a try/except
                   # rather than a hard import so the module still loads
                   # (minus cross-process file locking) on a non-POSIX dev
                   # machine running validate.py locally outside Docker.
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover -- not exercised in this project's
                       # own Linux-only CI/Docker environment
    _HAVE_FCNTL = False


class _FlockContext:
    """Blocking exclusive fcntl.flock on a sibling lock file, used by
    FileStorageProvider.lock_for_save() to serialize the load-merge-save
    critical section across separate OS processes (see that method's own
    comment, and TokenStore.save(), for why this is needed on top of the
    in-process threading.Lock() TokenStore already has). Falls back to a
    no-op if fcntl isn't available (see the import guard above) -- on a
    platform without it, TokenStore.save()'s read-merge-write logic alone
    still applies (reduces, doesn't eliminate, cross-process data loss;
    see BUGS_AND_FIXES.md for the measured before/after)."""

    def __init__(self, lock_path: str):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        if not _HAVE_FCNTL:
            return self
        # Opened in "a" (append) mode rather than "w": never truncates an
        # existing lock file, and creates it if missing. The lock file's
        # own contents are never read or written meaningfully -- it exists
        # only to be flock()'d, matching the standard POSIX advisory-lock-
        # via-sentinel-file pattern.
        self._fh = open(self._lock_path, "a")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


class _RedisLockContext:
    """Simple single-node Redis distributed lock (SET NX PX to acquire, a
    Lua script that only deletes-if-still-owner to release), used by
    RedisStorageProvider.lock_for_save() -- see that method's own comment
    for why this is scoped to single-node correctness rather than
    implementing the full Redlock algorithm."""

    _RELEASE_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """

    def __init__(self, client, lock_key: str, ttl_ms: int = 10_000,
                 acquire_timeout_s: float = 15.0):
        import uuid
        self._client = client
        self._lock_key = lock_key
        self._ttl_ms = ttl_ms
        self._acquire_timeout_s = acquire_timeout_s
        self._owner_token = uuid.uuid4().hex

    def __enter__(self):
        import time
        deadline = time.monotonic() + self._acquire_timeout_s
        delay = 0.01
        while True:
            if self._client.set(self._lock_key, self._owner_token, nx=True, px=self._ttl_ms):
                return self
            if time.monotonic() >= deadline:
                # Fail loudly rather than hang forever or silently proceed
                # without the lock -- proceeding unlocked would silently
                # reintroduce exactly the race this lock exists to close.
                raise TimeoutError(
                    f"could not acquire Redis save-lock {self._lock_key!r} "
                    f"within {self._acquire_timeout_s}s -- another process "
                    f"may be stuck holding it, or Redis is unreachable"
                )
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)  # capped exponential backoff

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._client.eval(self._RELEASE_SCRIPT, 1, self._lock_key, self._owner_token)
        return False


class _VaultLockContext:
    """Optimistic-concurrency lock built on Vault KV v2's own CAS (check-
    and-set) primitive, used by VaultStorageProvider.lock_for_save() --
    the Vault-side counterpart to _FlockContext (file) and
    _RedisLockContext (Redis) above, serving the same purpose: closing
    the gap between one process's load() and its subsequent save() (see
    StorageProvider.lock_for_save()'s own docstring).

    Vault's KV v2 secrets engine has no native TTL/expiry primitive for
    ordinary key-value writes the way Redis's SET ... PX does (Vault's
    lease/TTL system belongs to its DYNAMIC secrets engines -- database
    credentials, PKI certificates -- not plain KV data). This
    implementation compensates with an explicit staleness check instead:
    the lock record stores its own acquisition timestamp, and a lock
    older than ttl_s is treated as abandoned (a crashed holder) and
    force-acquired by the next caller, rather than blocking forever.
    Acquisition uses Vault's `cas` parameter -- create_or_update_secret
    only succeeds if the secret's CURRENT version matches the version
    passed in -- which is what makes "check it's free, then take it"
    atomic against a concurrent process doing the identical check at the
    identical moment; without CAS, two processes could both read
    "unlocked" and both believe they'd acquired it.

    NOT verified against a live Vault instance in this environment (see
    VaultStorageProvider's own class docstring for why) -- implemented
    and reasoned through against Vault's documented KV v2 CAS semantics,
    and exercised here with a mocked hvac client
    (tests/test_vault_storage_provider.py), not confirmed against a real
    Vault server's actual behavior.
    """

    def __init__(self, client, mount_point: str, lock_path: str,
                 ttl_s: float = 10.0, acquire_timeout_s: float = 15.0):
        import uuid
        self._client = client
        self._mount_point = mount_point
        self._lock_path = lock_path
        self._ttl_s = ttl_s
        self._acquire_timeout_s = acquire_timeout_s
        self._owner_token = uuid.uuid4().hex

    def _read_lock(self):
        import hvac.exceptions
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=self._lock_path, mount_point=self._mount_point,
            )
            return resp["data"]["data"], resp["data"]["metadata"]["version"]
        except hvac.exceptions.InvalidPath:
            return None, 0

    def __enter__(self):
        import time
        deadline = time.monotonic() + self._acquire_timeout_s
        delay = 0.05
        while True:
            data, version = self._read_lock()
            is_free = (
                not data
                or not data.get("owner")
                or (time.time() - float(data.get("acquired_at", 0))) > self._ttl_s
            )
            if is_free:
                try:
                    self._client.secrets.kv.v2.create_or_update_secret(
                        path=self._lock_path,
                        secret={"owner": self._owner_token, "acquired_at": time.time()},
                        mount_point=self._mount_point,
                        cas=version,
                    )
                    return self
                except Exception:
                    # Lost the race against a concurrent acquirer's own CAS
                    # write between our read above and this write -- retry
                    # from the top rather than assuming we hold the lock.
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not acquire Vault save-lock {self._lock_path!r} "
                    f"within {self._acquire_timeout_s}s -- another process "
                    f"may be stuck holding it, or Vault is unreachable"
                )
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)  # capped exponential backoff

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Only release if this context still appears to hold it -- same
        # "release only your own acquisition" principle
        # _RedisLockContext's Lua release script enforces via an owner-
        # token comparison. A lock we lost to another process's
        # staleness-triggered force-acquisition should not be clobbered
        # by our own late release.
        data, version = self._read_lock()
        if data and data.get("owner") == self._owner_token:
            try:
                self._client.secrets.kv.v2.create_or_update_secret(
                    path=self._lock_path,
                    secret={"owner": None, "acquired_at": 0},
                    mount_point=self._mount_point,
                    cas=version,
                )
            except Exception:
                # Best-effort release. If this fails (e.g. a concurrent
                # staleness-triggered force-acquisition landed between our
                # read and this write), the staleness check on the NEXT
                # acquire attempt is what actually protects correctness,
                # not this release -- so a failed release here is not a
                # correctness bug, only a lock that stays held slightly
                # longer than necessary.
                pass
        return False


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def dedup_spans(spans: list[dict]) -> list[dict]:
    """Collapse overlapping spans of the same type down to one, keeping the
    first occurrence. Detection layers (regex, NER) frequently agree on the
    same span -- that agreement should not cause it to be transformed twice.
    Overlapping spans of *different* types (a genuine detector disagreement)
    are left as-is and will surface as a collision when applied; callers
    should treat that case as a signal to route to manual review rather than
    silently pick one, which is why this function does not resolve it."""
    ordered = sorted(spans, key=lambda s: s["start"])
    kept: list[dict] = []
    for span in ordered:
        if any(span["type"] == k["type"] and _overlaps(span, k) for k in kept):
            continue
        kept.append(span)
    return kept


def _apply_right_to_left(text: str, spans: list[dict], transform) -> str:
    spans = dedup_spans(spans)
    ordered = sorted(spans, key=lambda s: s["start"], reverse=True)
    out = text
    for span in ordered:
        original = out[span["start"]:span["end"]]
        replacement = transform(original, span)
        out = out[:span["start"]] + replacement + out[span["end"]:]
    return out


def redact(text: str, spans: list[dict], placeholder: str = "[REDACTED]") -> str:
    return _apply_right_to_left(text, spans, lambda original, span: placeholder)


def pseudonymize(text: str, spans: list[dict], key: str, token_len: int = 32) -> str:
    def transform(original: str, span: dict) -> str:
        digest = hmac.new(key.encode(), original.encode(), hashlib.sha256).hexdigest()
        prefix = span["type"].lower()[:4]
        return f"{prefix}_{digest[:token_len]}"
    return _apply_right_to_left(text, spans, transform)


class StorageProvider(ABC):
    """Persistence backend for TokenStore's forward/reverse mapping.

    TokenStore owns the business logic (token generation, the in-memory
    dicts, the thread lock); a StorageProvider only owns getting those two
    dicts to and from durable storage. This split exists because the flat
    JSON file (FileStorageProvider, TokenStore's original and still-default
    behavior) was always explicitly documented as a demo/local-dev stand-in,
    not a production secrets store -- see FileStorageProvider's own
    docstring. Swapping in RedisStorageProvider (or a future
    HashiCorpVaultStorageProvider) should not require touching
    get_or_create_token, resolve, or anything in TokenStore's threading
    model.
    """

    @abstractmethod
    def load(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (forward, reverse) dicts. Empty dicts if nothing stored yet."""
        raise NotImplementedError

    @abstractmethod
    def save(self, forward: dict[str, str], reverse: dict[str, str]) -> None:
        """Persist the full forward/reverse mapping. Called after every new
        token is minted (see TokenStore.save()), so this needs to be cheap
        enough to call frequently -- not necessarily on every single
        get_or_create_token() call, but on whatever cadence the caller
        chooses to call TokenStore.save()."""
        raise NotImplementedError

    def save_incremental(self, new_forward: dict[str, str], new_reverse: dict[str, str]) -> bool:
        """Persist ONLY the entries minted since the last successful save
        (not the full accumulated store) -- the real fix for Bug 15
        (BUGS_AND_FIXES.md), added 2026-08-08. `save()` above is O(total
        store size) by construction (it takes the full dicts and has no way
        to know what's new); this method exists so a provider CAN offer an
        O(batch size) path instead, when its backend supports partial
        writes.

        Default implementation returns False, meaning "not supported by
        this provider" -- TokenStore.save() falls back to the old
        read-merge-write via save() above for any provider that doesn't
        override this (e.g. a future Vault provider that hasn't
        implemented it yet). A provider that overrides this and returns
        True is asserting it fully persisted new_forward/new_reverse;
        TokenStore trusts that and does not additionally call save().

        `new_forward`/`new_reverse` contain ONLY newly-minted entries since
        this TokenStore's last successful persist, not the full store --
        callers must not assume they can reconstruct total store state from
        this method's arguments."""
        return False

    def lock_for_save(self):
        """Context manager serializing TokenStore.save()'s load-merge-save
        critical section ACROSS PROCESSES, not just across threads in one
        process (self._lock in TokenStore already handles that). Added
        2026-08-08 after validation/multiprocess_tokenstore_test.py showed
        read-merge-write alone (see TokenStore.save()) cuts data loss under
        real concurrent multi-process load substantially but does not
        eliminate it -- two processes' save() calls can still race within
        the gap between one process's own load() and its subsequent
        save(). Default implementation is a no-op (nullcontext): providers
        for a backend with no natural locking primitive get read-merge-
        write's improvement without a hard correctness guarantee, and
        should override this if that's not good enough for their
        deployment. FileStorageProvider and RedisStorageProvider below
        both override it with a real lock, closing the race properly."""
        from contextlib import nullcontext
        return nullcontext()


class FileStorageProvider(StorageProvider):
    """Flat-JSON persistence. This is the open-source, zero-budget stand-in
    for a proper secrets store (HashiCorp Vault, AWS Secrets Manager) --
    explicitly NOT an acceptable production token store on its own (see the
    limitations note at the bottom of this file): no access control beyond
    filesystem permissions, no encryption at rest, no key rotation, and a
    full-file rewrite on every save() rather than an incremental write. It
    is enough to demonstrate and evaluate the tokenize/detokenize round trip
    honestly, and remains the default for local dev and testing (including
    everything in validate.py and evaluate.py) since it needs no external
    service running."""

    def __init__(self, path: str, wal_compact_threshold_lines: int = 200):
        self.path = path
        # Append-only write-ahead log: this is what actually fixes Bug 15
        # (BUGS_AND_FIXES.md) for this provider, added 2026-08-08. save() above is O(total store
        # size) because it rewrites the whole JSON snapshot on every call;
        # save_incremental() below instead appends ONLY the new entries as
        # one JSON line, an O(batch size) operation regardless of how large
        # the snapshot has grown. load() reads the snapshot, then replays
        # any WAL lines on top of it, so nothing is lost between
        # compactions. wal_compact_threshold_lines bounds how large the WAL
        # is allowed to grow before save_incremental() folds it back into
        # the snapshot and truncates it (see compact()) -- without this,
        # load()'s own cost would grow unboundedly with the WAL instead of
        # the snapshot, just moving Bug 15's problem rather than fixing it.
        self.wal_path = path + ".wal"
        self._wal_compact_threshold_lines = wal_compact_threshold_lines

    def lock_for_save(self):
        # A sibling ".lock" file, held with an exclusive POSIX advisory
        # lock (fcntl.flock) for the duration of TokenStore.save()'s
        # load-merge-save critical section. Advisory, not mandatory --
        # only cooperating code that also calls lock_for_save() is
        # protected, which is fine here since TokenStore is the only
        # caller of load()/save() in this codebase. flock() blocks until
        # acquired rather than failing fast, which is the right default
        # for save() calls that are individually fast (see
        # BUGS_AND_FIXES.md for measured timings) -- a process waiting
        # briefly for the lock is far preferable to the data loss this
        # lock exists to close.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        return _FlockContext(self.path + ".lock")

    def load(self) -> tuple[dict[str, str], dict[str, str]]:
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
                forward, reverse = data.get("forward", {}), data.get("reverse", {})
        else:
            forward, reverse = {}, {}
        # Replay the WAL on top of the snapshot -- entries appended by
        # save_incremental() since the last compact() aren't in the
        # snapshot file yet, only in the WAL. Corrupt/partial trailing
        # lines (e.g. a process killed mid-append) are skipped rather than
        # raising: an append is not atomic the way FileStorageProvider's
        # snapshot rename is (see save()'s own comment on why the snapshot
        # write uses write-temp-then-rename), so a truncated last line is a
        # real possibility this needs to tolerate, at the cost of losing at
        # most that one incomplete batch -- the same bounded crash-window
        # tradeoff already documented for the save_every_n_calls debounce
        # (Bug 15, BUGS_AND_FIXES.md), not a new one.
        if os.path.exists(self.wal_path):
            with open(self.wal_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    forward.update(entry.get("f", {}))
                    reverse.update(entry.get("r", {}))
        return forward, reverse

    def save(self, forward: dict[str, str], reverse: dict[str, str]) -> None:
        # Write-to-temp-then-rename, not a direct open(path, "w"). This came
        # out of debugging a real failure (ROADMAP item 6 follow-on,
        # validation/multiprocess_tokenstore_test.py): opening the real
        # path directly in "w" mode truncates it immediately, before
        # json.dump() has written anything back -- a concurrent process's
        # load() landing in that window reads a truncated, invalid file
        # and crashes with json.JSONDecodeError. Confirmed live: a
        # multi-process test with service.py's real usage pattern
        # (TokenStore.save() called after every request, one instance per
        # gunicorn worker, all sharing one token_store.json via the
        # redact-output volume) crashed a worker this way within a
        # handful of concurrent saves. os.replace() is atomic on POSIX
        # (and on Windows since Python 3.3) as long as the temp file is on
        # the same filesystem as the destination -- writing it into the
        # same directory as self.path guarantees that.
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".token_store_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"forward": forward, "reverse": reverse}, f)
            os.replace(tmp_path, self.path)
        except BaseException:
            # Clean up the temp file on any failure so a crashed save()
            # doesn't leave stray .token_store_*.tmp files behind; the
            # real self.path is untouched either way since os.replace()
            # never ran.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def save_incremental(self, new_forward: dict[str, str], new_reverse: dict[str, str]) -> bool:
        # Bug 15's actual fix (BUGS_AND_FIXES.md) for this provider: append
        # only the new batch to the WAL (O(batch size)) instead of
        # rewriting the entire snapshot (O(total store size), what save()
        # above does on every call). Still guarded by lock_for_save() --
        # unlike RedisStorageProvider's per-key HSET, a plain file append
        # from two processes at once can interleave mid-line if the batch
        # is larger than the OS's atomic-write guarantee (PIPE_BUF, 4KB on
        # Linux), corrupting that line for both -- but the critical section
        # here is bounded by batch size, not store size, so the lock is
        # held for a short, constant-ish duration regardless of how large
        # the store has grown, which is the actual fix: the lock itself was
        # never the O(n) cost, the read-merge-write of the FULL store while
        # holding it was.
        if not new_forward and not new_reverse:
            return True  # nothing to do; still "handled"
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        with self.lock_for_save():
            with open(self.wal_path, "a") as f:
                f.write(json.dumps({"f": new_forward, "r": new_reverse}) + "\n")
            if self._wal_line_count() >= self._wal_compact_threshold_lines:
                self.compact()
        return True

    def _wal_line_count(self) -> int:
        if not os.path.exists(self.wal_path):
            return 0
        with open(self.wal_path) as f:
            return sum(1 for _ in f)

    def compact(self) -> None:
        """Fold the WAL into the canonical snapshot and truncate it. This
        is the one operation in the incremental-write path that IS still
        O(total store size) -- unavoidable, since the snapshot format is a
        single JSON object, not itself an append-only structure -- but
        save_incremental() above only calls it once every
        wal_compact_threshold_lines batches (default 200), not once per
        batch and certainly not once per request, which is the actual
        improvement over Bug 15's original every-single-call full rewrite.
        Safe to call directly too (e.g. a graceful-shutdown hook, or an
        operator running periodic maintenance) -- calling it more often
        than the automatic threshold only costs more of this O(n) work
        sooner, it never produces incorrect state."""
        forward, reverse = self.load()  # snapshot + WAL replay, current full state
        self.save(forward, reverse)     # full read-merge-write of the snapshot
        try:
            os.remove(self.wal_path)
        except FileNotFoundError:
            pass


class RedisStorageProvider(StorageProvider):
    """Redis-backed persistence, for a real multi-instance production
    deployment where a flat local JSON file (FileStorageProvider) can't be
    shared across redact-service replicas or survive a container restart
    without a mounted volume.

    STATUS: verified against a live Redis instance, both for single-process
    thread safety (validation/redis_storage_provider_test.py: basic token
    round trip, persistence across a fresh TokenStore/provider instance,
    tokenize/detokenize round trip, and the 8-threads-x-200-calls
    concurrent-access test that originally found the TokenStore race
    condition, Bug 6) and for real multi-process concurrency
    (validation/multiprocess_redis_test.py: 8 separate OS processes x 50
    tokens each, save() after every token, 0 of 400 tokens lost -- see Bug
    14 in BUGS_AND_FIXES.md, which found and fixed a real cross-process
    data-loss bug in TokenStore.save() itself, not in this class, but this
    class's lock_for_save() is what the fix relies on for Redis). Both
    results are dated 2026-08-07/2026-08-08 -- see ROADMAP.md item 6 for
    the full verification history.

    Stores the forward map as a Redis hash at `{key_prefix}:forward` and the
    reverse map at `{key_prefix}:reverse`, mirroring the two-dict structure
    TokenStore already uses in memory -- this keeps load()/save() simple
    (HGETALL / one HSET per entry) rather than re-deriving one direction
    from the other on every load.

    SENTINEL SUPPORT, added 2026-08-11 when docker-compose.yml's Redis
    became a real master/2-replica/3-sentinel topology (ROADMAP item 12):
    pass `sentinels=[("host", port), ...]` to connect via redis-py's
    Sentinel client instead of a fixed `redis_url` -- see __init__'s own
    comment for exactly what this does and doesn't change. NOT YET
    verified against a live failover in this sandbox (no Docker daemon
    here); the single- and multi-process results above were measured
    against a plain single-instance Redis, before this parameter existed.
    See lock_for_save()'s own updated comment for the specific,
    disclosed correctness window this introduces around a failover
    moment (unreplicated writes/locks on a failed master), not something
    this class's own logic can close on its own.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 key_prefix: str = "redact:tokenstore",
                 sentinels: list[tuple[str, int]] | None = None,
                 sentinel_service_name: str = "redact-master"):
        # Imported lazily so importing this module doesn't require the
        # redis package to be installed for callers who only use
        # FileStorageProvider (the default) -- same pattern detect.py uses
        # for presidio_analyzer.
        import redis  # noqa: E402

        # sentinels, added when docker-compose.yml's single-node `redis`
        # became a real master/2-replica/3-sentinel topology (ROADMAP
        # item 12's queue-side follow-up to the OpenSearch multi-node
        # work): when given a list of (host, port) sentinel addresses,
        # this class asks Sentinel for the CURRENT master on every new
        # connection via redis-py's own Sentinel client, rather than
        # connecting to a single fixed redis_url. That's what lets this
        # provider keep working after Sentinel promotes a replica --
        # redis_url is ignored entirely when sentinels is set, since
        # there's no single fixed URL that stays correct across a
        # failover. When sentinels is None (the default, and what every
        # existing caller -- validation/redis_storage_provider_test.py,
        # validation/multiprocess_redis_test.py, tests/test_redis_validation.py
        # via CI's plain single-instance redis:7 service container --
        # still uses), behavior is unchanged from before this parameter
        # existed: a direct connection to redis_url, no Sentinel involved.
        if sentinels:
            from redis.sentinel import Sentinel  # noqa: E402
            self._sentinel = Sentinel(sentinels, decode_responses=True)
            self._sentinel_service_name = sentinel_service_name
            self._client = self._sentinel.master_for(
                sentinel_service_name, decode_responses=True
            )
        else:
            self._sentinel = None
            self._client = redis.from_url(redis_url, decode_responses=True)
        self._forward_key = f"{key_prefix}:forward"
        self._reverse_key = f"{key_prefix}:reverse"
        self._lock_key = f"{key_prefix}:save-lock"

    def lock_for_save(self):
        # Redis-side counterpart to FileStorageProvider's fcntl-based
        # lock_for_save() -- see StorageProvider.lock_for_save()'s
        # docstring and TokenStore.save() for why read-merge-write alone
        # (this class's load()/save() above) isn't sufficient on its own:
        # two redact-service replicas' save() calls can still interleave
        # within the gap between one replica's own load() and its
        # subsequent save(). Uses the standard simple Redis distributed
        # lock pattern: SET key owner-token NX PX ttl to acquire (only
        # succeeds if no one else holds it), a short retry loop with
        # backoff while it's held elsewhere, and a Lua script to release
        # that only deletes the key if it still holds THIS acquisition's
        # own owner token (so a lock that expired and was re-acquired by
        # someone else during an unexpectedly slow save() is never
        # released out from under its new, legitimate holder).
        #
        # Deliberately NOT the full Redlock algorithm (which addresses
        # correctness across multiple independent Redis nodes). This
        # lock's correctness argument was originally: a single-node
        # Redis's own operations are inherently linearizable, so a
        # single-node SET-NX lock has no split-brain risk the way a
        # multi-node Redlock deployment would need to guard against.
        #
        # THAT ASSUMPTION CHANGED, disclosed rather than silently
        # invalidated, when docker-compose.yml's Redis became a real
        # master/2-replica/3-sentinel topology (ROADMAP item 12): this
        # lock is still correct against any SINGLE master at a time --
        # Redis's own single-instance linearizability guarantee doesn't
        # change just because replicas exist -- but Redis's default
        # asynchronous replication means a lock acquired on the master
        # immediately before it fails is not guaranteed to have already
        # replicated to whichever replica Sentinel promotes next. Two
        # concrete, real failure windows this introduces, not eliminated
        # by anything in this class: (1) a lock acquired on the old
        # master a few milliseconds before failover can vanish entirely
        # on the newly promoted master, letting a second process acquire
        # what looks like a fresh lock while the first process's save()
        # is still in flight against a connection that's about to error
        # out; (2) the reverse -- a save() that completed and released
        # its lock on the old master right before failure may not have
        # replicated either, so those specific token entries can be lost
        # the same way any unreplicated write to a failed master is lost,
        # independent of locking correctness entirely. This is the
        # standard, well-documented tradeoff of Sentinel's asynchronous
        # replication (not something unique to this implementation, and
        # not fixed by a smarter lock -- Redis's own semi-synchronous
        # WAIT command narrows but does not eliminate this window, and
        # isn't used here). Full Redlock, spanning multiple independent
        # masters rather than one master with async replicas, is the
        # documented next step if this narrow, failover-window race ever
        # needs closing -- not implemented here, the same "disclosed gap,
        # not silently accepted as fine" standard this project applies to
        # BLPOP's own non-redelivery limitation (see logstash-queued's
        # comment in docker-compose.yml) and the ordered-vs-load-balanced
        # OpenSearch failover tradeoff in queue_consumer.py.
        return _RedisLockContext(self._client, self._lock_key)

    def load(self) -> tuple[dict[str, str], dict[str, str]]:
        forward = self._client.hgetall(self._forward_key) or {}
        reverse = self._client.hgetall(self._reverse_key) or {}
        return forward, reverse

    def save(self, forward: dict[str, str], reverse: dict[str, str]) -> None:
        # Redis HSET can't set an empty mapping; skip rather than error on
        # an empty store (e.g. the very first save() before any token has
        # been minted).
        pipe = self._client.pipeline()
        pipe.delete(self._forward_key, self._reverse_key)
        if forward:
            pipe.hset(self._forward_key, mapping=forward)
        if reverse:
            pipe.hset(self._reverse_key, mapping=reverse)
        pipe.execute()

    def save_incremental(self, new_forward: dict[str, str], new_reverse: dict[str, str]) -> bool:
        # What actually fixes Bug 15 (BUGS_AND_FIXES.md) on the Redis path:
        # HSET only the entries minted since the last save, instead of
        # save()'s delete-then-full-rewrite of the entire hash. Cost here
        # is O(len(new_forward) + len(new_reverse)) -- the size of ONE
        # debounce batch -- not O(total store size), regardless of how
        # many entries have accumulated in Redis from this or any other
        # process over the store's lifetime.
        #
        # Deliberately NOT wrapped in lock_for_save(): unlike save()'s
        # delete-then-rewrite (which would clobber a sibling process's
        # concurrent write if not serialized), HSET on a per-key basis is
        # additive and safe from two different processes writing
        # different, non-overlapping keys at once. If two processes DO
        # write the same key (i.e. both independently minted a token for
        # the identical original value), that's not a real conflict --
        # get_or_create_token's HMAC is deterministic, so both processes
        # compute the identical token for the identical original, and the
        # second HSET simply overwrites the first with an identical value.
        # This is the same "no real conflict, only two processes agreeing"
        # reasoning save()'s own merge logic already relies on (see
        # TokenStore.save()'s comment). Skipping the lock here is not
        # cutting a corner -- it's removing a cross-process lock this
        # access pattern never actually needed, which the original
        # delete-then-full-rewrite design forced onto it as an artifact of
        # using a destructive write instead of an additive one.
        if not new_forward and not new_reverse:
            return True  # nothing to do; still "handled"
        pipe = self._client.pipeline()
        if new_forward:
            pipe.hset(self._forward_key, mapping=new_forward)
        if new_reverse:
            pipe.hset(self._reverse_key, mapping=new_reverse)
        pipe.execute()
        return True


class VaultStorageProvider(StorageProvider):
    """HashiCorp Vault-backed persistence via the KV v2 secrets engine --
    the open-source, self-hostable secrets-store option this project's own
    "Known limitations" section (README.md) named as not yet implemented,
    alongside RedisStorageProvider above.

    STATUS: NOT verified against a live Vault instance. Unlike
    RedisStorageProvider (verified against a real Redis, both single- and
    multi-process, see that class's own docstring), this environment has
    no route to run a Vault server -- no network access to install or
    download it, the same constraint documented throughout
    BUGS_AND_FIXES.md for anything needing external infrastructure this
    sandbox doesn't have. Implemented against Vault's documented KV v2
    HTTP API and the `hvac` client library's documented behavior, and
    exercised here with a mocked hvac client
    (tests/test_vault_storage_provider.py) to verify THIS CLASS'S OWN
    logic in isolation -- not the same thing as confirming it against a
    real Vault server's actual behavior. Treat this the same way this
    project treats every other environment-blocked claim: a real next
    step for whoever deploys this against actual Vault infrastructure,
    not something already confirmed working.

    Design choices, and why they differ from RedisStorageProvider:
    - One secret per direction (`{path_prefix}/forward`,
      `{path_prefix}/reverse`), each holding the ENTIRE forward or
      reverse map as that secret's data (a flat dict of string keys to
      string values) -- not one Vault secret per token the way
      RedisStorageProvider uses one Redis hash FIELD per token. Vault's
      KV v2 write always replaces a secret's entire data at that path in
      one new version; there is no per-field partial-write primitive the
      way Redis's HSET offers. A one-secret-per-token design would
      restore that granularity, but at the cost of a Vault LIST
      operation plus N individual reads on every load() (KV v2 has no
      "read every secret under this prefix in one call" primitive
      either) -- worse for the frequent case (load() at process startup)
      to optimize the less frequent one (an individual save()). This
      implementation makes that tradeoff explicitly, not accidentally.
    - save_incremental() is DELIBERATELY NOT overridden here (the base
      class default applies -- always returns False). See
      StorageProvider.save_incremental()'s own docstring: a provider
      returning True is asserting it persisted the given batch in
      O(batch size), not O(total store size). This provider cannot make
      that claim honestly given the one-secret-per-direction design
      above -- a "fake" incremental save here would just be save()'s own
      O(n) read-then-write cost renamed, not a real fix, and asserting a
      false performance property is exactly the kind of overstatement
      this project's own standing directive is to avoid. TokenStore.save()
      correctly falls back to its generic read-merge-write path via
      load()/save() below when this returns False -- correct, just
      without the O(batch size) speedup RedisStorageProvider's HSET path
      and FileStorageProvider's WAL provide. A future one-secret-per-
      token redesign could close this gap; not attempted here without a
      real Vault instance available to verify the LIST-then-N-reads
      load() path against.
    """

    def __init__(self, vault_addr: str, vault_token: "str | None" = None,
                 mount_point: str = "secret", path_prefix: str = "redact/tokenstore"):
        # Imported lazily, same pattern as RedisStorageProvider's `import
        # redis` above and detect.py's presidio_analyzer import -- callers
        # who never configure Vault shouldn't need hvac installed.
        import hvac  # noqa: E402
        self._hvac = hvac
        self._client = hvac.Client(
            url=vault_addr,
            token=vault_token or os.environ.get("VAULT_TOKEN"),
        )
        self._mount_point = mount_point
        self._forward_path = f"{path_prefix}/forward"
        self._reverse_path = f"{path_prefix}/reverse"
        self._lock_path = f"{path_prefix}/save-lock"

    def _read_secret(self, path: str) -> dict[str, str]:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount_point,
            )
            return dict(resp["data"]["data"])
        except self._hvac.exceptions.InvalidPath:
            # Nothing written at this path yet -- e.g. the very first
            # load() before any token has ever been minted. Same "empty,
            # not an error" contract FileStorageProvider.load() and
            # RedisStorageProvider.load() both already follow for their
            # own not-yet-created cases.
            return {}

    def _write_secret(self, path: str, data: dict[str, str]) -> None:
        self._client.secrets.kv.v2.create_or_update_secret(
            path=path, secret=data, mount_point=self._mount_point,
        )

    def load(self) -> tuple[dict[str, str], dict[str, str]]:
        return self._read_secret(self._forward_path), self._read_secret(self._reverse_path)

    def save(self, forward: dict[str, str], reverse: dict[str, str]) -> None:
        self._write_secret(self._forward_path, forward)
        self._write_secret(self._reverse_path, reverse)

    def lock_for_save(self):
        # See _VaultLockContext's own docstring for the CAS-plus-
        # staleness-check design and why Vault needs a different
        # mechanism than Redis's native SET ... PX TTL.
        return _VaultLockContext(self._client, self._mount_point, self._lock_path)


class TokenStore:
    """Reversible token <-> original-value mapping. Owns the business logic
    (token generation, in-memory dicts, thread-safety); persistence is
    delegated to a StorageProvider (see above) so the backend -- flat JSON
    file for local dev/testing, Redis or a future Vault provider for
    production -- can be swapped without touching this class.
    """

    def __init__(self, path_or_provider: "str | StorageProvider",
                 token_key: str = "demo-token-key-do-not-use-in-prod",
                 save_every_n_calls: int = 1):
        # Accepts a bare path (original call signature, still the default
        # everywhere in this project -- service.py, pipeline.py, validate.py)
        # for backward compatibility, wrapping it in a FileStorageProvider.
        # Pass a StorageProvider instance directly (e.g. RedisStorageProvider(...))
        # to use a different backend.
        if isinstance(path_or_provider, StorageProvider):
            self._provider: StorageProvider = path_or_provider
        else:
            self._provider = FileStorageProvider(path_or_provider)
        # See save()'s own extensive comment (Bug 15, BUGS_AND_FIXES.md) for
        # why this exists: the default of 1 preserves the exact save-after-
        # every-call behavior every existing test in this project assumes
        # and verifies (validation/multiprocess_tokenstore_test.py,
        # validation/multiprocess_redis_test.py both construct TokenStore
        # with no override and assert zero loss calling save() after every
        # single token). service.py opts into a higher value in production
        # to avoid Bug 15's O(store size) cost on every single request.
        self._save_every_n_calls = max(1, save_every_n_calls)
        self._calls_since_save = 0
        # Entries minted since the last successful persist, tracked
        # separately from self._forward/self._reverse (which hold the FULL
        # in-memory state, old and new alike). This is what makes the real
        # Bug 15 fix possible (StorageProvider.save_incremental(),
        # BUGS_AND_FIXES.md): without a "what's new" set, save() would have
        # no way to persist only the delta and would always have to fall
        # back to the old full read-merge-write. Cleared once
        # save_incremental() confirms it persisted them.
        self._pending_forward: dict[str, str] = {}
        self._pending_reverse: dict[str, str] = {}
        # Keyed, not a plain hash: the mapping is already fully reversible via
        # this store for anyone with storage access, so the token's own
        # resistance to guessing matters for a different audience — someone
        # who sees only the anonymized log output, without store access, and
        # should not be able to enumerate candidates against an unkeyed hash
        # to recover the original value directly from what the log shows.
        self.token_key = token_key
        # Guards every read-modify-write against _forward/_reverse. Found
        # necessary the hard way: under real concurrent HTTP load (multiple
        # Logstash pipeline workers hitting src/service.py at once), one
        # thread's save() could be mid-iteration inside json.dump() while
        # another thread's get_or_create_token() mutated the same dict,
        # raising "RuntimeError: dictionary changed size during iteration".
        # Sequential, single-threaded testing (evaluate.py, the validation
        # suite) never exercises this path, since nothing there calls this
        # store from more than one thread. This lock only ever fixed
        # in-process thread-safety -- cross-process safety (multiple
        # gunicorn workers, or multiple redact-service replicas, sharing
        # one backend) is now handled separately by save()'s
        # read-merge-write logic below, added after a multi-process test
        # found this lock alone was not enough (see save()'s own comment
        # and BUGS_AND_FIXES.md for the full writeup).
        self._lock = threading.Lock()
        self._forward, self._reverse = self._provider.load()

    def save(self, force: bool = False) -> bool:
        # Return value (added as an engineering upgrade alongside
        # src/service.py's Prometheus metrics -- no caller checked this
        # before, so adding it is not a behavior change for any of
        # them): True if this call actually persisted something (either
        # path below), False if the debounce above short-circuited it.
        # Lets a caller distinguish "I called save() and it was a no-op"
        # from "I called save() and it wrote," which service.py's
        # redact_store_save_total{outcome=...} metric needs.
        # Debounce, added 2026-08-08 (Bug 15, BUGS_AND_FIXES.md). Every
        # call below this point -- the read-merge-write critical section --
        # costs O(total accumulated store size), not O(1): FileStorageProvider
        # rewrites the entire JSON file, RedisStorageProvider deletes and
        # re-HSETs the entire hash. That was fine (sub-millisecond) at the
        # store sizes every previous test in this project exercised, but a
        # 1,000,000-line load test (ROADMAP item 9) found it directly: a
        # single request took 2.927s once the store reached 93,279 entries
        # (12.4MB), confirmed via `time curl ... /anonymize` against the
        # live stack, and throughput collapsed from ~250 lines/sec to ~3/sec
        # well before the run finished. service.py calls this method after
        # EVERY /anonymize request, so paying that full cost every single
        # time makes total cost across a long-running deployment O(n^2) in
        # the number of distinct EMAIL/SSN/CREDIT_CARD/MRN values ever
        # tokenized, not O(n).
        #
        # This debounce is a mitigation, not a full fix, and it's worth
        # being upfront about that: it amortizes the same O(n) cost over
        # save_every_n_calls calls
        # instead of paying it on every one, cutting total cost by roughly
        # that factor -- still O(n^2 / k) overall, just with k times less
        # constant, which pushes the point where this becomes a practical
        # problem out by roughly the same factor. It does NOT change the
        # underlying shape. A real fix needs either an append-only/WAL
        # persistence format (avoids ever re-writing old entries) or, for
        # RedisStorageProvider specifically, switching its save() to
        # incremental per-key HSET of only the NEW entries since Redis
        # already supports atomic partial updates natively and never needed
        # the delete-then-full-rewrite pattern it currently uses -- neither
        # is implemented here; both are flagged as the real next step in
        # ROADMAP.md item 9's follow-on.
        #
        # Default save_every_n_calls=1 (set in __init__) makes this a
        # complete no-op change in behavior: every existing test in this
        # project (validation/multiprocess_tokenstore_test.py,
        # validation/multiprocess_redis_test.py) constructs TokenStore with
        # no override and continues to get a real read-merge-write on every
        # single save() call, so their own zero-loss guarantees are
        # unaffected by this change. force=True always performs the actual
        # write regardless of the counter, for callers (tests, a graceful
        # shutdown hook, an explicit "flush now" need) that need the
        # guarantee right now rather than on whatever cadence is configured.
        with self._lock:
            self._calls_since_save += 1
            should_write = force or self._calls_since_save >= self._save_every_n_calls
            if not should_write:
                return False
            self._calls_since_save = 0
            pending_forward = dict(self._pending_forward)
            pending_reverse = dict(self._pending_reverse)

        # Bug 15's fix, added 2026-08-08 (BUGS_AND_FIXES.md): try the
        # provider's incremental path first, which persists ONLY
        # pending_forward/pending_reverse -- the entries minted since the
        # last successful save, not the full accumulated store -- so cost
        # is O(batch size), not O(total store size). Both providers in
        # this codebase now implement it (RedisStorageProvider: per-key
        # HSET of just the new entries; FileStorageProvider: append the
        # new entries to a WAL instead of rewriting the whole JSON
        # snapshot). save_incremental() returns False for any provider
        # that doesn't override it (the default on StorageProvider), which
        # falls through to the exact same read-merge-write this method
        # always did -- so a provider without an incremental
        # implementation (e.g. a not-yet-updated custom provider) keeps
        # working exactly as before, just without the speedup.
        if self._provider.save_incremental(pending_forward, pending_reverse):
            with self._lock:
                # Only clear the pending entries this specific call
                # actually persisted -- if get_or_create_token() added MORE
                # entries on another thread between the snapshot above and
                # here, those stay pending for the next save() rather than
                # being incorrectly discarded.
                for k in pending_forward:
                    self._pending_forward.pop(k, None)
                for k in pending_reverse:
                    self._pending_reverse.pop(k, None)
            return True

        # Fallback path for providers without save_incremental() support:
        # read-merge-write, not a blind overwrite of whatever this
        # process happens to be holding in memory. Found necessary the
        # hard way (ROADMAP item 6 follow-on,
        # validation/multiprocess_tokenstore_test.py): __init__ above
        # loads persisted state ONCE, at construction; every save() before
        # this fix then persisted only THIS process's own local view,
        # completely overwriting the backend regardless of what any
        # concurrent process had written in the meantime. Under
        # service.py's real usage (one TokenStore per gunicorn worker
        # process, save() called after every single /anonymize request,
        # all workers sharing one backend) this is not a rare race -- it
        # is close to guaranteed, repeated data loss: whichever worker's
        # save() lands last in any given window silently erases every
        # reverse-map entry a sibling worker wrote that this worker never
        # itself loaded. A multi-process test mirroring this exact usage
        # pattern confirmed it live: a large fraction of minted tokens'
        # reverse-map entries were lost before this fix (see
        # BUGS_AND_FIXES.md).
        #
        # Fix: reload the CURRENT persisted state immediately before
        # writing, merge this process's own additions on top of it (not
        # instead of it), and adopt the merged result as this process's
        # own in-memory state too. That has the added benefit of letting
        # this process start seeing tokens minted by siblings it never
        # directly loaded from, beyond simply no longer erasing them.
        # Conflicting values for the same key are resolved by keeping
        # this process's own local value, which is safe specifically
        # *because* token generation is a deterministic HMAC of the
        # original value under a fixed key (get_or_create_token above) --
        # two processes independently minting a token for the identical
        # original value will always compute the identical token, so
        # there is no real "conflict" to lose information over, only two
        # processes agreeing.
        #
        # Read-merge-write alone is still just narrowing a race, not
        # closing it: two processes' save() calls could still interleave
        # within the gap between one process's own load() and its
        # subsequent save(). Measured directly (ROADMAP item 6 follow-on):
        # under a tight, adversarial 8-process stress test
        # (validation/multiprocess_tokenstore_test.py), read-merge-write
        # alone still lost 14.2% of minted tokens' reverse-map entries --
        # a large improvement over the pre-fix 58.7% (and pre-fix also
        # crashed 5 of 8 workers outright with json.JSONDecodeError, see
        # FileStorageProvider.save()'s own comment), but not acceptable
        # for a guarantee this framework's own tokenize() docstring states
        # plainly ("Exact original value can be recovered by anyone with
        # access to `store`"). self._provider.lock_for_save() closes that
        # remaining gap by serializing the ENTIRE load-merge-save critical
        # section across processes, not just within one -- confirmed via
        # the same test at 0/400 lost after this lock was added (see
        # BUGS_AND_FIXES.md for the full before/after numbers across all
        # three stages: crash-prone, reduced-but-real loss, zero loss).
        with self._lock, self._provider.lock_for_save():
            remote_forward, remote_reverse = self._provider.load()
            merged_forward = {**remote_forward, **self._forward}
            merged_reverse = {**remote_reverse, **self._reverse}
            self._provider.save(merged_forward, merged_reverse)
            self._forward, self._reverse = merged_forward, merged_reverse
            # The full write above just persisted everything, pending or
            # not -- clear pending so a later provider swap (or a provider
            # that only sometimes supports incremental writes) doesn't
            # re-send entries this fallback path already covered.
            self._pending_forward.clear()
            self._pending_reverse.clear()
            return True

    def get_or_create_token(self, original: str, pii_type: str) -> str:
        with self._lock:
            if original in self._forward:
                return self._forward[original]
            digest = hmac.new(self.token_key.encode(), original.encode(), hashlib.sha256).hexdigest()[:32]
            token = f"tok_{pii_type.lower()}_{digest}"
            self._forward[original] = token
            self._reverse[token] = original
            self._pending_forward[original] = token
            self._pending_reverse[token] = original
            return token

    def resolve(self, token: str) -> str | None:
        with self._lock:
            return self._reverse.get(token)



def tokenize(text: str, spans: list[dict], store: TokenStore) -> str:
    def transform(original: str, span: dict) -> str:
        return store.get_or_create_token(original, span["type"])
    return _apply_right_to_left(text, spans, transform)


def detokenize(text: str, store: TokenStore) -> str:
    """Reverses tokenize() by replacing every token pattern found in the
    text with its original value, for an authorized investigator only."""
    with store._lock:
        reverse_snapshot = dict(store._reverse)
    out = text
    for token, original in reverse_snapshot.items():
        out = out.replace(token, original)
    return out


# ---------------------------------------------------------------------------
# Decision matrix implementation: routes each detected span to redact,
# pseudonymize, or tokenize based on the field type, matching the table in
# Section 3.6 of the chapter.
# ---------------------------------------------------------------------------

# High-cardinality identifiers where correlation across events matters more
# than recovering the literal original value -> pseudonymize. NOTE: this is
# a one-way transform (see module docstring); choosing this path means the
# original value is gone once the log is written, by design.
PSEUDONYMIZE_TYPES = {"IP", "PERSON"}

# Low-cardinality identifiers where an investigator needs the exact original
# value back -> tokenize (reversible via the store, not just a key).
TOKENIZE_TYPES = {"EMAIL", "MRN", "CREDIT_CARD", "SSN"}

# Fields with no analytic value at all would be redacted; none of the six
# ground-truth types in this dataset fall in that bucket, so REDACT_TYPES is
# empty here but the routing function still supports it for fields that do
# (e.g. free-text password-reset bodies, as discussed in the chapter).
REDACT_TYPES: set[str] = set()


def anonymize_by_policy(text: str, spans: list[dict], key: str, store: TokenStore) -> str:
    to_redact = [s for s in spans if s["type"] in REDACT_TYPES]
    to_pseudonymize = [s for s in spans if s["type"] in PSEUDONYMIZE_TYPES]
    to_tokenize = [s for s in spans if s["type"] in TOKENIZE_TYPES]

    # apply in a single right-to-left pass over the union so offsets stay
    # valid regardless of which action touches which span
    def transform(original: str, span: dict) -> str:
        if span["type"] in REDACT_TYPES:
            return "[REDACTED]"
        if span["type"] in PSEUDONYMIZE_TYPES:
            digest = hmac.new(key.encode(), original.encode(), hashlib.sha256).hexdigest()
            return f"{span['type'].lower()[:4]}_{digest[:32]}"
        if span["type"] in TOKENIZE_TYPES:
            return store.get_or_create_token(original, span["type"])
        return original  # unknown type: leave untouched rather than guess

    return _apply_right_to_left(text, spans, transform)
