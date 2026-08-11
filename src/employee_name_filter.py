"""
Peppered Bloom filter for employee-name matching (Layer 4 companion,
2026-08-11) -- fifth and last item from the external-review batch (see
BUGS_AND_FIXES.md's "Engineering upgrade" entries 1-3 and the ONNX spike
for the rest of that batch).

--- What problem this solves, and what it doesn't ---
`flattened_names.py`'s existing Layer 4 matches flattened usernames
against Faker's public en_US name dictionary -- useful, but this
project's own `validation/non_us_name_test.py` and
`validation/real_name_frequency/` measured directly that this dictionary
badly misses real names outside its own population (1.4% recall on
non-en_US names, 15.2% on a realistic US-frequency-weighted sample).
A real deployment usually has a much better name list already sitting in
its own Active Directory or Okta directory: the actual employees whose
names might appear in its own logs. This module lets that real list be
used for matching WITHOUT the redaction pipeline ever storing or querying
plaintext names -- the review's original framing called this
"zero-knowledge identity matching."

--- Why "zero-knowledge" is the wrong term, said plainly rather than
    inherited uncritically ---
"Zero-knowledge" is a specific cryptographic term (a proof system where a
verifier learns the truth of a statement and NOTHING else, with a formal
security proof). What this module actually implements is a KEYED
(peppered) Bloom filter -- a real, useful, well-understood privacy
technique, but not a zero-knowledge proof system in the formal sense, and
calling it one overstates what it provides. Using the precise name
matters here specifically because this project has a stated commitment
(BUGS_AND_FIXES.md, README.md) to not overclaim what's actually verified;
inheriting a marketing term from the source review without correcting it
would be exactly that kind of overclaim.

--- The security property this DOES provide, and the real limit of it ---
A plain SHA-256(lowercase(name)) Bloom filter -- the review's original,
literal proposal -- is trivially reversible: human names have low
entropy, and this project's own `validation/real_name_frequency/` already
has real SSA/Census name-frequency data. An attacker with read access to
such a filter could hash every candidate name from that same public
dataset and check membership, fully reconstructing "who's on this list"
in minutes. This is real HR/personnel data, not something that should be
this cheaply reversible.

Fix: every hash uses HMAC-SHA256 keyed with a secret, deployment-specific
PEPPER, not bare SHA-256. Without the pepper, an attacker with only the
filter's contents cannot replicate the hash function at all -- brute-force
enumeration over a name-frequency dictionary produces nothing useful,
because HMAC-SHA256(pepper, "john smith") is indistinguishable from
random without knowing `pepper`.

**This narrows the attack surface, it does not eliminate it.** An
attacker who compromises BOTH the filter's contents (e.g. a Redis/disk
read) AND the pepper (wherever it's actually stored -- see below) can
still run the exact same brute-force attack the unsalted version was
vulnerable to. The real security property is defense-in-depth (splitting
one secret into two independently-compromisable pieces), not immunity.
Said here plainly rather than glossed over.

--- Pepper storage: a real operational requirement, not an afterthought ---
The pepper MUST live somewhere other than next to the filter data, or
none of the above matters. This project already has a real secrets
backend for exactly this kind of value (`VaultStorageProvider`,
`src/anonymize.py`) -- store the pepper there (or in whatever KMS/secrets
manager backs a real deployment), read it via an env var
(`REDACT_NAME_MATCH_PEPPER`) at process startup, and never persist it
alongside the filter's serialized bit array. This module does not manage
pepper storage/rotation itself -- it takes the pepper as a constructor
argument and trusts the caller to have sourced it correctly, the same
division of responsibility `RedisStorageProvider`/`VaultStorageProvider`
already use for `TOKEN_KEY`/`PSEUDO_KEY`.

**Rotation has a real, disclosed cost:** rotating the pepper changes
every hash output, which means the filter must be fully rebuilt from the
original plaintext AD/Okta name export -- there is no way to "re-key" an
existing filter without the source data. A real deployment needs to
retain (securely) access to that source export for as long as it wants
to be able to rotate this pepper, which is a real operational
constraint worth planning for up front, not discovering at rotation time.

--- Why a plain Python bit array, not RedisBloom ---
The review's diagram used a "Redis Bloom Filter." This project's
docker-compose.yml uses plain `redis:7-alpine`, not
`redis/redis-stack-server` (the image that actually bundles the
RedisBloom module) -- adding it would mean a new infrastructure
dependency and image swap. A Bloom filter's whole point is that querying
it needs no network round-trip if it's small enough to hold in memory (a
few hundred thousand employee names is a few hundred KB to a few MB as a
bit array, not a multi-GB structure), so this implements it as a plain
in-process Python bit array with HMAC-based hash functions instead --
loaded once per worker process at startup (the same per-process warm-up
pattern this project already uses for the spaCy/Presidio analyzer, see
`detect._get_analyzer()`), with no new infrastructure dependency and no
network call on the hot path. `save()`/`load()` (de)serialize to a single
file so the same built filter can be distributed to every worker/replica
without each one re-processing the raw name list.

--- False positives are real, and land in the SAFE direction ---
Bloom filters have a tunable false-positive rate and a hard guarantee of
zero false negatives: if `might_contain()` returns False, the item was
genuinely never inserted. If it returns True, the item was very likely
inserted, but could rarely be a false positive. For a redaction system,
that asymmetry is the right one: a false positive here means an
occasional non-name token gets flagged as PERSON and over-redacted (safe
failure mode), never that a real employee's name silently passes through
undetected (which zero false negatives rules out by construction).

--- Deletion/offboarding: a real, disclosed gap ---
A standard Bloom filter (this implementation) cannot remove an item once
added -- a departed employee's name can't be individually un-flagged.
The correct operational pattern is a periodic full rebuild from a fresh
AD/Okta export (this module's CLI, see `build_from_names_file()`), not
incremental deletion. A counting Bloom filter variant could support
deletion but adds real complexity for a benefit (avoiding a scheduled
rebuild) most deployments won't need -- not implemented here, flagged as
a real design tradeoff rather than a silent limitation.

--- What is and isn't verified here ---
This module's own hashing/bit-array/false-positive-rate logic is
directly, deterministically testable without any external dependency
(see tests/test_employee_name_filter.py) -- no live model or Docker
needed for that. What is NOT verified: real AD/Okta export data (none
available in this environment), integration into detect.py's actual
detection ensemble as a new layer (this module is standalone and
opt-in, not yet wired into scan_flattened() or scan_regex() -- see
ROADMAP.md for that as a disclosed next step, gated the same way Vault/
Sentinel support is: present and tested on its own, not yet exercised
end-to-end against the live pipeline in this sandbox).
"""
import hashlib
import hmac
import math
import pickle


