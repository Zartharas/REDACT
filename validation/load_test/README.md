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

**DONE — clean, complete, verified pass at N=100,000, 2026-08-07** (run
by the user locally). Three runs were needed to get there; the first two
were cut short by bugs in this test's own harness, both found, fixed, and
documented along the way. Final result, from the third (clean) run:

| Index | Expected | Actual | Result |
|---|---|---|---|
| `security-logs-anonymized-*` | 100,000 | **100,000** | exact |
| `security-logs-quarantine-*` | 0 | **0** | exact |
| `redact-audit-trail-*` | 89,159 (the pipeline's own fan-out count) | **89,159** | exact |

Wall clock: 400s, corpus generation/export included. **True end-to-end
throughput at this scale: ~250 lines/sec** (file input → `http` filter →
`redact-service` → OpenSearch write, one Docker Desktop machine, default
resource allocation). No timeout/error bursts beyond the already-fixed
Bug 3 startup cluster.

### How three runs became necessary

**Run 1** looked like a failure at first — `security-logs-anonymized-*`
reported exactly 10,000 regardless of the true 100,000-line input. That
turned out to be a false alarm in the test tooling, not the pipeline:
Elasticsearch/OpenSearch's `_search` API caps `hits.total.value` at
10,000 by default unless `track_total_hits=true` is set, and
`reconcile.py` was missing it. Re-querying directly with
`track_total_hits=true` showed the main pipeline was actually exact —
100,000/0 — but `redact-audit-trail-*` had a real shortfall, 55,577 of a
confirmed 89,159 sent (confirmed via Logstash's own pipeline stats API).
Root cause: the audit branch's document ID (`authentication_tag`, an HMAC
over content plus a *second*-granularity timestamp) collided for
genuinely distinct audit events sharing identical content within the same
wall-clock second — Bug 4's exact failure mode, reintroduced in a branch
Bug 4's actual fix (a random UUID) was never applied to. Fixed in
`logstash/redact-pipeline.conf` (a fresh random UUID generated per audit
clone, replacing the content-derived ID as the document's primary key).
Full writeup: `BUGS_AND_FIXES.md` Bug 12.

**Run 2**, meant to confirm the Bug 12 fix, FAILed reconciliation again —
but for a *different* reason. `reconcile.py` had been fixed, but
`run_load_test.sh`'s own stability-poll loop makes its own separate
`curl` calls rather than reusing `reconcile.py`, and had the identical
missing `track_total_hits=true`. The poll loop's capped queries plateaued
at exactly 10,000 for three consecutive polls once the real count crossed
that threshold, read as "ingestion finished," and the run exited 118
seconds early — real ingestion was still only 28,375/100,000 done. Fixed
the same way, independently, in `run_load_test.sh` itself. Full writeup:
`BUGS_AND_FIXES.md` Bug 13 (both occurrences).

**An earlier ~950-1,000 lines/sec throughput estimate, written into this
README and `ROADMAP.md` after runs 1 and 2, was itself invalid** — both
of those runs stopped timing at their respective premature "stable"
points, not real completion, so neither elapsed-time measurement was ever
trustworthy. The ~250 lines/sec figure above, from run 3's genuinely
complete pass, is the first real throughput number this test has
produced, and supersedes the earlier one everywhere it was written down.

**This is exactly the kind of finding this test was built to surface** —
a bug invisible at 10,000 lines that only appears once the system runs
long enough, and with enough content repetition, for a coarse timestamp
to stop being a reliable uniqueness guarantee, plus a reminder that a
test harness's own measurement code needs the same scrutiny as the
system it's testing. It's also a good illustration of this project's own
stated scope limits: this was a single-machine test, and it still found
a real, previously-undocumented data-loss bug — which says more about
how much a single-machine test *can* find than it does about having
exhausted what a real multi-node production deployment might
additionally surface.

### 1,000,000 lines, 2026-08-08: the natural next step, and it found the biggest bug yet

Running at 10x this scale again — `./validation/load_test/run_load_test.sh 1000000` —
answered the "does ~250 lines/sec hold flat or degrade further" question
above directly: it degraded, badly. The first attempt failed
reconciliation outright, throughput collapsing from ~250 lines/sec to
~3/sec well before the run finished. Root cause: `TokenStore.save()`
(the read-merge-write fix Bug 14 introduced) rewrote the entire persisted
token store on every single request — cheap at 100,000-line scale,
a 2.927-second-per-request wall once the store reached 93,279 entries at
1,000,000-line scale. That's Bug 15 in `BUGS_AND_FIXES.md`, the most
consequential bug this project has found — invisible at every scale
tested before this one, exactly the kind of thing only a full order-of-
magnitude jump surfaces.

A same-day debounce mitigation, then a same-day real fix (incremental
per-entry writes instead of rewriting the whole store), resolved it. The
rerun after both: **`security-logs-anonymized-*` = 1,000,000 exact,
`security-logs-quarantine-*` = 0 exact, `redact-audit-trail-*` = 893,150
exact, reconciliation PASS**, ~224 lines/sec average across the full run
— close to the 100,000-line baseline despite the store growing to
hundreds of thousands of entries along the way. Full root-cause writeup,
the mitigation-vs-fix distinction, and what's still open: Bug 15 in
`BUGS_AND_FIXES.md`.

**Still open:** runs beyond 1,000,000 lines, to see how far the current
fix's remaining O(n) compaction cost (now paid far less often, not
eliminated) can be pushed before it becomes visible again.
