"""
Format-Preserving Encryption (FF3-1) as an OPTIONAL alternative to
TokenStore for a narrow class of values -- last of the five items from
the external-review batch this session worked through (see
BUGS_AND_FIXES.md's "Engineering upgrade" entries 1, 2, 3, 4 and the ONNX
spike for the rest).

--- What this is an alternative TO, and what it is not ---
This is NOT a StorageProvider (src/anonymize.py) and does not replace
TokenStore, RedisStorageProvider, FileStorageProvider, or
VaultStorageProvider. Those exist because tokenize()/detokenize() need a
persisted forward/reverse mapping -- this module exists specifically to
explore the *other* branch of that design space: a scheme where
"detokenization" needs no persisted mapping at all, because decryption
IS the reverse operation, given the same key. That eliminates the
TokenStore HA problem this project spent real effort solving (Redis
Sentinel, live failover testing, Bugs 22-24) -- but it introduces a
different, and arguably worse, problem instead. Both tradeoffs are real
and neither is free; see below.

--- Why this is scoped to digit-only fields, not "any PII value" ---
The external review's proposal ("use AES-FFX instead of a token
mapping") did not address this, but it matters: format-preserving
encryption requires a fixed alphabet/radix. Encrypting "239-65-9864" into
"812-94-0381" works because the domain is digits 0-9 (radix 10) --
FF3-1's algorithm operates over that fixed alphabet. There is no
equivalent well-defined domain for a PERSON value like "Timothy Wong" --
what would a "format-preserving" encrypted name even look like, and what
alphabet would it draw from? This module is deliberately scoped to
digit-only identifier fields (SSN, credit card numbers, and similarly
shaped numeric IDs) where FPE is a coherent idea at all -- PERSON/EMAIL
values stay on TokenStore's existing tokenize()/detokenize() path, full
stop.

--- Library and standard used ---
`ff3` (PyPI), a real implementation of NIST SP 800-38G Revision 1 (FF3-1
-- the CORRECTED standard, not the original FF3, which had a real
cryptanalytic weakness published in 2017 that led NIST to require the
distinguishing fix: a 56-bit tweak instead of FF3's original 64-bit
tweak, and a minimum domain size of radix^minLen >= 1,000,000 instead of
FF3's original >= 100). This module always uses a 56-bit (7-byte) tweak
specifically to get the library's FF3-1 code path
(`calculate_tweak64_ff3_1`, confirmed by reading the installed package's
source in this sandbox), never the legacy 64-bit tweak the same library
also supports for backward compatibility with the deprecated original
FF3 -- using the legacy tweak length here would silently opt back into
the weaker, deprecated construction.

--- The real, disclosed security tradeoff: smaller margins than
    ordinary AES ---
Even with FF3-1's fix applied, format-preserving encryption over a small
domain has a smaller security margin than standard AES-GCM/AES-CTR over
arbitrary-length ciphertext. A 9-digit SSN has a plaintext domain of
10^9 (~30 bits) -- nowhere near AES's normal 128-bit security margin,
because FPE's whole point is staying within that small domain rather
than expanding into a much larger ciphertext space. This does not mean
FF3-1 is "broken" (it is the NIST-standardized, current-best construction
for this exact problem), but it does mean an SSN's actual practical
brute-force resistance is bounded by the SIZE OF THE SSN DOMAIN ITSELF
(10^9 possibilities), not by the underlying AES key strength, in a way a
non-format-preserving encryption of the same value would not be bounded.
Worth knowing before treating an FPE-encrypted SSN as having the same
practical guessing-resistance as an AES-256-GCM-encrypted blob.

--- The real, disclosed operational tradeoff: key rotation is much
    worse than TokenStore's ---
`dags/redact_weekly_validation.py`'s `rotate_token_key` task rotates
`TOKEN_KEY` specifically because that rotation is SAFE under this
project's existing design: `TokenStore`'s resolution is lookup-table-based,
not key-based, so rotating `TOKEN_KEY` only affects the guessability
resistance of *future* tokens -- every already-minted token stays
resolvable exactly as before (see that task's own docstring). FPE cannot
offer this property at all: decryption IS the reverse of encryption
under the same key, by construction, so rotating this module's key
makes every previously-encrypted value permanently unrecoverable unless
it's re-encrypted under the new key before the old key is discarded (a
full reprocessing pass over historical data, not a background task).
This is a genuine, structural downgrade versus TokenStore's rotation
story, not a minor inconvenience -- stated here plainly because the
external review's "stateless AES-FFX" framing presented statelessness as
a pure win without mentioning this cost.

--- Key management (out of scope for this module, same as TokenStore's
    own division of responsibility) ---
This module takes the key as a constructor argument and does not manage
its storage -- same division of responsibility `VaultStorageProvider`
already uses for `TOKEN_KEY`/`PSEUDO_KEY`, and `HashedNameFilter`
(`src/employee_name_filter.py`) uses for its pepper. A real deployment
should source this from Vault/KMS via `REDACT_FPE_KEY` (hex-encoded,
128/192/256-bit per FF3-1's requirement), never hardcoded.

--- What is and isn't verified here ---
This module's own encrypt/decrypt round-trip correctness, domain-size
enforcement, and format-preservation (dashes/separators reinserted
correctly around the encrypted digit sequence) are all directly,
deterministically testable without any external dependency (see
tests/test_fpe_provider.py). What is NOT claimed: this module has not
been cryptanalyzed by this project (that's the `ff3` library's and
NIST's job, not this project's), and it is NOT wired into
`anonymize.py`'s live pseudonymization path -- it exists as a
standalone, evaluated, opt-in alternative, not an active part of the
detection/anonymization pipeline. See ROADMAP.md for that as an
explicit, undone next step.
"""
import re

