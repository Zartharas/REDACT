#!/bin/bash
# Task #53: validate the Kafka-shaped queue path (src/queue_consumer.py's
# _run_kafka_consumer, logstash/redact-pipeline-kafka.conf) against a
# REAL managed Kafka -- Confluent Cloud's Basic tier -- rather than
# floci's local, unauthenticated Redpanda emulation (run_floci_kafka_test.sh,
# already confirmed live, see BUGS_AND_FIXES.md's "Bug 23/24 CLOSED" entry).
# Confluent Cloud is an explicitly-allowed substitute for real AWS MSK per
# this task's own description (CLOUD_SETUP.md) -- AWS MSK has no free
# tier and bills per broker-hour from the moment a cluster exists, which
# is real, avoidable financial risk for a one-off validation test.
#
# WHAT THIS ADDS OVER THE FLOCI VERSION: real SASL_SSL authentication
# (Confluent Cloud requires it; floci's local Redpanda needs none) --
# see src/queue_consumer.py's KAFKA_SECURITY_PROTOCOL/KAFKA_SASL_* env
# vars and redact-pipeline-kafka.conf's matching security_protocol/
# sasl_mechanism/sasl_jaas_config fields, both added 2026-09-05
# specifically for this. Everything else (corpus generation, the
# docker-compose services, the poll-then-reconcile logic) reuses
# run_floci_kafka_test.sh's already-proven pattern rather than
# reinventing it -- same reasoning as that script's own header comment
# about reusing validation/load_test/run_load_test.sh's poll logic.
#
# REAL, ONGOING COST WHILE THIS CLUSTER EXISTS, stated plainly: Confluent
# Cloud's Basic tier is "first eCKU free, then $0.14/eCKU-hr" plus
# $0.05/GB data in/out and $0.08/GB-month storage (confirmed on the
# console at cluster-creation time, 2026-09-05 -- reconfirm on your own
# console since pricing can change). A short run of this script should
# land at or near $0 given the $400 default trial credit every new
# Confluent Cloud org gets, but the cluster keeps existing (and could
# keep costing something) until YOU delete it -- see the cleanup
# reminder at the end of this script, which is not run automatically.
#
# NOT run in this sandbox (no Docker daemon, and this needs real
# Confluent Cloud credentials this sandbox was never given) --
# syntax-checked only (bash -n). Needs the user's machine, same
# disclosure as every other new script in this project until it's
# actually been run.
set -euo pipefail

git pull origin main   # pick up this session's changes (this script,
                        # the SASL_SSL support in queue_consumer.py/
                        # redact-pipeline-kafka.conf/docker-compose.yml)
                        # before running anything against them.

# Real secrets (bootstrap server, API key, API secret) that only the
# user has -- deliberately NOT auto-generated the way the five
# REDACT_*_KEY values below are (those are this project's own internal
# keys; these three are Confluent Cloud's actual credentials this
# script was never given and must never invent). If .env doesn't
# already have them, prompt for them interactively instead of just
# failing -- API key and bootstrap server aren't secret-shaped (fine to
# echo back so you can check for typos), but the API secret is read
# with `read -s` (silent, no terminal echo) the same way a password
# prompt would be, and is never printed anywhere by this script,
# including in the "wire REDACT" step below.
touch .env
if ! grep -q "^CONFLUENT_BOOTSTRAP_SERVERS=" .env; then
  read -r -p "Confluent Cloud bootstrap server (e.g. pkc-921jm.us-east-2.aws.confluent.cloud:9092): " CONFLUENT_BOOTSTRAP_SERVERS_INPUT
  echo "CONFLUENT_BOOTSTRAP_SERVERS=${CONFLUENT_BOOTSTRAP_SERVERS_INPUT}" >> .env
fi
if ! grep -q "^CONFLUENT_API_KEY=" .env; then
  read -r -p "Confluent Cloud API key: " CONFLUENT_API_KEY_INPUT
  echo "CONFLUENT_API_KEY=${CONFLUENT_API_KEY_INPUT}" >> .env
fi
if ! grep -q "^CONFLUENT_API_SECRET=" .env; then
  read -r -s -p "Confluent Cloud API secret (input hidden): " CONFLUENT_API_SECRET_INPUT
  echo   # read -s eats the newline the user's Enter key would normally
         # produce -- print one explicitly so the next echo below
         # doesn't run into the prompt text.
  echo "CONFLUENT_API_SECRET=${CONFLUENT_API_SECRET_INPUT}" >> .env
fi
echo "Confluent Cloud credentials confirmed in .env (never echoed above beyond what you typed)."
echo "Reminder: .env is already .gitignore'd in this project -- never commit it or paste its contents into chat."

# This project's own five internal keys -- same bootstrap this project's
# other scripts already do, only adding what's not already present.
for key in REDACT_PSEUDO_KEY REDACT_AUDIT_KEY REDACT_SERVICE_API_KEY REDACT_FINGERPRINT_KEY REDACT_TOKEN_KEY; do
  if ! grep -q "^${key}=" .env; then
    echo "${key}=$(openssl rand -hex 32)" >> .env
    echo "Added ${key} to .env"
  fi
