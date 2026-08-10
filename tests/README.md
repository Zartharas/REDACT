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
