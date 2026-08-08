# Load testing beyond demo scale (ROADMAP item 9)

Every number in `BUGS_AND_FIXES.md` and the main measurement section of
`README.md` was measured against exactly 10,000 synthetic log lines on one
Docker Desktop machine. That's enough to find and fix real pipeline bugs
(and it did — see `BUGS_AND_FIXES.md` bugs 1-11), but it says nothing about
throughput at anything resembling production log volume. This directory is
the scoped attempt to close that gap honestly, without overclaiming what a
single-machine test can actually establish.

## What this test can and can't tell you

**Can:** the throughput ceiling of the exact stack this project ships
(`docker-compose.yml`, unmodified — single-node OpenSearch,
`redact-service` under gunicorn with `--workers $(nproc)`, Logstash with
`pipeline.workers => 8`) on one machine, and whether anything breaks
(timeouts, dropped events, memory exhaustion, the class of bug documented
in `BUGS_AND_FIXES.md`) before that ceiling is reached.

**Can't:** anything about a real production deployment — a multi-data-node
OpenSearch cluster, multiple `redact-service` replicas behind a load
balancer, sustained terabytes/day ingestion, or network-partition/failover
behavior. That needs real infrastructure (cloud VMs, an actual
multi-node OpenSearch cluster) that isn't available in this project's
development environment. Stating a "runs at scale" claim on the strength
of this test alone would repeat the same shape of problem this project's
own `BUGS_AND_FIXES.md` exists to catch: a plausible-sounding number that
doesn't actually support the claim being made with it.

## Usage

Requires Docker, Docker Compose, and the same `.env`-based
`REDACT_PSEUDO_KEY`/`REDACT_AUDIT_KEY` setup every other Docker Compose run
in this project already needs.

```bash
./validation/load_test/run_load_test.sh 100000
```

The argument is the number of synthetic log lines to generate (default
100,000 — 10x the demo-scale baseline). This:

1. Generates a fresh, deterministic corpus at that size
   (`src/generate_logs.py --n N --dirty-ratio 0.3`, same seed-42 convention
   as everywhere else in this project).
2. Exports it to the raw per-source log files Logstash actually reads
   (`src/export_raw_logs.py`).
3. Does a clean `docker compose down -v` / `up --build -d`, timing from
   the start of `up` to when ingestion stabilizes (the anonymized +
   quarantine total stops changing across two consecutive 15-second
   polls, replacing the fixed `sleep 90` used in earlier manual
   verification runs — that fixed sleep was calibrated for 10,000 lines
   and would either under- or over-wait at a different scale).
4. Captures `docker stats` for all three containers throughout the run
   (CPU%, memory usage, memory% — written to
   `validation/load_test/results/docker_stats_<N>_<timestamp>.log`).
5. Runs the same `_search?size=0`-based reconciliation check used
   throughout `BUGS_AND_FIXES.md` (anonymized + quarantine should equal
   the exported line count, exactly — see Bug 6 and Bug 8 for why
   `_search` and not `_cat/indices`).
6. Writes a summary (elapsed time, approximate end-to-end throughput in
   lines/sec, reconciliation result) to
   `validation/load_test/results/summary_<N>_<timestamp>.txt`.

The stack is left running afterward for manual inspection (check
`docker compose logs`, query OpenSearch directly, etc.) — run
`docker compose down -v` when done.

`reconcile.py` is also usable standalone for a one-off manual check:

```bash
python3 validation/load_test/reconcile.py
```

## What to look for in the results

- **Reconciliation must still hold exactly** at whatever scale is tested
  — anonymized + quarantine == exported line count. If it doesn't, that's
  a real bug (data loss or duplication under load), not just a slow
  number, and should be written up in `BUGS_AND_FIXES.md` with the same
  rigor as bugs 1-11.
- **Timeout/error bursts beyond the known startup-only cluster** (Bug 3):
  a short burst in the first ~30-60 seconds while the model warms up is
  expected and already fixed/documented; a *sustained* timeout rate
  during steady-state ingestion at higher volume would be a new finding,
  not a repeat of Bug 3.
- **Memory growth over the run** in the `docker_stats` log, especially for
  `redact-opensearch` (single-node, `-Xms1g -Xmx1g` — see
  `docker-compose.yml`'s own comment on tuning this for your host) and
  `redact-service` (gunicorn's non-preload fork model means each of the
  `$(nproc)` workers independently loaded its own copy of the spaCy/
  Presidio model — see `Dockerfile`'s comments on this tradeoff).
- **Throughput trend as N increases**: run at a few sizes (e.g. 10,000 /
  100,000 / 1,000,000) and compare lines/sec — a roughly flat number
  across sizes suggests the pipeline is I/O- or CPU-bound in a way that
  scales linearly with volume (expected, healthy); a number that drops
  sharply at higher N suggests a resource ceiling being approached
  (worth investigating which container's `docker_stats` shows the
  saturation).

## Status

**Not yet run.** This script and methodology were written and
shell-syntax-checked (`bash -n`), and the corpus-generation step was
timed standalone (5,000 lines in ~2.4s, so 100,000 lines should take
roughly 45-50s of that step alone — not the bottleneck). The actual
Docker-dependent run has not been executed, since this project's
development sandbox has no Docker runtime available (see the note at the
top of `docker-compose.yml` and throughout `BUGS_AND_FIXES.md` for the
recurring pattern of Docker-dependent work in this project being handed
off to be run locally). Running this at a few sizes and folding the
results back into this README and `ROADMAP.md` item 9 is the concrete
next step.
