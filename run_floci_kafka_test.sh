#!/bin/bash
# Tests REDACT's Kafka-shaped queue path (src/queue_consumer.py's
# _run_kafka_consumer, logstash/redact-pipeline-kafka.conf) against a
# real Redpanda broker provisioned via floci's MSK emulation. See
# docker-compose.yml's floci comment for what floci is and its honest
# limits, and BUGS_AND_FIXES.md's "Engineering upgrade 6" for the
# analogous ELB v2 test's own honest-uncertainty framing -- this one is
# on firmer ground: floci's own service table lists MSK as "Real Docker"
# (a genuine Redpanda broker), not "In-process" the way ELB v2 is, so
# there is less ambiguity here about whether real traffic actually flows.
#
# NOT run in this sandbox (no Docker daemon here) -- syntax-checked only
# (bash -n, every `aws kafka` command's parameters checked against real,
# documented AWS CLI syntax for the MSK API, not invented). Needs the
# same live confirmation pass every other new piece of infrastructure in
# this project has gone through.
set -euo pipefail

# Real gap found and fixed 2026-08-11 (Bug 22, BUGS_AND_FIXES.md), same
# missing step run_1m_load_test.sh/run_5m_load_test.sh already have:
# redact-service's docker-compose.yml block requires 5 keys
# (REDACT_PSEUDO_KEY/REDACT_AUDIT_KEY/REDACT_SERVICE_API_KEY/
# REDACT_FINGERPRINT_KEY/REDACT_TOKEN_KEY) via `${VAR:?...}` -- without
# them already in .env, `docker compose up` for redact-service (needed
# later in this script) fails with its own required-variable error. This
# script never bootstrapped them; fixed here, only adding keys that
# aren't already present, never touching existing values.
touch .env
for key in REDACT_PSEUDO_KEY REDACT_AUDIT_KEY REDACT_SERVICE_API_KEY REDACT_FINGERPRINT_KEY REDACT_TOKEN_KEY; do
  if ! grep -q "^${key}=" .env; then
    echo "${key}=$(openssl rand -hex 32)" >> .env
    echo "Added ${key} to .env"
  fi
done
set -a
source .env
set +a

# Real bug, found 2026-08-11 from a live rerun (Bug 24, BUGS_AND_FIXES.md):
# a second consecutive run reported 77,812 documents for a 20,000-line
# corpus -- HIGHER than the FIRST run's already-wrong 60,000, which ruled
# out the Bug 23 doc_id_base fix as the cause (a fix that makes writes
# MORE idempotent cannot legitimately produce a HIGHER count than before
# it existed). Root cause: this script never did a clean-slate teardown
# the way validation/load_test/run_load_test.sh already does (see that
# script's own "Tearing down any previous stack (clean slate)" step) --
# `docker compose down` (without `-v`) does NOT delete OpenSearch's named
# volumes, and security-logs-anonymized-* indices are DATE-suffixed
# (day granularity, redact-pipeline-kafka.conf / queue_consumer.py's own
# date_suffix), so a second run on the same calendar day writes into the
# EXACT SAME index as the first run's leftover 60,000 documents --
# 60,000 (stale, from before the Bug 23 fix existed) + this run's own
# contribution landed at 77,812 total, not a fresh, comparable count.
# Fixed by tearing down (INCLUDING volumes) before this script does
# anything else, mirroring run_load_test.sh's own proven pattern exactly
# rather than reinventing a different, weaker approach.
echo "=== Part 0: tearing down any previous stack for a clean slate (docker compose down -v) ==="
docker compose --profile kafka-queued --profile cloud-sim down -v || true

echo "=== Part 1: bring up floci ==="
docker compose --profile cloud-sim up -d floci

echo "Waiting for floci to report healthy on :4566..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:4566/_localstack/health >/dev/null 2>&1 \
       || curl -sf http://localhost:4566/health >/dev/null 2>&1; then
        echo "floci responding."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "floci did not become healthy in time. Check: docker compose logs floci"
        exit 1
    fi
    sleep 2
