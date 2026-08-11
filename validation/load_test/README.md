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
(`docker-compose.yml`, unmodified — a 3-node OpenSearch cluster as of
2026-08-11, `redact-service` under gunicorn with `GUNICORN_WORKERS`
replicas behind `redact-lb`, Logstash with `pipeline.workers => 8`) on one
machine, and whether anything breaks (timeouts, dropped events, memory
exhaustion, the class of bug documented in `BUGS_AND_FIXES.md`) before
that ceiling is reached.

**Can't:** anything about a real production deployment — a real
multi-data-center OpenSearch topology, sustained terabytes/day ingestion,
or actual network-partition/failover behavior under production traffic.
`docker-compose.yml` now runs a genuine 3-node OpenSearch cluster (see
that file's own comments on why 3, not the 2 shown in OpenSearch's
official example) and multiple `redact-service` replicas behind an nginx
load balancer, both on one Docker host — real clustering/load-balancing
behavior, not a mock, but still one machine's worth of CPU/memory/network
shared across every container, not physically separate infrastructure.
This test script has not yet been re-run against the 3-node topology
specifically (added after this README was last updated) — the throughput
numbers below predate that change and were measured against the
single-node OpenSearch config. Stating a "runs at scale" claim on the
strength of this test alone would repeat the same shape of problem this
project's own `BUGS_AND_FIXES.md` exists to catch: a plausible-sounding
number that doesn't actually support the claim being made with it.

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
  `redact-opensearch-node1/2/3` (3-node cluster as of 2026-08-11, `-Xms512m
  -Xmx512m` each — see `docker-compose.yml`'s own comment on tuning this
  for your host, and why the per-node heap dropped from the old
  single-node service's `-Xms1g -Xmx1g` now that three nodes share the
  host) and `redact-service` (gunicorn's non-preload fork model means
  each `GUNICORN_WORKERS` worker per replica independently loaded its own
  copy of the spaCy/Presidio model — see `Dockerfile`'s comments on this
  tradeoff, and Bug 18/`docker-compose.yml`'s `GUNICORN_WORKERS` comment
  for the OOM this caused when scaled without a per-replica cap).
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

### 5,000,000 lines: harness prepared 2026-08-11, not yet run (Task #47)

`run_5m_load_test.sh` (repo root) runs this exact same harness at 5x the
previous scale, as a stretch goal toward narrowing the gap to real
production volume -- terabytes/day stays honestly out of reach without
real cloud spend/hardware (see `ROADMAP.md` item 12's closing paragraph),
this is not a claim of closing that gap, only of finding where this
project's single-machine ceiling sits somewhere before it.

**A real gap found and fixed before this was handed off, not left for
the run itself to discover:** `run_load_test.sh`'s poll-loop deadline
(`REDACT_LOAD_TEST_MAX_WAIT_SECONDS`) defaulted to a fixed 14,400s
(4 hours), sized for the 1,000,000-line runs actually completed so far.
At 5,000,000 lines, even the faster of those two measured rates
(~439 lines/sec) implies ~3.2 hours, and the slower one (~224 lines/sec)
implies ~6.2 hours -- past the old fixed deadline, which would have
falsely reported a deadline-exceeded failure on a pipeline that was
still healthy and converging, the exact false-FAIL shape Bug 15's
iteration-count fix above exists to prevent, just for a different
mechanism (wall-clock deadline instead of poll-count cap). Fixed at the
source: the default now scales with `N` (a conservative 100 lines/sec
floor, 1.5x safety margin, `run_load_test.sh` itself, not worked around
here) -- protects any future run at any scale, not just this one.

Disk-space and expected-runtime estimates are written into
`run_5m_load_test.sh`'s own header comment, extrapolated from the
1,000,000-line runs' measured numbers (~190 bytes/line corpus size,
~9.3% token-store growth rate, ~89.3% audit fan-out) rather than
measured directly -- this sandbox has no Docker daemon to run this
against. Handed off for the user to run; not yet executed anywhere.

