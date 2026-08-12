#!/usr/bin/env bash
# Task #12: full 1,000,000-line Docker Compose stack rerun, now exercising
# everything added since the last 1M-line run (2026-08-08, BUGS_AND_FIXES.md
# Bug 15): the X-Redact-Api-Key auth header, the non-root Dockerfile,
# Prometheus metrics, and -- new this time -- field-gated NER as the
# default detection path plus log_type being forwarded through
# logstash/redact-pipeline.conf's http filter for the first time ever
# at this scale.
#
# Uses the existing, already-verified validation/load_test/run_load_test.sh
# harness unmodified (corpus generation, docker stats capture, the
# track_total_hits=true reconciliation poll loop, summary file) -- no
# reason to rewrite something that already works.
#
# Run from the repo root:
#   chmod +x run_1m_load_test.sh
#   ./run_1m_load_test.sh
#
# Needs Docker Desktop running, several GB of free disk space, and takes
# a while -- the last 1,000,000-line run took ~75 minutes end to end.
set -euo pipefail

git push origin main   # publish everything from this session first

# docker-compose.yml requires five keys in .env (REDACT_PSEUDO_KEY,
# REDACT_AUDIT_KEY, REDACT_SERVICE_API_KEY, REDACT_FINGERPRINT_KEY,
# REDACT_TOKEN_KEY) and fails immediately if any are missing. Your .env
# currently only has the first two -- add the other three if not already
# present, without touching any keys that already exist.
touch .env
for key in REDACT_SERVICE_API_KEY REDACT_FINGERPRINT_KEY REDACT_TOKEN_KEY; do
  if ! grep -q "^${key}=" .env; then
    echo "${key}=$(openssl rand -hex 32)" >> .env
    echo "Added ${key} to .env"
  fi
done

echo "--- .env now has these keys set (values redacted) ---"
sed 's/=.*/=<redacted>/' .env

# Fast pre-flight, added 2026-08-10 after the first 1M-line attempt hit a
# real Logstash config syntax error (a comma in a hash literal --
# invalid Logstash DSL, valid-looking Ruby/JSON -- that crashed the whole
# pipeline at startup, see logstash/redact-pipeline.conf's own comment on
# the fix) that only surfaced after a ~90s image build and a full stack
# startup, reported as a false "success" by the load test's own stability
# poll (0/0/0/0 looked "stable" to it -- also since fixed, see
# run_load_test.sh). Logstash's own --config.test_and_exit flag parses
# the pipeline config without starting it or needing OpenSearch/
# redact-service reachable at all -- catches exactly this class of
# mistake in seconds instead of minutes, and should be run after any
# future edit to redact-pipeline.conf, not just before a full load test.
echo
echo "--- Pre-flight: validating Logstash pipeline config syntax ---"
docker compose build logstash

# Real bug, found 2026-08-11 via a live run_5m_load_test.sh run (same
# pre-flight step, copied here verbatim): `docker compose run --rm
# logstash ...` only starts logstash's DIRECT depends_on entries
# (opensearch-node1, redact-service, redact-lb) -- not opensearch-node2/3,
# since nothing in that graph names them. Harmless when OpenSearch was a
# single-node service; broken since ROADMAP item 12 made it a real 3-node
# cluster, because opensearch-node1's own healthcheck requires all 3
# nodes to have joined ("number_of_nodes":3) and can never pass alone.
# This script hadn't been rerun since that change, so the bug was latent
# here too even though it was only actually triggered via the 5M-line
# script first. Fixed the same way: bring up the full dependency set
# (all 3 OpenSearch nodes, redact-service, redact-lb) explicitly before
# `docker compose run`, matching run_floci_kafka_test.sh's own proven,
# explicitly-scoped invocation.
echo "--- Bringing up Logstash's full dependency set first (all 3 OpenSearch"
echo "    nodes, not just node1 -- see comment above for why) ---"
docker compose up -d --build redact-service redact-lb \
  opensearch-node1 opensearch-node2 opensearch-node3

docker compose run --rm logstash \
  bin/logstash --config.test_and_exit -f /usr/share/logstash/pipeline/redact-pipeline.conf
echo "Logstash config syntax OK."

echo
echo "--- Running the 1,000,000-line load test ---"
./validation/load_test/run_load_test.sh 1000000
