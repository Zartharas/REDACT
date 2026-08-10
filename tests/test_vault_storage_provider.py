"""
Tests VaultStorageProvider and _VaultLockContext (src/anonymize.py) --
see VaultStorageProvider's own class docstring for the full "why" of its
design and its explicitly NOT-live-verified status.

No real Vault server is reachable from this environment (no network
access to install/run one here, same constraint as everywhere else in
this project that needs external infrastructure). Instead, this file
injects a small fake `hvac` module into sys.modules BEFORE importing
anonymize, implementing just enough of hvac's Client/secrets.kv.v2
surface (read_secret_version, create_or_update_secret with `cas`
support, an InvalidPath exception for missing paths) to exercise
VaultStorageProvider's actual logic -- the load/save round trip, the
CAS-based lock's acquire/release/staleness-force-acquire behavior.

This verifies VaultStorageProvider's OWN code is correct against the
documented hvac/Vault KV v2 API surface. It does NOT verify that surface
itself behaves the way this fake assumes against a real Vault server --
that needs a live Vault, which is exactly what's disclosed as unverified
in this environment throughout this file and VaultStorageProvider's own
docstring.
"""
import os
import sys
import time
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(REPO_ROOT, "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)


class FakeInvalidPath(Exception):
    pass


# Keyed by Vault URL, so two separately-constructed FakeClient instances
# pointed at the "same" fake Vault address share state -- mirroring two
# real processes talking to the same real Vault server, which matters for
# the lock tests below (one client acquires, a second client's own
# _VaultLockContext must see that acquisition).
_FAKE_VAULT_BACKENDS: dict[str, dict] = {}


class _FakeKV2:
    def __init__(self, backend: dict):
        self._backend = backend  # (mount_point, path) -> {"data": {...}, "version": int}

    def read_secret_version(self, path, mount_point):
        entry = self._backend.get((mount_point, path))
        if entry is None:
            raise FakeInvalidPath(path)
        return {"data": {"data": dict(entry["data"]),
                          "metadata": {"version": entry["version"]}}}

    def create_or_update_secret(self, path, secret, mount_point, cas=None):
        key = (mount_point, path)
        entry = self._backend.get(key)
        current_version = entry["version"] if entry else 0
        if cas is not None and cas != current_version:
            raise RuntimeError(f"cas mismatch: expected {cas}, current is {current_version}")
        new_version = current_version + 1
        self._backend[key] = {"data": dict(secret), "version": new_version}
        return {"data": {"version": new_version}}


class _FakeKV:
    def __init__(self, backend):
        self.v2 = _FakeKV2(backend)


class _FakeSecrets:
    def __init__(self, backend):
        self.kv = _FakeKV(backend)


class FakeClient:
    def __init__(self, url, token=None):
        self.url = url
        self.token = token
        backend = _FAKE_VAULT_BACKENDS.setdefault(url, {})
        self.secrets = _FakeSecrets(backend)


@pytest.fixture(autouse=True)
def fake_hvac(monkeypatch):
    """Installs the fake hvac module for the duration of one test, and
    gives each test a fresh backend (keyed by a unique fake URL) so tests
    don't see each other's state."""
    fake_module = types.ModuleType("hvac")
    fake_exceptions = types.ModuleType("hvac.exceptions")
    fake_exceptions.InvalidPath = FakeInvalidPath
    fake_module.exceptions = fake_exceptions
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "hvac", fake_module)
    monkeypatch.setitem(sys.modules, "hvac.exceptions", fake_exceptions)
    yield
    _FAKE_VAULT_BACKENDS.clear()


def _make_provider(url="vault://test-1", **kwargs):
    import anonymize
    return anonymize.VaultStorageProvider(url, vault_token="fake-token", **kwargs)


def test_load_on_empty_backend_returns_empty_dicts():
    provider = _make_provider()
    forward, reverse = provider.load()
    assert forward == {}
    assert reverse == {}


def test_save_then_load_round_trips():
    provider = _make_provider()
    forward = {"jane.doe@example.com": "tok_email_abc123"}
    reverse = {"tok_email_abc123": "jane.doe@example.com"}
    provider.save(forward, reverse)

    loaded_forward, loaded_reverse = provider.load()
    assert loaded_forward == forward
    assert loaded_reverse == reverse


