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

echo
echo "--- Running the 1,000,000-line load test ---"
./validation/load_test/run_load_test.sh 1000000