# REAL BUG, found and fixed 2026-08-11: this used to be `from ff3 import
# FF3Cipher` at module level, which meant simply IMPORTING fpe_provider
# (e.g. tests/test_fpe_provider.py's own `from fpe_provider import ...`)
# required the optional `ff3` package (requirements-fpe.txt) to already
# be installed -- and pytest's collection phase fails HARD on an
# ImportError, aborting the entire `pytest tests/` run, not just skipping
# this one file. Every other optional dependency in this project
# (kafka-python, hvac, redis, prometheus-client) is lazily imported
# inside the function/class that actually needs it, specifically so a
# missing optional dependency degrades to "this one feature isn't
# available" rather than "nothing in this test suite can run at all."
# This module was the one exception, caught only when a user ran
# `pytest tests/` in a fresh environment that had installed
# requirements.txt but not requirements-fpe.txt -- exactly the
# environment tests/README.md's own documented quick-start produces.
# Fixed by moving the import inside __init__, matching the established
# pattern everywhere else.

# 56-bit tweak, deliberately -- this is what selects FF3-1's corrected
# code path in the underlying library rather than the deprecated
# original FF3's 64-bit tweak. A fixed, non-secret domain-separator value
# (not a per-value random nonce -- FF3-1 does not use one; the tweak's
# role here is closer to an HMAC key-derivation "info" string than to an
# IV). Different tweaks partition the encryption space -- using a
# different tweak per field TYPE (SSN vs. CREDIT_CARD) is a reasonable
# future refinement so the same digit sequence encrypted as different
# field types doesn't map to the same ciphertext, not implemented here
# to keep this module's first version simple and directly testable.
DEFAULT_TWEAK = "D8E7920AFA330A"  # 7 bytes / 56 bits, hex-encoded

# FF3-1's own minimum domain requirement (radix^minLen >= 1,000,000) --
# for radix 10 (plain digits), this works out to needing at least 6
# digits. Anything shorter is out of scope for this module entirely, not
# silently padded or worked around.
_MIN_DIGIT_LENGTH = 6


class FPEDigitsProvider:
    """Format-preserving encryption for digit-only PII values (SSN,
    credit card numbers, and similarly shaped numeric identifiers).
    NOT for PERSON/EMAIL/free-text values -- see module docstring."""

    def __init__(self, key_hex: str, tweak_hex: str = DEFAULT_TWEAK):
        """key_hex: 128/192/256-bit AES key, hex-encoded (32/48/64 hex
        chars). Caller is responsible for sourcing this securely (Vault/
        KMS) -- see module docstring's key-management section."""
        from ff3 import FF3Cipher  # lazy -- see module header comment
        self._cipher = FF3Cipher(key_hex, tweak_hex, radix=10)

    def encrypt_digits(self, digits: str) -> str:
        """digits: a string of ONLY digit characters (no separators --
        see encrypt_formatted() below for values with dashes/spaces).
        Raises if shorter than FF3-1's minimum domain requirement."""
        if not digits.isdigit():
            raise ValueError(f"encrypt_digits expects digits only, got {digits!r}")
        if len(digits) < _MIN_DIGIT_LENGTH:
            raise ValueError(
                f"value {digits!r} ({len(digits)} digits) is shorter than FF3-1's "
                f"minimum domain requirement ({_MIN_DIGIT_LENGTH} digits for radix "
                f"10) -- this module is out of scope for values this short, not "
                f"silently padding or working around the constraint."
            )
        return self._cipher.encrypt(digits)

    def decrypt_digits(self, ciphertext_digits: str) -> str:
        if not ciphertext_digits.isdigit():
            raise ValueError(f"decrypt_digits expects digits only, got {ciphertext_digits!r}")
        return self._cipher.decrypt(ciphertext_digits)

    def encrypt_formatted(self, value: str) -> str:
        """Handles a value with non-digit separators (e.g. an SSN
        "239-65-9864" or a credit card "4111 1111 1111 1111") by
        encrypting only the digit sequence and reinserting it into the
        original template, so the output visually resembles the input
        format -- separators are NOT part of FF3-1's own domain, this is
        a thin wrapper around it, not a claim about the underlying
        cipher's own capabilities."""
        digits = re.sub(r"\D", "", value)
        encrypted_digits = self.encrypt_digits(digits)
        result = []
        digit_iter = iter(encrypted_digits)
        for ch in value:
            if ch.isdigit():
                result.append(next(digit_iter))
            else:
                result.append(ch)
        return "".join(result)

    def decrypt_formatted(self, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        decrypted_digits = self.decrypt_digits(digits)
        result = []
        digit_iter = iter(decrypted_digits)
        for ch in value:
            if ch.isdigit():
                result.append(next(digit_iter))
            else:
                result.append(ch)
        return "".join(result)
