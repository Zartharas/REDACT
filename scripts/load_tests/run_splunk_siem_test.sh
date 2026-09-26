#!/bin/bash
# Ask 2's SIEM half (manuscript revision engineering support): a real
# (not simulated) SIEM ingestion path test against a real, locally-run
# Splunk instance -- chosen over a cloud SIEM (e.g. Microsoft Sentinel)
# specifically to avoid real per-GB ingestion cost; Splunk's official
# Docker image runs as a time-limited Enterprise trial that reverts to
# the permanently-free Splunk Free tier (500MB/day) automatically, no
# signup, no card, no cost at this test's scale.
#
# WHAT THIS TESTS, narrowly: (1) real HEC (HTTP Event Collector) ingestion
# of REDACT's real anonymized output; (2) a DLP-style check, run against
# the LIVE running Splunk instance via its own search REST API (not
# against local files), confirming no raw ground-truth PII value from the
# source corpus is present in what actually got indexed and is now
# searchable. It does NOT test Splunk's own detection rules, dashboards,
# alerting, or any volume beyond a few hundred KB -- none of that is
# claimed.
#
# REUSES Ask 2a's real finding directly: validation/cloud_loglake/
# prepare_loglake_upload.py's "strip 'original' via an explicit allow-list"
# fix is imported by validation/siem_splunk/ingest_to_splunk.py, not
# reimplemented -- the same real risk (uploading/ingesting raw PII sitting
# next to the anonymized text in pipeline.py's own output schema) applies
# identically to a SIEM ingestion path as to object storage.
#
# A REAL GOTCHA CAUGHT BEFORE BUILDING AROUND IT: the official image's
# SPLUNK_HEC_TOKEN environment variable is documented, per a real
# long-standing upstream issue (splunk/docker-splunk#40), as only applying
# to forwarder/indexer roles -- NOT the standalone role this single-
# container test uses. Rather than build around an env var that may
# silently do nothing here, this script enables HEC and creates the token
# itself via Splunk's management REST API (port 8089) after the container
# is confirmed up, which works regardless of role.
#
# COST: $0 (Splunk Free tier, no signup). Requires Docker + docker compose
# locally -- not run in this sandbox (no Docker daemon here); every
# `curl`/REST call below checked against Splunk's current documented HEC
# and search-export API surface, genuinely unverified until run live, same
# disclosure as every other new script in this project before its first
# real run.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found. Install Docker Desktop (or Docker Engine) first." >&2
  exit 1
fi

ADMIN_PASSWORD="Redact-Test-Pw1"   # matches docker-compose-splunk.yml's SPLUNK_PASSWORD
# Host port 8091, not Splunk's default 8089 -- see docker-compose-splunk.yml's
# port mapping comment for why (an unrelated already-running Splunk
# container on the test machine had claimed 8000/8089).
BASE_URL="https://localhost:8091"
HEC_URL="https://localhost:8088/services/collector"
INDEX="main"
HEC_TOKEN=$(python3 -c "import uuid; print(uuid.uuid4())")

ANONYMIZED_IN="output/anonymized.jsonl"
if [ ! -f "$ANONYMIZED_IN" ]; then
  echo "ERROR: $ANONYMIZED_IN not found. Run this first (needs spaCy/Presidio locally):" >&2
  echo "  python3 src/pipeline.py --in data/synthetic_logs.jsonl \\" >&2
  echo "      --out output/anonymized.jsonl --audit-out output/audit_log.jsonl" >&2
  exit 1
fi

echo "=== Part 0: start Splunk (standalone, single container) ==="
docker compose -f docker-compose-splunk.yml up -d

echo ""
echo "=== Part 1: wait for the management API to come up (can take 1-3 minutes on first boot) ==="
for attempt in $(seq 1 40); do
  if curl -sk -m 5 "$BASE_URL/services/server/info" -u "admin:$ADMIN_PASSWORD" >/dev/null 2>&1; then
    echo "Management API is up."
    break
  fi
  echo "  attempt $attempt: not up yet, waiting 15s..."
  sleep 15
  if [ "$attempt" -eq 40 ]; then
    echo "ERROR: Splunk did not come up after 10 minutes. Check: docker compose -f docker-compose-splunk.yml logs splunk" >&2
    exit 1
  fi
done

echo ""
echo "=== Part 2: enable HEC and create a token (REST API, not the disputed env var) ==="
# Enables the global HEC service (idempotent -- safe to call even if
# already enabled). enableSSL=0 keeps this test simple (localhost-only,
# throwaway instance); a real deployment would keep HEC's own TLS on.
curl -sk -u "admin:$ADMIN_PASSWORD" "$BASE_URL/services/data/inputs/http/http" \
    -d disabled=0 -d enableSSL=0 --output /dev/null
echo "HEC service enabled."

curl -sk -u "admin:$ADMIN_PASSWORD" "$BASE_URL/services/data/inputs/http" \
    -d name=redact_hec -d index="$INDEX" -d token="$HEC_TOKEN" --output /dev/null
echo "HEC token created."

# HEC config changes on a standalone instance can need a moment to take
# effect on port 8088 even though the REST call above already returned.
sleep 10

echo ""
echo "=== Part 3: real HEC ingestion of REDACT's real anonymized output ==="
python3 validation/siem_splunk/ingest_to_splunk.py \
    --in "$ANONYMIZED_IN" --hec-url "$HEC_URL" --hec-token "$HEC_TOKEN" --index "$INDEX"

echo ""
echo "Waiting 20s for indexing to catch up before verifying (HEC-ingested events aren't"
echo "always immediately searchable)..."
sleep 20

echo ""
echo "=== Part 4: verify against the LIVE running instance -- count + DLP-style leak check ==="
set +e
python3 validation/siem_splunk/verify_splunk_ingestion.py \
    --base-url "$BASE_URL" --admin-password "$ADMIN_PASSWORD" --index "$INDEX" \
    --corpus data/synthetic_logs.jsonl --anonymized "$ANONYMIZED_IN"
VERIFY_STATUS=$?
set -e

echo ""
echo "=== IMPORTANT: cleanup (not run automatically) ==="
echo "Splunk Web (if you want to look around first): http://localhost:8001 (admin / $ADMIN_PASSWORD)"
echo ""
echo "Stop and remove the container + its volume:"
echo ""
echo "  docker compose -f docker-compose-splunk.yml down -v"
echo ""
echo "This is a throwaway single-container instance -- there is no ongoing cost to"
echo "forget to clean up (unlike the Azure Blob Storage test), but leaving it running"
echo "does hold local disk/memory."

exit $VERIFY_STATUS
