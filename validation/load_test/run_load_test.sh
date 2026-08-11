#!/usr/bin/env bash
# ROADMAP item 9: load testing beyond demo scale.
#
# Everything this project has verified so far (Bug 6's confirmation,
# Bug 3's timeout-burst fix, the gunicorn rerun) was against exactly
# 10,000 lines on a single Docker Desktop machine. This script scales
# that same methodology up -- same pipeline, same reconciliation check,
# bigger corpus -- to see where a single machine's vertical limits
# actually are.
#
# HONEST SCOPE, stated up front: this measures single-node,
# single-shard, one-machine throughput. It does NOT establish anything
# about a real production multi-node OpenSearch cluster, multiple
# redact-service replicas behind a load balancer, or actual
# terabytes/day volume -- that needs real infrastructure (cloud VMs, a
# real multi-data-node OpenSearch cluster) that isn't available here.
# What this DOES establish: the throughput ceiling of the exact stack
# this project ships (docker-compose.yml, unmodified) on one machine,
# and whether anything breaks (timeouts, dropped events, memory
# exhaustion) before that ceiling is reached. That's a real, useful
# data point -- just not the same claim as "runs at scale" in a
# production-cluster sense.
#
# Usage:
#   ./validation/load_test/run_load_test.sh [N]
#     N = number of synthetic log lines to generate (default 100000)
#
# Requires: Docker, Docker Compose, the same .env-based
# REDACT_PSEUDO_KEY / REDACT_AUDIT_KEY setup the rest of this project's
# Docker Compose runs already need.
set -euo pipefail

N="${1:-100000}"
CORPUS_PATH="data/load_test_corpus_${N}.jsonl"
RESULTS_DIR="validation/load_test/results"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
STATS_FILE="${RESULTS_DIR}/docker_stats_${N}_${TIMESTAMP}.log"
SUMMARY_FILE="${RESULTS_DIR}/summary_${N}_${TIMESTAMP}.txt"

mkdir -p "$RESULTS_DIR"

echo "=== Load test: N=${N} lines ==="

echo "--- Generating corpus (deterministic, seed 42) ---"
python3 src/generate_logs.py --n "$N" --out "$CORPUS_PATH" --dirty-ratio 0.3

echo "--- Exporting to raw per-source log files ---"
python3 src/export_raw_logs.py --input "$CORPUS_PATH" --output-dir data/raw

echo "--- Tearing down any previous stack (clean slate) ---"
docker compose down -v

echo "--- Starting docker stats capture in background ---"
# `docker stats` streams continuously by default (no --no-stream flag
# needed/valid here) -- backgrounded and killed once the run finishes.
docker stats --format \
  "{{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" \
  > "$STATS_FILE" 2>&1 &
STATS_PID=$!

echo "--- Starting the stack (docker compose up --build -d) ---"
START_TS=$(date +%s)
docker compose up --build -d

echo "--- Waiting for the stack to become healthy ---"
# Same healthcheck-gated startup docker-compose.yml already defines;
# this just waits for it from the outside instead of assuming a fixed
# sleep, since a bigger corpus doesn't change startup time but WILL
# change how long ingestion itself takes.
for i in $(seq 1 60); do
  # opensearch-node1 specifically, not "opensearch" -- the single-node
  # service was replaced with a 3-node cluster (opensearch-node1/2/3,
  # ROADMAP.md item 12) whose healthcheck lives on node1 and verifies all
  # 3 nodes actually joined (number_of_nodes:3), not just that node1 is up.
  if docker compose ps redact-service | grep -q "healthy" && \
     docker compose ps opensearch-node1 | grep -q "healthy"; then
    echo "Stack healthy after approx $((i * 10))s."
    break
  fi
  sleep 10
done