def test_save_is_a_full_replace_not_a_merge():
    """save() overwrites the ENTIRE secret at that path -- unlike
    RedisStorageProvider's HSET, this is not additive. Documented
    behavior, verified directly."""
    provider = _make_provider()
    provider.save({"a": "tok_a"}, {"tok_a": "a"})
    provider.save({"b": "tok_b"}, {"tok_b": "b"})

    forward, reverse = provider.load()
    assert forward == {"b": "tok_b"}
    assert reverse == {"tok_b": "b"}


def test_save_incremental_not_supported_falls_back_to_base_default():
    """VaultStorageProvider deliberately does not override
    save_incremental() -- see its class docstring for why a "fake"
    incremental save would misrepresent this provider's actual
    performance characteristics. Confirms the base class default (False)
    is really what's in effect, which is what makes TokenStore.save()
    fall back to the correct (if slower) read-merge-write path."""
    provider = _make_provider()
    assert provider.save_incremental({"a": "tok_a"}, {"tok_a": "a"}) is False


def test_lock_for_save_acquire_and_release_round_trip():
    provider = _make_provider()
    with provider.lock_for_save():
        pass  # acquired and released without error


def test_lock_for_save_blocks_a_second_concurrent_holder():
    """Two separate provider instances pointed at the same fake Vault
    backend (mirroring two real processes against one real Vault
    server): while the first holds the lock, the second must NOT be able
    to acquire it (raises TimeoutError given a short acquire_timeout_s
    rather than hanging or silently proceeding unlocked)."""
    import anonymize

    url = "vault://shared-backend"
    provider_a = _make_provider(url=url)
    provider_b = _make_provider(url=url)

    lock_a = anonymize._VaultLockContext(
        provider_a._client, provider_a._mount_point, provider_a._lock_path,
        ttl_s=60.0, acquire_timeout_s=15.0,
    )
    lock_b = anonymize._VaultLockContext(
        provider_b._client, provider_b._mount_point, provider_b._lock_path,
        ttl_s=60.0, acquire_timeout_s=0.3,
    )

    with lock_a:
        with pytest.raises(TimeoutError):
            with lock_b:
                pass  # should never get here


def test_lock_for_save_force_acquires_a_stale_lock():
    """A lock whose acquired_at timestamp is older than ttl_s is treated
    as abandoned (a crashed holder) and force-acquired by the next
    caller, rather than blocking forever -- this is what compensates for
    Vault KV v2 having no native TTL/expiry the way Redis's SET ... PX
    does (see _VaultLockContext's own docstring)."""
    import anonymize

    url = "vault://stale-lock-backend"
    provider_a = _make_provider(url=url)
    provider_b = _make_provider(url=url)

    # Simulate an abandoned lock: write a lock record with an
    # acquired_at far enough in the past to exceed a short ttl_s below,
    # as if provider_a's process crashed while holding it.
    provider_a._client.secrets.kv.v2.create_or_update_secret(
        path=provider_a._lock_path,
        secret={"owner": "crashed-process-owner-token", "acquired_at": time.time() - 999},
        mount_point=provider_a._mount_point,
    )

    lock_b = anonymize._VaultLockContext(
        provider_b._client, provider_b._mount_point, provider_b._lock_path,
        ttl_s=1.0, acquire_timeout_s=5.0,
    )
    with lock_b:
        pass  # must succeed despite the "held" (but stale) lock above


def test_vault_storage_provider_end_to_end_via_tokenstore():
    """One level up: a real TokenStore using VaultStorageProvider as its
    backend, confirming the two integrate correctly, not just that
    VaultStorageProvider's own methods work in isolation."""
    import anonymize

    provider = _make_provider(url="vault://tokenstore-e2e")
    store = anonymize.TokenStore(provider, token_key="test-key")

    token = store.get_or_create_token("jane.doe@example.com", "EMAIL")
    store.save(force=True)

    # A second TokenStore instance, same backend -- mirrors a fresh
    # process/worker picking up what a previous one persisted.
    provider_2 = _make_provider(url="vault://tokenstore-e2e")
    store_2 = anonymize.TokenStore(provider_2, token_key="test-key")
    assert store_2.resolve(token) == "jane.doe@example.com"
