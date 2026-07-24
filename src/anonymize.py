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


class TokenStore:
    """Reversible token <-> original-value mapping, persisted as JSON.

    This is the open-source, zero-budget stand-in for a proper secrets store
    (HashiCorp Vault, AWS Secrets Manager). The chapter is explicit that a
    flat JSON file is not an acceptable production token store on its own —
    see the limitations note at the bottom of this file — but it is enough
    to demonstrate and evaluate the tokenize/detokenize round trip honestly.
    """

    def __init__(self, path: str, token_key: str = "demo-token-key-do-not-use-in-prod"):
        self.path = path
        # Keyed, not a plain hash: the mapping is already fully reversible via
        # this store for anyone with file access, so the token's own
        # resistance to guessing matters for a different audience — someone
        # who sees only the anonymized log output, without store access, and
        # should not be able to enumerate candidates against an unkeyed hash
        # to recover the original value directly from what the log shows.
        self.token_key = token_key
        self._forward: dict[str, str] = {}   # original -> token
        self._reverse: dict[str, str] = {}    # token -> original
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
                self._forward = data.get("forward", {})
                self._reverse = data.get("reverse", {})

    def save(self):
        with open(self.path, "w") as f:
            json.dump({"forward": self._forward, "reverse": self._reverse}, f)

    def get_or_create_token(self, original: str, pii_type: str) -> str:
        if original in self._forward:
            return self._forward[original]
        digest = hmac.new(self.token_key.encode(), original.encode(), hashlib.sha256).hexdigest()[:32]
        token = f"tok_{pii_type.lower()}_{digest}"
        self._forward[original] = token
        self._reverse[token] = original
        return token

    def resolve(self, token: str) -> str | None:
        return self._reverse.get(token)



def tokenize(text: str, spans: list[dict], store: TokenStore) -> str:
    def transform(original: str, span: dict) -> str:
        return store.get_or_create_token(original, span["type"])
    return _apply_right_to_left(text, spans, transform)


def detokenize(text: str, store: TokenStore) -> str:
    """Reverses tokenize() by replacing every token pattern found in the
    text with its original value, for an authorized investigator only."""
    out = text
    for token, original in store._reverse.items():
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
