#!/usr/bin/env bash
# Follow-up to Bug 23's confirmed Sentinel-level failover
# (BUGS_AND_FIXES.md): that test killed redis-master with the queue path
# idle -- no traffic in flight -- and confirmed Sentinel itself promotes
# a replica correctly. What it did NOT exercise is the two CLIENT-side
# pieces Bug 22 added on top of that: whether src/queue_consumer.py's
# Sentinel-aware reconnect logic actually resumes cleanly while it's
# mid-BLPOP-loop processing real events, and whether logstash-queued's
# disclosed non-Sentinel-aware limitation (docker-compose.yml's own
# comment on that service, and redact-pipeline-queued.conf's `redis`
# output block) reproduces exactly as predicted rather than failing some
# different, undocumented way.
#
# WHAT TO EXPECT, READ THIS BEFORE RUNNING: unlike
# run_replica_and_queue_test.sh's Part B, this test is NOT expected to
# reconcile at the full corpus size. logstash-queued has no Sentinel
# awareness -- once redis-master is killed, it will keep trying to write
# to that now-dead IP and FAIL to enqueue any new raw events for the rest
# of the run (see docker-compose.yml's own comment on this service for
# why). That means ingestion of NEW lines effectively stops at the
# moment of the kill; only events already sitting in the Redis list
# before that moment can still be drained. A reconciliation total that
# stalls below N once the kill happens is the PREDICTED, DISCLOSED
# outcome this test exists to confirm, not evidence of a new bug --
# see the "WHAT A GENUINE PROBLEM WOULD LOOK LIKE" note near the bottom
# for what actually would be a red flag.
#
# Needs Docker Desktop running. Run from the repo root:
#   chmod +x run_redis_failover_test.sh
#   ./run_redis_failover_test.sh
set -euo pipefail

touch .env
for key in REDACT_PSEUDO_KEY REDACT_AUDIT_KEY REDACT_SERVICE_API_KEY REDACT_FINGERPRINT_KEY REDACT_TOKEN_KEY; do
  if ! grep -q "^${key}=" .env; then
    echo "${key}=$(openssl rand -hex 32)" >> .env
    echo "Added ${key} to .env"
  fi
done

# Larger than run_replica_and_queue_test.sh's 20,000 -- deliberately, so
# there's a real multi-minute window of in-flight processing to kill
# redis-master into, rather than a run so fast the kill might land
# before or after all meaningful activity by accident.
N=60000
CORPUS_PATH="data/redis_failover_test_corpus_${N}.jsonl"

echo "=================================================================="
echo "Redis Sentinel failover under live traffic (queue-decoupled path)"
echo "=================================================================="
python3 src/generate_logs.py --n "$N" --out "$CORPUS_PATH" --dirty-ratio 0.3
python3 src/export_raw_logs.py --input "$CORPUS_PATH" --output-dir data/raw

docker compose down -v --remove-orphans
docker compose --profile queued up --build -d --scale queue-consumer=3 \
  opensearch-node1 opensearch-node2 opensearch-node3 \
  redis-master redis-replica-1 redis-replica-2 \
  redis-sentinel-1 redis-sentinel-2 redis-sentinel-3 \
  redact-service redact-lb logstash-queued queue-consumer

echo "--- Waiting for services to report healthy ---"
for i in $(seq 1 60); do
  if docker compose ps opensearch-node1 | grep -q "healthy" && \
     docker compose ps redis-master | grep -q "healthy" && \
     docker compose ps redact-service | grep -q "healthy"; then
    echo "Core services healthy after approx $((i * 5))s."
    break
  fi
  sleep 5
done

# Confirm Sentinel sees a healthy master with both replicas attached
# BEFORE the kill -- if this doesn't show num-slaves:2, don't proceed;
# something about startup is wrong and the failover test below would be
# meaningless.
echo "--- Sentinel status before the kill ---"
docker compose exec -T redis-sentinel-1 redis-cli -p 26379 sentinel master redact-master

echo "--- Waiting for ingestion to be genuinely in flight before killing redis-master ---"
target=$((N / 5))  # wait for at least 20% processed -- enough to be
                    # confident this is mid-run, not startup noise
for i in $(seq 1 60); do
  sleep 5
  anon=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
  quar=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
  total=$((anon + quar))
  echo "  poll $i: total=${total} (waiting for >= ${target})"
  if [ "$total" -ge "$target" ]; then
    echo "  in-flight threshold reached -- killing redis-master now."
    break
  fi
