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

    def __init__(self, path: str):
        self.path = path

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
        if not os.path.exists(self.path):
            return {}, {}
        with open(self.path) as f:
            data = json.load(f)
            return data.get("forward", {}), data.get("reverse", {})

    def save(self, forward: dict[str, str], reverse: dict[str, str]) -> None:
        # Write-to-temp-then-rename, not a direct open(path, "w"). Found
        # necessary the hard way (ROADMAP item 6 follow-on,
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


class RedisStorageProvider(StorageProvider):
    """Redis-backed persistence, for a real multi-instance production
    deployment where a flat local JSON file (FileStorageProvider) can't be
    shared across redact-service replicas or survive a container restart
    without a mounted volume.

    STATUS, stated plainly rather than implied: this has been written
    against the documented `redis-py` client API and is straightforward
    (two hashes, HSET/HGET/HGETALL), but it has NOT been run against a live
    Redis instance in this project's own testing -- unlike everything else
    in BUGS_AND_FIXES.md, which is only ever marked verified after an actual
    execution, not just a plausible-looking implementation. Treat this class
    as unverified until it has been exercised the same way: point it at a
    real `redis` container, run the tokenize/detokenize round-trip and the
    concurrent-access test that originally found the TokenStore race
    condition (see Bug 6), and update this docstring with the result before
    relying on it in production.

    Stores the forward map as a Redis hash at `{key_prefix}:forward` and the
    reverse map at `{key_prefix}:reverse`, mirroring the two-dict structure
    TokenStore already uses in memory -- this keeps load()/save() simple
    (HGETALL / one HSET per entry) rather than re-deriving one direction
    from the other on every load.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 key_prefix: str = "redact:tokenstore"):
        # Imported lazily so importing this module doesn't require the
        # redis package to be installed for callers who only use
        # FileStorageProvider (the default) -- same pattern detect.py uses
        # for presidio_analyzer.
        import redis  # noqa: E402
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
        # correctness across multiple independent Redis nodes) --
        # docker-compose.yml's own Redis is explicitly single-node,
        # matching this project's stated single-node scope everywhere
        # else (see validation/load_test/README.md). This lock is correct
        # for that scope: a single-node Redis's own operations are
        # inherently linearizable, so a single-node SET-NX lock has no
        # split-brain risk the way a multi-node Redlock deployment would
        # need to guard against.
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


class TokenStore:
    """Reversible token <-> original-value mapping. Owns the business logic
    (token generation, in-memory dicts, thread-safety); persistence is
    delegated to a StorageProvider (see above) so the backend -- flat JSON
    file for local dev/testing, Redis or a future Vault provider for
    production -- can be swapped without touching this class.
    """

    def __init__(self, path_or_provider: "str | StorageProvider",
                 token_key: str = "demo-token-key-do-not-use-in-prod"):
        # Accepts a bare path (original call signature, still the default
        # everywhere in this project -- service.py, pipeline.py, validate.py)
        # for backward compatibility, wrapping it in a FileStorageProvider.
        # Pass a StorageProvider instance directly (e.g. RedisStorageProvider(...))
        # to use a different backend.
        if isinstance(path_or_provider, StorageProvider):
            self._provider: StorageProvider = path_or_provider
        else:
            self._provider = FileStorageProvider(path_or_provider)
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

    def save(self):
        # Read-merge-write, not a blind overwrite of whatever this
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
        # itself loaded. Confirmed live: a multi-process test mirroring
        # this exact usage pattern lost a large fraction of minted
        # tokens' reverse-map entries before this fix (see
        # BUGS_AND_FIXES.md).
        #
        # Fix: reload the CURRENT persisted state immediately before
        # writing, merge this process's own additions on top of it (not
        # instead of it), and adopt the merged result as this process's
        # own in-memory state too -- which has the added benefit of
        # letting this process start seeing tokens minted by siblings it
        # never directly loaded from, not just avoiding erasing them.
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

    def get_or_create_token(self, original: str, pii_type: str) -> str:
        with self._lock:
            if original in self._forward:
                return self._forward[original]
            digest = hmac.new(self.token_key.encode(), original.encode(), hashlib.sha256).hexdigest()[:32]
            token = f"tok_{pii_type.lower()}_{digest}"
            self._forward[original] = token
            self._reverse[token] = original
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