class HashedNameFilter:
    """A keyed (peppered) Bloom filter over normalized name strings.

    Every membership check requires the same `pepper` used when the
    filter was built -- this is what makes the filter's contents
    unusable to an attacker who doesn't also have the pepper (see this
    module's own top-of-file design note for the honest limits of that
    property).
    """

    def __init__(self, pepper: bytes, capacity: int, error_rate: float = 0.01):
        """
        pepper: secret, deployment-specific key (bytes). Caller is
            responsible for sourcing this securely (Vault, KMS, etc.) --
            see the module docstring's "Pepper storage" section. Must be
            non-empty; an empty pepper degrades this back to the
            unsalted, reversible construction this module exists to fix.
        capacity: expected number of names to be inserted. Used to size
            the bit array -- undersizing raises the real false-positive
            rate above `error_rate`, oversizing wastes memory. Rebuild
            with a new capacity if the real employee count grows well
            past what was estimated.
        error_rate: target false-positive probability at `capacity`
            inserted items. Standard Bloom filter sizing formulas
            (below) -- never claims a specific number was empirically
            measured against real data, since this sandbox has no real
            AD/Okta export to measure against; see
            tests/test_employee_name_filter.py for the synthetic-data
            measurement that DOES exist.
        """
        if not pepper:
            raise ValueError(
                "pepper must be non-empty -- an empty pepper defeats the entire "
                "point of this class (see module docstring's security note). "
                "Use a plain unsalted set/dict if you genuinely don't need this "
                "property; don't call this class with an empty pepper instead."
            )
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if not (0 < error_rate < 1):
            raise ValueError("error_rate must be between 0 and 1")

        self.pepper = pepper
        self.capacity = capacity
        self.error_rate = error_rate

        # Standard Bloom filter sizing: m = -(n * ln(p)) / (ln(2)^2),
        # k = (m/n) * ln(2). Rounded to at least 1 in each case so a
        # tiny capacity doesn't produce a degenerate zero-size filter.
        n = capacity
        p = error_rate
        m = max(8, math.ceil(-(n * math.log(p)) / (math.log(2) ** 2)))
        k = max(1, round((m / n) * math.log(2)))

        self.num_bits = m
        self.num_hashes = k
        self.bits = bytearray((m + 7) // 8)
        self._count = 0

    def _normalize(self, name: str) -> str:
        """Same normalization for both add() and might_contain() --
        lowercase, collapse internal whitespace, strip leading/trailing.
        Deliberately simple and deterministic; a real deployment's AD/
        Okta export may need its own normalization pass (Unicode
        normalization, honorifics stripped, etc.) before calling add() --
        that's the caller's job, this just guarantees add() and
        might_contain() apply the identical transform to whatever string
        they're given."""
        return " ".join(name.strip().lower().split())

    def _hash_positions(self, normalized: str) -> list[int]:
        """Derives `num_hashes` independent-enough bit positions from one
        HMAC-SHA256 digest (the standard double-hashing technique --
        Kirsch/Mitzenmacher -- rather than computing num_hashes separate
        HMAC calls, which would be needlessly slower for no real benefit
        at this filter's scale)."""
        digest = hmac.new(self.pepper, normalized.encode("utf-8"), hashlib.sha256).digest()
        h1 = int.from_bytes(digest[:16], "big")
        h2 = int.from_bytes(digest[16:], "big")
        return [(h1 + i * h2) % self.num_bits for i in range(self.num_hashes)]

    def add(self, name: str) -> None:
        normalized = self._normalize(name)
        for pos in self._hash_positions(normalized):
            self.bits[pos // 8] |= (1 << (pos % 8))
        self._count += 1

    def might_contain(self, token: str) -> bool:
        """False is a guarantee (token was never added). True means
        "probably added" -- see module docstring on the false-positive
        rate and why it's the safe direction for this use case."""
        normalized = self._normalize(token)
        return all(
            self.bits[pos // 8] & (1 << (pos % 8))
            for pos in self._hash_positions(normalized)
        )

    def __contains__(self, token: str) -> bool:
        return self.might_contain(token)

    def estimated_false_positive_rate(self) -> float:
        """Real current false-positive rate given how many items have
        actually been added so far, not just the target `error_rate`
        this filter was sized for -- these diverge if the real inserted
        count differs from the `capacity` estimate used at construction."""
        if self._count == 0:
            return 0.0
        exponent = -self.num_hashes * self._count / self.num_bits
        return (1 - math.exp(exponent)) ** self.num_hashes

    def save(self, path: str) -> None:
        """Serializes the filter's bit array and parameters -- NOT the
        pepper (never persisted; see module docstring). A file produced
        by save() is useless for membership queries without separately
        supplying the same pepper to load()."""
        with open(path, "wb") as f:
            pickle.dump({
                "num_bits": self.num_bits,
                "num_hashes": self.num_hashes,
                "bits": bytes(self.bits),
                "capacity": self.capacity,
                "error_rate": self.error_rate,
                "count": self._count,
            }, f)

    @classmethod
    def load(cls, path: str, pepper: bytes) -> "HashedNameFilter":
        with open(path, "rb") as f:
            state = pickle.load(f)
        obj = cls.__new__(cls)
        obj.pepper = pepper
        obj.capacity = state["capacity"]
        obj.error_rate = state["error_rate"]
        obj.num_bits = state["num_bits"]
        obj.num_hashes = state["num_hashes"]
        obj.bits = bytearray(state["bits"])
        obj._count = state["count"]
        return obj


def build_from_names_file(names_path: str, pepper: bytes, error_rate: float = 0.01) -> HashedNameFilter:
    """Builds a filter from a plaintext file, one name per line (the
    expected shape of an AD/Okta export flattened to display names).
    Capacity is derived from the actual line count, not guessed --
    sizing the filter correctly requires knowing the real count, and
    this is the one point in this module's lifecycle where the raw
    plaintext names are ever read at all."""
    with open(names_path, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip()]

    filt = HashedNameFilter(pepper=pepper, capacity=max(1, len(names)), error_rate=error_rate)
    for name in names:
        filt.add(name)
    return filt


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Build a peppered employee-name Bloom filter from a plaintext names file."
    )
    parser.add_argument("names_file", help="One name per line (AD/Okta display-name export)")
    parser.add_argument("output_file", help="Where to save the serialized filter")
    parser.add_argument("--error-rate", type=float, default=0.01)
    args = parser.parse_args()

    pepper_hex = os.environ.get("REDACT_NAME_MATCH_PEPPER")
    if not pepper_hex:
        raise SystemExit(
            "REDACT_NAME_MATCH_PEPPER must be set (hex-encoded secret) -- "
            "see this module's docstring for why this must never be hardcoded "
            "or committed alongside the built filter file."
        )
    pepper = bytes.fromhex(pepper_hex)

    filt = build_from_names_file(args.names_file, pepper, args.error_rate)
    filt.save(args.output_file)
    print(f"Built filter: {filt._count} names, {filt.num_bits} bits, "
          f"{filt.num_hashes} hash functions, "
          f"estimated FP rate {filt.estimated_false_positive_rate():.4%}")
    print(f"Saved to {args.output_file} (pepper NOT included in this file -- "
          f"store it separately, see module docstring)")
