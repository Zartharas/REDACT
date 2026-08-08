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
   quarantine total, queried with `track_total_hits=true` — see the
   Status section below for why that matters — stops changing across
   three consecutive 15-second polls, replacing the fixed `sleep 90` used
   in earlier manual verification runs — that fixed sleep was calibrated
   for 10,000 lines and would either under- or over-wait at a different
   scale).
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

**Two runs completed, 2026-08-07, at N=100,000** (run by the user
locally), the second needed because the first run's own harness had a bug
that cut it short. Results and what they found, across both runs:

- **Corpus generation and export:** as expected, not the bottleneck
  (100,000 lines generated and split into the three raw source files in
  well under a minute).
- **Main pipeline (detection, anonymization, quarantine routing):**
  correct at 10x demo scale. `security-logs-anonymized-*` landed at
  exactly 100,000 documents (verified with `track_total_hits=true` — see
  the note below), `security-logs-quarantine-*` at exactly 0, matching
  the 100,000-line input exactly. No timeouts or errors beyond the
  known, already-fixed Bug 3 startup cluster.
- **Approximate throughput:** ~950-1,000 events/sec sustained,
  end-to-end (file input → `http` filter → `redact-service` → OpenSearch
  write), on one Docker Desktop machine with the stack's default resource
  allocation.
- **The harness itself had a bug, in two independent places, found across
  both runs.** Run 1's *final reconciliation* query (`reconcile.py`) was
  missing `track_total_hits=true`, so Elasticsearch/OpenSearch's default
  10,000-hit accurate-counting cap made `security-logs-anonymized-*`
  initially report exactly 10,000 no matter the real count —
  indistinguishable at first glance from a real ceiling. Fixed by adding
  `track_total_hits=true` to every `_search` call in `reconcile.py`. Run 2
  then hit the *same* bug a second time, independently, in
  `run_load_test.sh`'s own stability-poll loop — which makes its own
  separate `curl` calls rather than reusing `reconcile.py`, and had the
  identical missing parameter. The poll loop's capped queries plateaued
  at exactly 10,000 for three consecutive polls once the real count
  crossed that threshold, read as "ingestion finished," and exited the
  run 118 seconds early — real ingestion was still only 28,375/100,000
  done at that point (confirmed by run 2's *final* reconciliation call,
  which correctly used the already-fixed `reconcile.py`, so it caught
  its own poll loop's premature exit rather than silently reporting
  success). See `BUGS_AND_FIXES.md` Bug 13 for the full writeup of both
  occurrences; fixed the second time in `run_load_test.sh` itself.
- **A real pipeline bug was found and fixed as a direct result of this
  test**, not a script bug: `redact-audit-trail-*` landed at only 55,577
  documents out of the 89,159 audit events Logstash's own pipeline stats
  confirmed were correctly sent. Root cause: the audit branch's
  document ID (`authentication_tag`, an HMAC over event content plus a
  *second*-granularity timestamp) collided for genuinely distinct audit
  events sharing identical content within the same wall-clock second —
  Bug 4's exact failure mode, reintroduced in a branch Bug 4's actual fix
  (a random UUID) was never applied to. See `BUGS_AND_FIXES.md` Bug 12
  for the full writeup, root-cause evidence (Logstash pipeline stats
  showing sent-vs-stored counts), and the fix (a random UUID generated
  per audit clone in `logstash/redact-pipeline.conf`'s `ruby` filter,
  replacing the content-derived ID as the document's primary key).

**This is exactly the kind of finding this test was built to surface** —
a bug invisible at 10,000 lines that only appears once the system runs
long enough, and with enough content repetition, for a coarse timestamp
to stop being a reliable uniqueness guarantee. It's also a good
illustration of this project's own stated scope limits: this was a
single-machine test, and it still found a real, previously-undocumented
data-loss bug — which says more about how much a single-machine test
*can* find than it does about having exhausted what a real multi-node
production deployment might additionally surface.

**Not yet done:** a clean run with both harness bugs fixed (the
`run_load_test.sh` poll-loop fix landed after run 2 already exited early,
so run 2's own numbers — 28,375 anonymized, 19,716 audit records — are a
mid-flight snapshot from a run that was cut short, not a real result to
compare against anything). The concrete next step is simply re-running
`./validation/load_test/run_load_test.sh 100000` now that both fixes are
in place, to get one clean, complete pass confirming: the main pipeline
still holds exactly at 100,000/0 (strong prior it will, since run 1
already confirmed this via direct `track_total_hits=true` queries outside
the buggy poll loop), and — the actual open question — whether
`redact-audit-trail-*` now lands exactly at the real fan-out count with
the Bug 12 fix in place. After that, runs at additional sizes (e.g.
1,000,000) to see whether throughput holds roughly flat or degrades are
the next steps beyond this item's original scope.