### 1,000,000 lines again, 2026-08-10: field-gated NER as the live default, a real Logstash config bug, then a clean PASS

Rerun via `run_1m_load_test.sh` (repo root) to exercise everything added
since the 2026-08-08 run: service auth, the non-root Dockerfile,
Prometheus metrics, and — the actual point of this rerun — field-gated
NER (`detect_all_field_gated`) wired into `service.py` as the default
detection path, with `log_type` forwarded from Logstash's `http` filter
to `redact-service` for the first time ever at this scale.

**First attempt failed before a single line was processed.** The
`http` filter's body hash gained a second key
(`"log_type" => "%{log_type}"`) separated from the first by a comma —
valid-looking Ruby/JSON syntax, a hard parse error in Logstash's own
config DSL, which separates hash entries by whitespace only. Logstash
crashed at startup; since the `logstash` service has no healthcheck,
`docker compose ps` still reported it as running, and this script's own
poll loop read three consecutive `total=0` readings as "stable,"
reporting a fabricated `~5,208 lines/sec` for a run that had indexed
nothing. `RECONCILIATION: FAIL` did catch the actual mismatch (expected
1,000,000, got 0) — the throughput number above it just shouldn't have
been trusted, and after this run wasn't. Root-caused via
`docker compose logs logstash --tail 200` (not `redact-logstash` — that's
the container name, not the Compose service name).

**Fixed on three levels**, not just the one-line syntax fix: the comma
was removed from `logstash/redact-pipeline.conf`; `run_load_test.sh`'s
stability check now requires `TOTAL > 0` in addition to three consecutive
identical readings (an all-zero "stable" reading no longer exits the
poll loop early) and its throughput line is gated on
`RECONCILE_STATUS`, printing `n/a` instead of a fabricated number when
reconciliation didn't pass; and `run_1m_load_test.sh` now runs
`bin/logstash --config.test_and_exit` as a pre-flight step, catching this
exact class of mistake in seconds instead of after a full build-and-run
cycle. Full writeup: Bug 16 in `BUGS_AND_FIXES.md`.

**Clean rerun, same day:** pre-flight printed `Configuration OK` /
`Logstash config syntax OK.`, then the full run completed —

| Index | Expected | Actual | Result |
|---|---|---|---|
| `security-logs-anonymized-*` | 1,000,000 | **1,000,000** | exact |
| `security-logs-quarantine-*` | 0 | **0** | exact |
| `redact-audit-trail-*` | — | **893,150** | see note below |

Wall clock 2,276s, **~439.4 lines/sec end-to-end**.

**On the audit count matching 2026-08-08's exactly:** plausible, not
alarming — the corpus generator is seeded (`Faker.seed(42)`), so both
runs process the same 1,000,000 raw lines, and the majority of audit
events come from regex-detected types (EMAIL, SSN, CREDIT_CARD, IP, MRN)
that are identical between naive and field-gated detection. Only PERSON
detections can differ, and this project's own real-data validation
(`validation/real_data/`) already found field-gated's recall
statistically indistinguishable from naive's after the key-prefix
excision fix — an exact aggregate match is consistent with that, not
independent proof of it. Breaking this down by detected type via the
Prometheus `redact_detections_total{type=...}` metric would be needed
before citing this number as direct evidence of detection parity.

**On 439.4 vs. 224 lines/sec:** worth reporting, not worth concluding
anything from on its own. This is a single, uncontrolled run on a
different day against a different Docker Desktop session — image layer
caching, host load, and ordinary run-to-run variance are all live
confounds, and this project's own order-controlled A/B test (same
session, see `tests/README.md`) already found field-gated and naive
statistically indistinguishable at the algorithm level. Treat this as a
data point pending a controlled rerun, not a claim that field-gating (or
anything else) made the end-to-end pipeline faster.
