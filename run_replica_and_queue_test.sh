#!/usr/bin/env bash
# ROADMAP item 12, closing the two pieces that were only ever "scoped and
# documented, not implemented" as of 2026-08-09: multi-replica
# redact-service behind a real load balancer, and a queue-decoupled
# ingestion path. This script runs BOTH live, back to back, against a
# moderate corpus (20,000 lines -- big enough to give a real distribution
# signal across 3 replicas/consumers, small enough that this isn't
# another hour-long run like the 1,000,000-line test).
#
# Part A tests: does redact-lb's nginx proxy actually distribute requests
# across 3 redact-service replicas (not just "does the pipeline still
# reconcile with one replica renamed"), via the new gunicorn
# --access-logfile - flag added alongside this.
#
# Part B tests: does the queue-decoupled path (logstash-queued -> Redis
# list -> 3x queue-consumer -> redact-lb -> OpenSearch) reconcile exactly,
# same as the synchronous path always has.
#
# FIRST LIVE RUN OF THIS SCRIPT (2026-08-10) OOM-KILLED OPENSEARCH (exit
# 137) during Part A's --scale redact-service=3 -- root-caused to gunicorn
# defaulting to $(nproc) workers PER REPLICA, so 3 replicas multiplied
# total worker count (and spaCy/Presidio model memory, one copy per
# worker) by 3 x nproc, not just 3x a single replica's own footprint.
# Fixed: GUNICORN_WORKERS is now a configurable env var (Dockerfile CMD,
# default 2 in docker-compose.yml's redact-service block), so 3 replicas
# now means 6 total workers regardless of host core count. That run was
# worked around live with `--scale redact-service=2` instead (succeeded,
# all containers healthy) rather than this fix, since the fix hadn't been
# written yet -- this script still asks for 3 below now that the fix is
# in place; if your host is memory-constrained, override with
# `GUNICORN_WORKERS=1 ./run_replica_and_queue_test.sh` or edit the
# --scale value down to 2, same workaround as before.
#
# Needs Docker Desktop running. Run from the repo root:
#   chmod +x run_replica_and_queue_test.sh
#   ./run_replica_and_queue_test.sh
set -euo pipefail

git push origin main

touch .env
for key in REDACT_PSEUDO_KEY REDACT_AUDIT_KEY REDACT_SERVICE_API_KEY REDACT_FINGERPRINT_KEY REDACT_TOKEN_KEY; do
  if ! grep -q "^${key}=" .env; then
    echo "${key}=$(openssl rand -hex 32)" >> .env
    echo "Added ${key} to .env"
  fi
done

N=20000
CORPUS_PATH="data/replica_queue_test_corpus_${N}.jsonl"

wait_for_reconciliation() {
  # Same track_total_hits=true methodology as validation/load_test/reconcile.py
  # -- polls until the anonymized+quarantine total stops changing across 3
  # consecutive checks AND is greater than zero (see BUGS_AND_FIXES.md Bug 16
  # for why the ">0" part matters: an all-zero "stable" reading means the
  # pipeline never started, not that it finished).
  local expected="$1"
  local prev=-1 stable=0
  for i in $(seq 1 80); do
    sleep 15
    local anon quar total
    anon=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
    quar=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
    total=$((anon + quar))
    echo "  poll $i: anonymized=${anon} quarantine=${quar} total=${total} (expecting ${expected})"
    if [ "$total" -eq "$prev" ] && [ "$total" -gt 0 ]; then
      stable=$((stable + 1))
    else
      stable=0
    fi
    prev=$total
    if [ "$stable" -ge 3 ]; then
      echo "  stable for 3 consecutive polls."
      return 0
    fi
  done
  echo "  WARNING: did not stabilize within the poll budget -- check docker compose logs before trusting anything below." >&2
  return 1
}

echo "=================================================================="
echo "PART A: multi-replica redact-service (x3) behind redact-lb"
echo "=================================================================="
echo "GUNICORN_WORKERS=${GUNICORN_WORKERS:-2 (docker-compose.yml default)} per replica -- 3 replicas = ~6 total workers, not 3x nproc (see this script's header comment re: the OOM this fixes)."
python3 src/generate_logs.py --n "$N" --out "$CORPUS_PATH" --dirty-ratio 0.3
python3 src/export_raw_logs.py --input "$CORPUS_PATH" --output-dir data/raw

docker compose down -v
docker compose up --build -d --scale redact-service=3

echo "--- Waiting for all services to report healthy ---"
for i in $(seq 1 60); do
  healthy_count=$(docker compose ps redact-service | grep -c "healthy" || true)
  if [ "$healthy_count" -eq 3 ] && docker compose ps opensearch | grep -q "healthy"; then
    echo "All 3 redact-service replicas + opensearch healthy after approx $((i * 5))s."
    break
  fi
  sleep 5
done

echo "--- Polling reconciliation ---"
wait_for_reconciliation "$N" || true

echo "--- Final reconciliation ---"
python3 validation/load_test/reconcile.py

echo
echo "--- Per-replica request distribution (gunicorn access log lines per container) ---"
echo "If redact-lb is actually load-balancing, all 3 lines below should show"
echo "a nonzero, roughly comparable count -- one replica at ~3x N/3 requests"
echo "and two at 0 would mean the proxy isn't distributing, despite everything"
echo "above still reconciling correctly (a single working replica is enough"
echo "for reconciliation to pass, which is exactly why this check is separate)."
docker compose logs redact-service 2>/dev/null | grep -oE '^[a-zA-Z0-9._-]+-redact-service-[0-9]+' | sort | uniq -c

echo
echo "=================================================================="
echo "PART B: queue-decoupled ingestion path (Redis list, 3x queue-consumer)"
echo "=================================================================="
echo "--- Tearing down Part A's stack first (both pipelines tail the same"
echo "    data/raw files independently -- running both at once would"
echo "    double-process every line) ---"
docker compose down -v

# Fresh corpus so Part B's reconciliation number isn't ambiguous with
# Part A's (different content, same size).
python3 src/generate_logs.py --n "$N" --out "$CORPUS_PATH" --dirty-ratio 0.3
python3 src/export_raw_logs.py --input "$CORPUS_PATH" --output-dir data/raw

docker compose --profile queued up --build -d --scale queue-consumer=3
# Note: no --scale on redact-service here -- Part B is specifically
# testing the queue's own decoupling/distribution behavior (3 CONSUMERS
# pulling from one Redis list), independent of Part A's redact-service
# replica count. Combining both in one run is a reasonable follow-up,
# not done here to keep each part's result attributable to one variable
# at a time.

echo "--- Waiting for services to report healthy ---"
for i in $(seq 1 60); do
  if docker compose ps opensearch | grep -q "healthy" && \
     docker compose ps redis | grep -q "healthy" && \
     docker compose ps redact-service | grep -q "healthy"; then
    echo "Core services healthy after approx $((i * 5))s."
    break
  fi
  sleep 5
done

echo "--- Polling reconciliation ---"
wait_for_reconciliation "$N" || true

echo "--- Final reconciliation ---"
python3 validation/load_test/reconcile.py

echo
echo "--- Queue depth check (should be 0 or near-0 if consumers kept up) ---"
docker compose exec -T redis redis-cli LLEN "${REDACT_QUEUE_KEY:-redact:raw-events}" || true

echo
echo "--- Leaving the stack running for manual inspection. ---"
echo "--- Run 'docker compose down -v' when done. ---"
