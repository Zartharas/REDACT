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
  if docker compose ps redact-service | grep -q "healthy" && \
     docker compose ps opensearch | grep -q "healthy"; then
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
MAX_POLLS=240  # 240 * 15s = 1 hour ceiling; raise if testing N well into the millions
for i in $(seq 1 $MAX_POLLS); do
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
  if [ "$TOTAL" -eq "$PREV_TOTAL" ]; then
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

THROUGHPUT=$(python3 -c "print(f'{${N} / ${ELAPSED}:.1f}')" 2>/dev/null || echo "n/a")

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