done

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL=http://localhost:4566

echo ""
echo "=== Part 2: provision an MSK-shaped cluster (real Redpanda broker under floci) ==="
CLUSTER_NAME="redact-msk-test"
CLUSTER_ARN=$(aws kafka create-cluster \
    --cluster-name "$CLUSTER_NAME" \
    --kafka-version "2.8.1" \
    --number-of-broker-nodes 1 \
    --broker-node-group-info '{"InstanceType":"kafka.m5.large","ClientSubnets":["subnet-placeholder"],"BrokerAZDistribution":"DEFAULT"}' \
    --query 'ClusterArn' --output text)
echo "Cluster ARN: $CLUSTER_ARN"

echo "Waiting for cluster to become ACTIVE..."
for i in $(seq 1 30); do
    STATE=$(aws kafka describe-cluster --cluster-arn "$CLUSTER_ARN" --query 'ClusterInfo.State' --output text)
    echo "  state: $STATE"
    if [ "$STATE" = "ACTIVE" ]; then break; fi
    if [ "$i" -eq 30 ]; then
        echo "Cluster did not become ACTIVE in time."
        exit 1
    fi
    sleep 3
done

BROKER_LIST=$(aws kafka get-bootstrap-brokers --cluster-arn "$CLUSTER_ARN" \
    --query 'BootstrapBrokerString' --output text)
echo "Broker list: $BROKER_LIST"

if [ -z "$BROKER_LIST" ] || [ "$BROKER_LIST" = "None" ]; then
    echo "ERROR: no broker address returned. This is real information worth reporting"
    echo "back either way -- it may mean floci's MSK emulation needs a different API"
    echo "call sequence than assumed here, not necessarily that MSK emulation itself"
    echo "doesn't work."
    exit 1
fi

echo ""
echo "=== Part 3: wire REDACT's Kafka-shaped path to this broker and run a real corpus ==="
export KAFKA_BROKERS="$BROKER_LIST"
export KAFKA_TOPIC="redact-raw-events"

echo "Generating a test corpus (same flags as run_replica_and_queue_test.sh's Part B)..."
N=20000
CORPUS_PATH="data/kafka_queue_test_corpus_${N}.jsonl"
python3 src/generate_logs.py --n "$N" --out "$CORPUS_PATH" --dirty-ratio 0.3
python3 src/export_raw_logs.py --input "$CORPUS_PATH" --output-dir data/raw

docker compose up -d --build redact-service redact-lb \
    opensearch-node1 opensearch-node2 opensearch-node3
docker compose --profile kafka-queued up -d --build logstash-kafka queue-consumer-kafka

echo "Waiting for the queue to drain (polling every 10s, up to 10 minutes)..."
# Real bug, found and fixed alongside the clean-slate teardown above
# (Bug 24, BUGS_AND_FIXES.md): this loop used to break the instant
# TOTAL >= N on a SINGLE poll, a single-shot check, not real stability.
# validation/load_test/run_load_test.sh already solved this exact
# problem correctly (3 consecutive unchanged polls, with TOTAL > 0
# required so an all-zero reading never counts as "stable") -- reused
# that proven logic here instead of the weaker one-shot check this
# script originally had, which (compounded by the stale-data bug above)
# meant a live run could report "done" before this run's own processing
# had actually finished, or without ever detecting a stalled pipeline
# separately from a genuinely-slow-but-healthy one.
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
# reconcile.py takes a single positional opensearch_host argument and
# computes the expected total from data/raw/*.log's own line counts --
# no --expected/--index flags exist (checked this script's own commands
# against reconcile.py's real, current CLI shape rather than inventing
# flags that would silently fail with an unrecognized-argument error).
python3 validation/load_test/reconcile.py "http://localhost:9200"

echo ""
echo "=== Cleanup reminder (not run automatically) ==="
echo "aws kafka delete-cluster --cluster-arn $CLUSTER_ARN"
echo "docker compose --profile kafka-queued down"
echo "docker compose --profile cloud-sim down"
echo "rm -f $CORPUS_PATH"