echo "--- Polling reconciliation until ingestion stabilizes ---"
# Rather than a fixed sleep (fine for 10,000 lines, not fine for
# 100,000+), poll the anonymized+quarantine total every 15s and stop
# once it hasn't changed for two consecutive checks -- the same signal
# "docker compose logs logstash --tail 30 shows no new activity" was
# checking for manually in the Bug 6 verification, just automated and
# count-based instead of eyeballing log lines.
PREV_TOTAL=-1
STABLE_COUNT=0
# Was a fixed MAX_POLLS=240 (240*15s = 1 hour ceiling) -- found to be too
# low at the 1,000,000-line scale (ROADMAP item 9 / BUGS_AND_FIXES.md Bug
# 15's confirmation run): that run took ~75 minutes to actually finish,
# so the loop hit its iteration cap and reported a false FAIL while
# ingestion was still healthy and climbing (971,625 -> 981,750 over the
# next 180s when checked manually). Replaced with a wall-clock deadline
# instead of an iteration count, so the cap scales with how long the run
# actually takes rather than an assumption baked in at 100,000-line
# scale. Default is generous (4 hours) since the cost of polling a few
# extra times is 15s of curl calls, not a real resource -- the real
# stability check below (3 consecutive unchanged totals) is what
# actually decides when ingestion is done; this is only a safety
# backstop against polling forever if something is genuinely stuck.
# Real gap found while scoping ROADMAP item 9's "push beyond 1M lines"
# stretch goal, 2026-08-11: the fixed 14400s (4h) default here was sized
# for the 1,000,000-line runs this project has actually completed
# (~75 minutes at the slower of the two measured rates, ~224 lines/sec).
# It does not scale automatically -- a straightforward 5,000,000-line run
# at that same conservative rate needs ~6.2 hours (22,320s), which the
# old fixed default would have cut off mid-run and falsely reported a
# deadline-exceeded failure on a pipeline that was still healthy and
# converging, exactly the false-FAIL failure mode Bug 15's own fix above
# was written to prevent for the iteration-count version of this same
# problem. Scaled here instead of just documented as "remember to raise
# this env var yourself": floor of 100 lines/sec (below both measured
# 1M-line rates, so this errs conservative) times a 1.5x safety margin,
# with the original 14400s kept as an absolute floor so small runs are
# unaffected. Still fully overridable via REDACT_LOAD_TEST_MAX_WAIT_SECONDS
# for a genuinely known-slower or known-faster environment.
DEFAULT_MAX_WAIT_SECONDS=$(python3 -c "print(max(14400, int(${N} / 100 * 1.5)))")
MAX_WAIT_SECONDS="${REDACT_LOAD_TEST_MAX_WAIT_SECONDS:-$DEFAULT_MAX_WAIT_SECONDS}"
POLL_START_TS=$(date +%s)
i=0
while true; do
  i=$((i + 1))
  NOW_TS=$(date +%s)
  if [ $((NOW_TS - POLL_START_TS)) -ge "$MAX_WAIT_SECONDS" ]; then
    echo "  reached ${MAX_WAIT_SECONDS}s poll deadline without 3 consecutive stable polls -- giving up."
    break
  fi
  sleep 15
  # track_total_hits=true is required here -- without it, Elasticsearch/
  # OpenSearch's _search API silently caps hits.total.value at exactly
  # 10,000 once the real count passes that (see BUGS_AND_FIXES.md Bug 13,
  # found by this exact bug in this exact poll loop the first time this
  # script ran at N=100000: the capped total held steady at 10,000 for
  # multiple polls, looking "stable," and the loop exited while real
  # ingestion was still only 28% done). reconcile.py already has this fix;
  # this poll loop needed the identical fix independently since it makes
  # its own separate curl calls.
  ANON=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_search?size=0&track_total_hits=true" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
  QUAR=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_search?size=0&track_total_hits=true" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
  TOTAL=$((ANON + QUAR))
  echo "  poll $i: anonymized=${ANON} quarantine=${QUAR} total=${TOTAL}"
  # Real bug, found via the 1,000,000-line run, 2026-08-10: a broken
  # Logstash config (a hard parse error at pipeline startup -- see
  # logstash/redact-pipeline.conf's own history for the specific bug)
  # crashed Logstash before it ever processed a single event. Every poll
  # read anonymized=0, quarantine=0, total=0 -- three (then four)
  # consecutive IDENTICAL values, which this loop's stability check
  # couldn't distinguish from "ingestion genuinely finished." It exited
  # "successfully" after 45s and the summary reported a nonsensical
  # ~5,200 lines/sec for a run that indexed exactly nothing. Fixed:
  # stability now additionally requires TOTAL > 0 -- an all-zero
  # "stable" reading no longer exits the loop early; it keeps polling
  # until the MAX_WAIT_SECONDS deadline instead, and the deadline
  # message below makes that failure mode visible rather than silently
  # reported as done.
  if [ "$TOTAL" -eq "$PREV_TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
    STABLE_COUNT=$((STABLE_COUNT + 1))
  else
    STABLE_COUNT=0
  fi
  PREV_TOTAL=$TOTAL
  if [ "$STABLE_COUNT" -ge 3 ]; then
    echo "  total stable for 3 consecutive polls (45s), assuming ingestion finished."
    break
  fi
done
if [ "$PREV_TOTAL" -eq 0 ]; then
  echo "  WARNING: poll loop ended with total=0 -- nothing was ever indexed." >&2
  echo "  This usually means Logstash failed to start (a config error) or" >&2
  echo "  never reached redact-service. Check 'docker compose logs logstash'" >&2
  echo "  before trusting anything below this line." >&2
fi
END_TS=$(date +%s)
ELAPSED=$((END_TS - START_TS))

echo "--- Stopping docker stats capture ---"
kill "$STATS_PID" 2>/dev/null || true

echo "--- Final reconciliation ---"
set +e  # reconcile.py exits 1 on a FAIL, which would otherwise kill this
        # script immediately under `set -e` before the summary below gets
        # written -- captured explicitly instead so the summary always
        # gets written, pass or fail.
python3 validation/load_test/reconcile.py | tee -a "$SUMMARY_FILE"
RECONCILE_STATUS=${PIPESTATUS[0]}
set -e

# Guard against reporting a meaningless throughput number when
# reconciliation didn't pass or nothing was ever indexed (RECONCILE_STATUS
# nonzero, or the poll loop's own PREV_TOTAL=0 warning above) -- this is
# exactly what silently reported ~5,200 "lines/sec" for the all-zero
# 2026-08-10 run before this guard existed.
if [ "$RECONCILE_STATUS" -eq 0 ]; then
  THROUGHPUT=$(python3 -c "print(f'{${N} / ${ELAPSED}:.1f}')" 2>/dev/null || echo "n/a")
else
  THROUGHPUT="n/a (reconciliation did not pass -- see above, do not trust a throughput number from a failed run)"
fi

{
  echo ""
  echo "=== Load test summary ==="
  echo "Lines: ${N}"
  echo "Wall clock (start of 'up' to stable ingestion): ${ELAPSED}s"
  echo "Approx throughput: ${THROUGHPUT} lines/sec end-to-end (export -> Logstash -> redact-service -> OpenSearch)"
  echo "docker stats samples: ${STATS_FILE}"
} | tee -a "$SUMMARY_FILE"

echo ""
echo "--- Leaving the stack running for manual inspection. ---"
echo "--- Run 'docker compose down -v' when done. ---"
echo ""
echo "Summary written to: ${SUMMARY_FILE}"

exit $RECONCILE_STATUS
