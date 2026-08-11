"""
Tests src/fpe_provider.py's FPEDigitsProvider -- FF3-1 format-preserving
encryption evaluated as an optional TokenStore alternative for digit-only
fields (BUGS_AND_FIXES.md, "Engineering upgrade 5" / ROADMAP.md item 13).

All deterministic, no external dependency beyond the `ff3` library itself
(a real NIST SP 800-38G Rev.1 implementation, not something this project
wrote or is claiming to have cryptanalyzed -- these tests check this
module's own wrapper logic: round-trip correctness, format preservation,
domain-size enforcement, and that the key genuinely changes output. They
do NOT attempt to verify FF3-1's own cryptographic security, which is
NIST's and the library's responsibility, not this test file's.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

# ff3 (requirements-fpe.txt) is optional -- most environments running
# `pytest tests/` per tests/README.md's own quick-start will NOT have it
# installed (it's a separate, opt-in requirements file, same as Redis/
# Vault/Kafka). importorskip here means this file cleanly SKIPS when ff3
# isn't available, rather than raising an ImportError during collection
# that used to abort pytest's ENTIRE run (see src/fpe_provider.py's own
# header comment for the real bug this fixes -- ff3 used to be imported
# at fpe_provider's module level, so even importing THIS test file
# without ff3 installed would fail before a single test ran).
pytest.importorskip("ff3")

from fpe_provider import FPEDigitsProvider, DEFAULT_TWEAK  # noqa: E402

# Real NIST SP 800-38G Rev. 1 published test-vector key (128-bit) --
# using a published test key here, not a random one, so a reader can
# independently cross-check this module's output against NIST's own
# documented test vectors if they want to (not attempted in this test
# file itself, since NIST's test vectors use a specific tweak/plaintext
# pair this module's DEFAULT_TWEAK doesn't match -- see the encrypt/
# decrypt round-trip tests below for what IS actually checked here).
KEY_A = "EF4359D8D580AA4F7F036D6F04FC6A94"
KEY_B = "AEE87D0D485B3AFD12BD1E0AF9C20DD" + "0"  # different 128-bit key


def test_round_trip_ssn_shaped_value():
    p = FPEDigitsProvider(key_hex=KEY_A)
    encrypted = p.encrypt_formatted("239-65-9864")
    decrypted = p.decrypt_formatted(encrypted)
    assert decrypted == "239-65-9864"


def test_round_trip_credit_card_shaped_value():
    p = FPEDigitsProvider(key_hex=KEY_A)
    encrypted = p.encrypt_formatted("4111 1111 1111 1111")
    decrypted = p.decrypt_formatted(encrypted)
    assert decrypted == "4111 1111 1111 1111"


def test_encrypted_output_preserves_format_and_length():
    p = FPEDigitsProvider(key_hex=KEY_A)
    original = "239-65-9864"
    encrypted = p.encrypt_formatted(original)
    assert len(encrypted) == len(original)
    # Separator positions must match exactly -- format-PRESERVING, not
    # just same-length.
    for orig_ch, enc_ch in zip(original, encrypted):
        assert orig_ch.isdigit() == enc_ch.isdigit()
    # And the encrypted digits are NOT the same as the original digits
    # (a trivial sanity check, not a cryptanalytic claim) -- confirms
    # this isn't accidentally an identity no-op.
    assert encrypted != original


def test_different_keys_produce_different_ciphertext():
    """The actual property that makes key rotation dangerous for this
    scheme (see module docstring) -- confirmed directly: two different
    keys really do produce different output for the same input, which
    is exactly why losing/rotating the key makes old ciphertext
    unrecoverable."""
    value = "239-65-9864"
    p_a = FPEDigitsProvider(key_hex=KEY_A)
    p_b = FPEDigitsProvider(key_hex=KEY_B)
    assert p_a.encrypt_formatted(value) != p_b.encrypt_formatted(value)


def test_wrong_key_does_not_decrypt_correctly():
    """Confirms the actual rotation-danger property end to end: data
    encrypted under one key cannot be recovered with a different key
    (silently returns garbage of the same format, not an error and not
    the real value) -- this is the concrete mechanism behind the
    module docstring's "old ciphertext becomes permanently
    unrecoverable" claim about key rotation."""
    value = "239-65-9864"
    p_a = FPEDigitsProvider(key_hex=KEY_A)
    p_b = FPEDigitsProvider(key_hex=KEY_B)

    encrypted = p_a.encrypt_formatted(value)
    wrongly_decrypted = p_b.decrypt_formatted(encrypted)
    assert wrongly_decrypted != value


def test_rejects_values_shorter_than_ff3_1_minimum_domain():
    """FF3-1's own minimum domain requirement (radix^minLen >=
    1,000,000) works out to 6 digits minimum for radix 10 -- this
    module must refuse shorter values outright, not silently pad or
    work around the constraint."""
    p = FPEDigitsProvider(key_hex=KEY_A)
    with pytest.raises(ValueError):
        p.encrypt_digits("1234")  # 4 digits, below the 6-digit minimum


def test_accepts_minimum_length_value():
    p = FPEDigitsProvider(key_hex=KEY_A)
    encrypted = p.encrypt_digits("123456")  # exactly 6 digits
    assert len(encrypted) == 6
    assert p.decrypt_digits(encrypted) == "123456"


def test_rejects_non_digit_input_to_raw_methods():
    p = FPEDigitsProvider(key_hex=KEY_A)
    with pytest.raises(ValueError):
        p.encrypt_digits("239-65-9864")  # has separators -- wrong method for this


def test_default_tweak_is_56_bits_ff3_1_not_legacy_ff3():
    """Confirms this module is actually using FF3-1's corrected tweak
    length (56 bits / 7 bytes), not the deprecated original FF3's 64-bit
    tweak -- see module docstring for why this distinction is the whole
    point of using FF3-1 at all rather than the algorithm NIST's own
    2017 cryptanalysis flagged."""
    tweak_bytes = bytes.fromhex(DEFAULT_TWEAK)
    assert len(tweak_bytes) == 7, (
        f"DEFAULT_TWEAK is {len(tweak_bytes)} bytes, expected 7 (56 bits) for "
        f"FF3-1 -- using an 8-byte tweak here would silently select the "
        f"deprecated, cryptanalyzed original FF3 construction instead"
    )
