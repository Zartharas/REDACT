# Automated test suite

Everything under this directory wraps the existing `validation/*.py` scripts
in `pytest`, rather than reimplementing their logic. Each test runs the
real script as a subprocess and asserts on its exit code and/or printed
output — the same thing a person sees running it by hand. See
`tests/conftest.py`'s own docstring for why: two copies of the same
assertion (one in the script, one duplicated in a pytest test) is exactly
the kind of drift risk that caused the overlapping-span bug documented in
`BUGS_AND_FIXES.md`.

```bash
pip install pytest
pytest tests/                      # everything reachable in this environment
pytest tests/test_fast_validation.py   # pure Python, no external services, ~5s
pytest tests/test_redis_validation.py  # needs a live Redis (auto-skips if none reachable)
```

## What's covered here vs. not

**Covered** (`test_fast_validation.py`, runs in CI on every push): the
TokenStore/WAL correctness and O(n)-growth regression guards from Bug 15,
all three syslog coverage rounds, the entropy UUID-exclusion regression
check. All deterministic, no Docker, no spaCy, no network.

**Covered, conditionally** (`test_redis_validation.py`, runs in CI with a
Redis service container): the Redis `StorageProvider` correctness tests —
skipped automatically if no Redis is reachable.

**Covered** (`test_key_rotation.py`, runs in CI on every push): the new
`rotate_token_key` task (`src/airflow_tasks.py`) -- key file retirement
and regeneration, the first-run-no-prior-key case, and the claim that
actually matters: a token minted before a `TOKEN_KEY` rotation still
resolves correctly after it, because `TokenStore` reversibility is
lookup-table-based, not key-based.

**Covered** (`test_field_level_gate.py`, runs in CI on every push): the
field-level NER gate added to `src/evaluate.py`
(`_mask_regex_covered_fields`, `run_evaluation(..., use_field_gate=True)`)
-- the engineering upgrade meant to close the documented PERSON recall
gap between the whole-line "tiered" strategy (0.113) and "naive" (0.359).
Monkeypatches `detect.scan_ner` to a recording stub rather than the real
spaCy/Presidio model (same reason as `test_service_auth.py` below: no
model download possible in this environment), so what's actually verified
here is the masking mechanics -- which characters get masked before NER
runs, and when the NER call is skipped entirely vs. made -- not real
recall numbers. **Real numbers now exist, run by the user locally
2026-08-09** (`python src/evaluate.py` against the full corpus with the
real model): the recall fix works as designed (0.356, nearly matching
naive), but the throughput-preservation claim this design started with
did NOT hold up -- field-gated measured slower than naive (~100 vs. ~119
events/sec), not faster, because the "skip NER entirely" path this
depends on rarely fires in practice. Full numbers and root-cause
reasoning in README.md's comparison table and
`_mask_regex_covered_fields`'s own updated docstring.

**Covered** (`test_vault_storage_provider.py`, runs in CI on every
push): `VaultStorageProvider` and its CAS-based `_VaultLockContext`
(`src/anonymize.py`) -- the engineering upgrade closing this project's
previously-unimplemented Vault backend. No live Vault server is
reachable from this environment, so these 8 tests inject a small fake
`hvac` module into `sys.modules` (a minimal in-memory stand-in for
Vault's KV v2 `read_secret_version`/`create_or_update_secret` API,
including `cas` version-conflict behavior) rather than skip Vault
coverage entirely. This verifies `VaultStorageProvider`'s own logic --
load/save round trip, `save()` being a full replace rather than a merge,
the lock correctly blocking a second concurrent holder and correctly
force-acquiring a lock left stale by a simulated crashed process, and an
end-to-end check through a real `TokenStore` -- against Vault's
documented API surface. It does NOT confirm that surface behaves the way
the fake assumes against a real Vault server; see
`VaultStorageProvider`'s own docstring for that disclosure.

**Covered** (`test_metrics.py`, runs in CI on every push): the Prometheus
metrics added to `src/service.py` -- `/metrics` stays behind the same
`X-Redact-Api-Key` check as `/anonymize` (unlike `/health`), the four
metric families are present in the scrape output, `redact_detections_total`
increments per detected span type, `redact_store_save_total` records
whether a save actually persisted or was skipped by the debounce, and
`redact_anonymize_request_seconds` records a latency sample. Also
exercises `service._metric()`'s idempotent-registration helper indirectly
-- these tests are what would fail with
`prometheus_client.registry.DuplicateTimeseries` if that helper
regressed, since they force the same `sys.modules`-eviction re-import
pattern `test_service_auth.py` uses.

**Covered** (`test_service_auth.py`, runs in CI on every push): the
`X-Redact-Api-Key` auth check added to `src/service.py` -- `/health`
stays open, `/anonymize` rejects a missing or wrong key with 401, and
accepts the correct one. Tests this without a live Docker stack or the
real spaCy model by monkeypatching `detect._get_analyzer()` to a no-op
before importing `service` (see that file's own docstring for why that's
needed: `service.py` calls the analyzer warmup at module level, on
purpose, so it can't just be skipped by importing under `__main__`).

**Not covered here, deliberately** — these are real, verified parts of
this project (see `BUGS_AND_FIXES.md` and `ROADMAP.md` for their own
verification history), just not practical to run on every push from a
standard CI runner:
- The full Docker Compose stack (`validation/load_test/`, the
  10,000/100,000/1,000,000-line reconciliation runs) — needs Docker
  Compose orchestrating three containers, and a full run takes anywhere
  from minutes to over an hour at the largest scale tested.
- Anything needing the `en_core_web_lg` spaCy model (`validate.py`,
  `src/evaluate.py`, `validation/baseline_presidio_default.py`,
  `validation/real_data/inject_and_evaluate.py`) — a multi-hundred-MB
  model download, and this project's own dev sandbox has never had
  network access to fetch it either (see `BUGS_AND_FIXES.md` for how
  those specific numbers were verified: run by a human, locally).
- `validation/real_name_frequency/build_real_name_test.py` — needs the
  SSA/Census raw data downloaded first (`download_name_data.sh`), which
  isn't committed to the repo (see `validation/real_name_frequency/raw/README.md`).

If a future CI job wants to cover the spaCy- or Docker-dependent paths,
the honest way to do it is a separate, explicitly slower/scheduled
workflow (nightly, not on every push) — not folding them into the fast
job and making every push wait on a multi-container stack or a model
download.
