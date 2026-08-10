"""
Tests rotate_token_key (src/airflow_tasks.py), the engineering upgrade
that closed TOKEN_KEY's previous lack of any rotation mechanism.

Covers both halves of what that function's own docstring claims:
1. The mechanical part (new key generated, old key retired to a
   timestamped file) -- shared with rotate_pseudonymization_key, which
   already has informal coverage via the DAG's own live-run history (see
   README.md's Airflow section), but not a dedicated pytest assertion
   until now.
2. The semantic claim specific to tokens, and the one actually worth a
   real test rather than just a docstring assertion: a token minted
   BEFORE a key rotation must still resolve correctly AFTER it, because
   TokenStore's reversibility is lookup-table-based, not key-based. This
   is the one thing that would be silently, badly wrong if that claim
   turned out to be false.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import airflow_tasks  # noqa: E402
import anonymize  # noqa: E402


def test_rotate_token_key_retires_and_regenerates():
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "token_key.txt")
        retired_dir = os.path.join(tmpdir, "retired_keys")

        with open(key_path, "w") as f:
            f.write("original-key-value")

        result = airflow_tasks.rotate_token_key(key_path, retired_dir)

        assert result["retired_key_path"] is not None
        with open(result["retired_key_path"]) as f:
            assert f.read() == "original-key-value"

        with open(key_path) as f:
            new_key = f.read()
        assert new_key != "original-key-value"
        assert len(new_key) == 64  # secrets.token_hex(32) -> 64 hex chars


def test_rotate_token_key_first_run_no_prior_key():
    """No key file exists yet -- e.g. the very first scheduled run --
    should generate one without erroring or claiming to have retired
    something that never existed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "token_key.txt")
        retired_dir = os.path.join(tmpdir, "retired_keys")

        result = airflow_tasks.rotate_token_key(key_path, retired_dir)

        assert result["retired_key_path"] is None
        assert os.path.exists(key_path)


def test_tokens_minted_before_rotation_still_resolve_after():
    """The claim that actually matters: TokenStore reversibility survives
    a TOKEN_KEY rotation, because resolve() is a dict lookup keyed on the
    token string itself, not a recomputation from the key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = os.path.join(tmpdir, "token_store.json")

        store_before = anonymize.TokenStore(store_path, token_key="key-epoch-one")
        token = store_before.get_or_create_token("jane.doe@example.com", "EMAIL")
        store_before.save(force=True)

        # Simulate a rotation: a fresh TokenStore instance with a
        # DIFFERENT token_key, pointed at the same persisted store --
        # exactly what a service restart after rotate_token_key updates
        # REDACT_TOKEN_KEY would look like.
        store_after = anonymize.TokenStore(store_path, token_key="key-epoch-two")

        assert store_after.resolve(token) == "jane.doe@example.com", (
            "a token minted under the old key must still resolve after "
            "rotation -- resolution is lookup-table-based, not key-based"
        )

        # And a REPEAT of the same original value should still return the
        # SAME token (correlation preserved), not mint a new one under the
        # new key, even though store_after's own token_key differs.
        same_token_again = store_after.get_or_create_token("jane.doe@example.com", "EMAIL")
        assert same_token_again == token

        # A genuinely NEW value, never seen before, mints fine under the
        # new key and is immediately resolvable too.
        new_token = store_after.get_or_create_token("new.person@example.com", "EMAIL")
        assert store_after.resolve(new_token) == "new.person@example.com"
        assert new_token != token
