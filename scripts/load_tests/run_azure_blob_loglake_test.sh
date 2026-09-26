#!/bin/bash
# Ask 2 (manuscript revision engineering support): a real, not simulated,
# cloud object-storage-backed security log lake, using the same Azure for
# Students subscription already validated for Task #52 (run_azure_lb_test.sh).
#
# WHAT THIS TESTS, narrowly: does REDACT's real anonymized output survive a
# round trip through real Azure Blob Storage with (a) a real Hive-style
# partitioned layout (the standard convention for both Blob-backed lakes and
# most SIEM bulk-ingestion paths) and (b) zero raw PII reaching cloud
# storage in any form. It does NOT test production-scale volume, lifecycle
# policies, access-tier tiering, or a real analytics query engine (Synapse/
# Databricks) on top of the lake -- those are real further steps, not
# claimed here.
#
# REAL FINDING THIS SCRIPT'S OWN PREP STEP EXISTS TO CLOSE: src/pipeline.py's
# output/anonymized.jsonl writes an "original" field containing the RAW
# pre-anonymization log text next to the anonymized version (useful for
# local evaluation, dangerous to upload as-is). Uploading that file
# unmodified would put real SSNs/credit-card numbers into live cloud
# storage. validation/cloud_loglake/prepare_loglake_upload.py strips
# "original" via an explicit allow-list (not a delete-and-hope), producing
# the only file this script ever uploads. Read that script's own docstring
# before changing anything about what gets uploaded.
#
# COST, disclosed plainly: one Standard general-purpose v2 Storage Account,
# LRS redundancy, Hot tier, holding a few hundred KB for the duration of
# this test. Azure Blob Storage's Hot-tier pricing is fractions of a cent
# per GB per month; a short-lived test at this scale is not meaningfully
# distinguishable from free against Azure for Students' $100 credit.
# Nothing is deleted automatically -- see the cleanup reminder at the end.
#
# PREREQUISITE: run the real pipeline locally first (needs spaCy/Presidio,
# not available in the sandbox this script was written in):
#   python3 src/pipeline.py --in data/synthetic_logs.jsonl \
#       --out output/anonymized.jsonl --audit-out output/audit_log.jsonl
#
# REQUIRES: Azure CLI (`az`), already logged in (`az login`). Not run live
# here (no Azure CLI/account in this sandbox) -- every `az storage` command
# below checked against the current documented CLI syntax, same disclosure
# as run_azure_lb_test.sh before its first live run.
set -euo pipefail

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: Azure CLI ('az') not found. See run_azure_lb_test.sh's header for install links." >&2
  exit 1
fi

if ! az account show >/dev/null 2>&1; then
  echo "ERROR: not logged in to Azure CLI. Run 'az login' first." >&2
  exit 1
fi

ANONYMIZED_IN="output/anonymized.jsonl"
if [ ! -f "$ANONYMIZED_IN" ]; then
  echo "ERROR: $ANONYMIZED_IN not found. Run the prerequisite pipeline command from" >&2
  echo "this script's own header comment first." >&2
  exit 1
fi

RG="redact-loglake-test"
# Same region-restriction lesson as run_azure_lb_test.sh -- Azure for
# Students subscriptions are subscription-specifically region-restricted.
# Default to whatever region already worked for the LB test; override as
# the first argument if that region isn't right for this subscription.
LOCATION="${1:-northcentralus}"
# Storage account names must be globally unique, lowercase alphanumeric
# only, 3-24 chars -- appending a short random suffix avoids colliding with
# another account (including a stale prior run's) rather than hardcoding
# one fixed name.
SUFFIX=$(python3 -c "import random,string; random.seed(); print(''.join(random.choices(string.ascii_lowercase+string.digits, k=6)))")
STORAGE_ACCOUNT="redactloglake${SUFFIX}"
CONTAINER="security-logs-anonymized"
UPLOAD_DIR="output/loglake_upload"
DOWNLOAD_DIR="output/loglake_downloaded"

echo "=== Part 0: resource group ==="
az group create --name "$RG" --location "$LOCATION" --output none
echo "Resource group: $RG ($LOCATION)"

echo ""
echo "=== Part 1: strip 'original' field and partition by log_type (local, no cloud calls) ==="
python3 validation/cloud_loglake/prepare_loglake_upload.py --in "$ANONYMIZED_IN" --out-dir "$UPLOAD_DIR"

echo ""
echo "=== Part 2: real Storage Account (StorageV2, LRS, Hot tier) + container ==="
az storage account create --resource-group "$RG" --name "$STORAGE_ACCOUNT" \
    --location "$LOCATION" --sku Standard_LRS --kind StorageV2 \
    --access-tier Hot --output none
echo "Storage account: $STORAGE_ACCOUNT"

ACCOUNT_KEY=$(az storage account keys list --resource-group "$RG" \
    --account-name "$STORAGE_ACCOUNT" --query '[0].value' --output tsv)

az storage container create --account-name "$STORAGE_ACCOUNT" \
    --account-key "$ACCOUNT_KEY" --name "$CONTAINER" --output none
echo "Container: $CONTAINER"

echo ""
echo "=== Part 3: real upload -- partitioned, PII-stripped files only ==="
az storage blob upload-batch --account-name "$STORAGE_ACCOUNT" \
    --account-key "$ACCOUNT_KEY" --destination "$CONTAINER" \
    --source "$UPLOAD_DIR" --output table

echo ""
echo "=== Part 4: real download back, into a separate directory (round-trip check) ==="
rm -rf "$DOWNLOAD_DIR"
mkdir -p "$DOWNLOAD_DIR"
az storage blob download-batch --account-name "$STORAGE_ACCOUNT" \
    --account-key "$ACCOUNT_KEY" --source "$CONTAINER" \
    --destination "$DOWNLOAD_DIR" --output table

echo ""
echo "=== Part 5: DLP-style verification -- does any raw ground-truth PII value survive? ==="
# set +e/-e around this one call: a non-zero exit here (real PII found) is
# a genuine result to report, not a script failure -- the cleanup
# reminder below must still print either way, same principle as
# run_azure_lb_test.sh never treating a failed data-plane check as fatal.
set +e
python3 validation/cloud_loglake/verify_no_pii_in_blob.py \
    --corpus data/synthetic_logs.jsonl \
    --anonymized "$ANONYMIZED_IN" \
    --downloaded-dir "$DOWNLOAD_DIR"
VERIFY_STATUS=$?
set -e

echo ""
echo "=== IMPORTANT: cleanup (not run automatically) ==="
echo "Deleting the resource group removes the storage account and everything in it:"
echo ""
echo "  az group delete --resource-group $RG --yes --no-wait"
echo ""
echo "Do this once you've confirmed the result above -- Storage accounts are cheap at"
echo "this scale but not free, same principle as run_azure_lb_test.sh's cleanup reminder."

exit $VERIFY_STATUS