done
set -a
source .env
set +a

echo "=== Part 0: tearing down any previous stack for a clean slate (docker compose down -v) ==="
docker compose --profile kafka-queued down -v || true

echo ""
echo "=== Part 1: wire REDACT's Kafka-shaped path to the real Confluent Cloud cluster ==="
export KAFKA_BROKERS="$CONFLUENT_BOOTSTRAP_SERVERS"
export KAFKA_TOPIC="${CONFLUENT_TOPIC:-redact-raw-events}"
export KAFKA_SECURITY_PROTOCOL="SASL_SSL"
export KAFKA_SASL_MECHANISM="PLAIN"
export KAFKA_SASL_USERNAME="$CONFLUENT_API_KEY"
export KAFKA_SASL_PASSWORD="$CONFLUENT_API_SECRET"
export KAFKA_SASL_JAAS_CONFIG="org.apache.kafka.common.security.plain.PlainLoginModule required username=\"${CONFLUENT_API_KEY}\" password=\"${CONFLUENT_API_SECRET}\";"
echo "Broker: $KAFKA_BROKERS"
echo "Topic:  $KAFKA_TOPIC"
echo "(API key/secret exported to this shell's environment only, never echoed)"

echo ""
echo "=== Part 2: generate a small test corpus ==="
# Deliberately smaller than run_floci_kafka_test.sh's 20,000 lines --
# this run has a REAL, if small, per-GB data-transfer and storage cost
# on Confluent Cloud (see header comment), and the goal here is
# confirming the architecture works against real infrastructure, not
# re-measuring throughput floci's local test already covered. 2,000
# lines is enough to exercise partitioning, consumer-group rebalancing,
# and the doc_id_base idempotency fix (Bug 23) meaningfully, at a small
# fraction of the data volume.
N=2000
CORPUS_PATH="data/confluent_kafka_test_corpus_${N}.jsonl"
python3 src/generate_logs.py --n "$N" --out "$CORPUS_PATH" --dirty-ratio 0.3
python3 src/export_raw_logs.py --input "$CORPUS_PATH" --output-dir data/raw

echo ""
echo "=== Part 3: bring up REDACT and the Kafka-shaped consumer/producer ==="
docker compose up -d --build redact-service redact-lb \
    opensearch-node1 opensearch-node2 opensearch-node3
docker compose --profile kafka-queued up -d --build logstash-kafka queue-consumer-kafka

echo ""
echo "Waiting for the queue to drain (polling every 10s, up to 10 minutes)..."
# Same 3-consecutive-stable-polls logic as run_floci_kafka_test.sh's own
# Bug 24 fix -- reused rather than reinvented, see that script's comment
# for the full history of why a naive single-shot check was wrong.
PREV_TOTAL=-1
STABLE_COUNT=0
for i in $(seq 1 60); do
    ANON_COUNT=$(curl -s "http://localhost:9200/security-logs-anonymized-*/_count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo 0)
    QUAR_COUNT=$(curl -s "http://localhost:9200/security-logs-quarantine-*/_count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo 0)
    TOTAL=$((ANON_COUNT + QUAR_COUNT))
    echo "  processed so far: $TOTAL / $N (anonymized=$ANON_COUNT, quarantine=$QUAR_COUNT)"
    if [ "$TOTAL" -eq "$PREV_TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
        STABLE_COUNT=$((STABLE_COUNT + 1))
    else
        STABLE_COUNT=0
    fi
    PREV_TOTAL=$TOTAL
    if [ "$STABLE_COUNT" -ge 3 ]; then
        echo "  total stable for 3 consecutive polls (30s), assuming ingestion finished."
        break
    fi
    sleep 10
done

echo ""
echo "=== Part 4: reconciliation ==="
python3 validation/load_test/reconcile.py "http://localhost:9200"

echo ""
echo "=== Part 5: check Logstash and consumer logs for real auth/connection errors ==="
echo "(a SASL_SSL misconfiguration typically shows up here, not as a bash error)"
docker compose logs logstash-kafka --tail 30
docker compose logs queue-consumer-kafka --tail 30

echo ""
echo "=== IMPORTANT: cleanup, both local and on Confluent Cloud ==="
echo "Local:"
echo "  docker compose --profile kafka-queued down"
echo "  rm -f $CORPUS_PATH"
echo ""
echo "On Confluent Cloud (console.confluent.cloud) -- do this even if the"
echo "run above failed, since the cluster keeps costing whatever it costs"
echo "regardless of whether this test succeeded:"
echo "  1. Delete the topic ($KAFKA_TOPIC), or"
echo "  2. Delete the whole cluster (Redact_1) if you're done validating,"
echo "     or at minimum confirm a usage/budget alert is active if you're"
echo "     keeping it around for further testing."
