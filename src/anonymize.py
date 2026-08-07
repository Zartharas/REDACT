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
import threading
from abc import ABC, abstractmethod


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

    def load(self) -> tuple[dict[str, str], dict[str, str]]:
        if not os.path.exists(self.path):
            return {}, {}
        with open(self.path) as f:
            data = json.load(f)
            return data.get("forward", {}), data.get("reverse", {})

    def save(self, forward: dict[str, str], reverse: dict[str, str]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"forward": forward, "reverse": reverse}, f)


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
        # store from more than one thread. The lock only fixes in-process
        # thread-safety -- it says nothing about a multi-instance production
        # deployment sharing one Redis backend, which needs the backend's
        # own atomicity guarantees (see RedisStorageProvider's use of a
        # pipeline for save()), not this in-process lock, to stay correct
        # across separate redact-service processes.
        self._lock = threading.Lock()
        self._forward, self._reverse = self._provider.load()

    def save(self):
        with self._lock:
            self._provider.save(self._forward, self._reverse)

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
