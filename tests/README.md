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
(`_build_ner_candidate`, `_remap_hit`, `run_evaluation(...,
use_field_gate=True)`) -- the engineering upgrade meant to close the
documented PERSON recall gap between the whole-line "tiered" strategy
(0.113) and "naive" (0.359). Two implementations, both same day
(2026-08-09), both mentioned here since the second replaced the first:

1. First implementation (`_mask_regex_covered_fields`, now superseded):
   replaced regex-covered field spans with same-length `#` placeholders.
   Run by the user locally against the real model: the recall fix worked
   (0.356, nearly matching naive), but the throughput-preservation claim
   it was designed for did NOT hold up -- measured slower than naive
   (~100 vs. ~119 events/sec), not faster, because same-length masking
   doesn't reduce what spaCy has to process, and the one path that could
   skip the NER call entirely rarely fires on lines that actually contain
   a PERSON.
2. Second implementation (`_build_ner_candidate` + `_remap_hit`), built
   the same day specifically to fix that: excises (removes) regex-covered
   spans instead of masking them, so the candidate passed to NER is
   actually shorter, with offsets remapped back to the original line's
   coordinates afterward. This file's tests were rewritten to match --
   confirming candidates are measurably shorter when something is
   excised, and, the correctness-critical piece, that offsets round-trip
   back to the exact original substring (including the non-trivial case
   of a hit sitting after an excised span). Monkeypatches `detect.scan_ner`
   to a recording stub rather than the real spaCy/Presidio model (same
   reason as `test_service_auth.py` below: no model download possible in
   this environment), so what's verified here is the excision/remapping
   mechanics, not real recall/throughput numbers for this second version.

   **Re-measured by the user locally against the real model, same day:**
   the recall prediction held almost exactly (0.360 vs. the first
   version's 0.356, precision 0.658 vs. 0.657), and throughput improved
   substantially (~110 vs. ~100 events/sec) but did not fully close the
   gap to naive (~114.5) -- 4.3% slower, down from 16.1% slower, real
   progress but not a net throughput win. Honest conclusion: this is a
   recall fix at close to throughput parity with naive, not a faster
   replacement for the whole-line tiered strategy. Full history and
   reasoning in README.md's comparison table and
   `_build_ner_candidate`'s own docstring.

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