done

pre_kill_anon=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
pre_kill_quar=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
echo "--- Snapshot immediately before kill: anonymized=${pre_kill_anon} quarantine=${pre_kill_quar} ---"

echo "--- Killing redact-redis-master ---"
docker kill redact-redis-master

echo "--- Confirming Sentinel promotes a replica (should complete in well under 10s) ---"
sleep 12
docker compose exec -T redis-sentinel-1 redis-cli -p 26379 sentinel get-master-addr-by-name redact-master

echo "--- Polling reconciliation for 3 minutes post-kill to see where it settles ---"
for i in $(seq 1 18); do
  sleep 10
  anon=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
  quar=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
  total=$((anon + quar))
  echo "  poll $i (post-kill): anonymized=${anon} quarantine=${quar} total=${total} (corpus size ${N})"
done

echo
echo "=================================================================="
echo "RESULTS"
echo "=================================================================="
final_anon=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
final_quar=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_search?size=0&track_total_hits=true" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hits',{}).get('total',{}).get('value',0))" 2>/dev/null || echo 0)
final_total=$((final_anon + final_quar))
echo "Pre-kill total:  $((pre_kill_anon + pre_kill_quar))"
echo "Final total:     ${final_total} / ${N}"
echo
echo "--- Remaining queue depth (events still sitting in Redis, un-consumed) ---"
# Ask Sentinel which node is CURRENTLY master rather than guessing --
# Sentinel picks whichever replica it promotes based on replication
# state, not deterministically replica-1 vs replica-2, so this run's
# promoted node may differ from Bug 23's own confirmed test.
current_master_ip=$(docker compose exec -T redis-sentinel-1 redis-cli -p 26379 sentinel get-master-addr-by-name redact-master | head -1 | tr -d '\r')
echo "Current master per Sentinel: ${current_master_ip}"
docker compose exec -T redis-sentinel-1 redis-cli -h "$current_master_ip" LLEN "${REDACT_QUEUE_KEY:-redact:raw-events}" 2>/dev/null || \
  echo "(could not reach ${current_master_ip} from redis-sentinel-1 -- check docker compose logs redis-sentinel-1 for the current failover state)"

echo
echo "--- queue-consumer logs (look for the connection-error/retry messages," \
     "then a resumed BLPOP loop after Sentinel's promotion) ---"
docker compose logs queue-consumer --tail 40

echo
echo "--- logstash-queued logs (look for repeated failures writing to the" \
     "dead redis-master IP -- this is the PREDICTED, DISCLOSED outcome," \
     "not a new bug) ---"
docker compose logs logstash-queued --tail 40

echo
echo "=================================================================="
echo "WHAT A GENUINE PROBLEM WOULD LOOK LIKE (vs. the predicted outcome above)"
echo "=================================================================="
echo "EXPECTED (matches disclosed limitations, not a new bug):"
echo "  - Final total stalls somewhere below ${N} -- new events stopped"
echo "    enqueuing once redis-master died, since logstash-queued has no"
echo "    Sentinel awareness."
echo "  - queue-consumer's logs show one or more 'Redis connection error"
echo "    (retrying)' lines right after the kill, then normal polling"
echo "    resumes."
echo "  - Queue depth ends at or near 0 -- whatever WAS already queued"
echo "    before the kill got fully drained by the surviving consumers,"
echo "    even though no new events could be added."
echo
echo "WOULD BE A NEW, GENUINE BUG (not predicted by anything disclosed"
echo "so far -- worth a fresh BUGS_AND_FIXES.md entry if you see this):"
echo "  - queue-consumer's logs show it stuck in a connection-error loop"
echo "    indefinitely, never resuming -- would mean the Sentinel"
echo "    reconnect path doesn't actually work under live BLPOP traffic,"
echo "    only when idle (as Bug 23's test alone would have missed)."
echo "  - Queue depth stays significantly nonzero and never drains, even"
echo "    minutes after Sentinel's promotion completed -- would mean"
echo "    events already queued before the kill got stranded, not just"
echo "    new events being blocked."
echo
echo "--- Leaving the stack running for manual inspection. ---"
echo "--- Run 'docker compose down -v' when done. ---"
