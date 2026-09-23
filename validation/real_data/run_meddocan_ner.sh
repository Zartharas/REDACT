#!/usr/bin/env bash
# Builds and runs the Spanish-NER MEDDOCAN evaluation in Docker -- the one
# piece of this validation pass that needs real internet access this
# project's own sandbox doesn't have (see Dockerfile.meddocan_ner and
# src/es_ner.py for why).
#
# Usage: run this script from ANYWHERE -- it cd's to the repo root itself,
# so you don't need to be in any particular directory first.
#
#   bash validation/real_data/run_meddocan_ner.sh
#
# What it does:
#   1. Builds redact-meddocan-ner from Dockerfile.meddocan_ner (downloads
#      spaCy's es_core_news_md Spanish model at build time -- needs real
#      internet, ~5-10 min the first time, cached after that as long as the
#      image isn't removed).
#   2. Runs it with the WHOLE repo mounted read-only at /app, so
#      evaluate_meddocan.py sees the same src/, validation/real_data/
#      datasets/, es_detect.py, and es_ner.py you have locally -- no
#      separate copy to keep in sync.
#   3. Prints results to your terminal AND tees them to
#      validation/real_data/meddocan_ner_results.txt, so you can paste that
#      file's contents back for the write-up to be updated with real
#      (not dictionary-only) PERSON numbers.
#
# Requires: Docker Desktop (or another Docker runtime) installed and
# running locally. Nothing else -- the image installs its own Python
# dependencies, it does not touch your host Python environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RESULTS_FILE="${SCRIPT_DIR}/meddocan_ner_results.txt"

echo "Repo root: ${REPO_ROOT}"
echo "Building redact-meddocan-ner (downloads es_core_news_md -- first run only, ~5-10 min)..."
docker build -f "${SCRIPT_DIR}/Dockerfile.meddocan_ner" -t redact-meddocan-ner "${REPO_ROOT}"

echo
echo "Running evaluate_meddocan.py --with-ner inside the container..."
echo "(results also being saved to ${RESULTS_FILE})"
echo

docker run --rm \
    -v "${REPO_ROOT}:/app:ro" \
    redact-meddocan-ner \
    python /app/validation/real_data/evaluate_meddocan.py --with-ner --diagnose \
    | tee "${RESULTS_FILE}"

echo
echo "Done. Full results saved to: ${RESULTS_FILE}"
echo "Share that file's contents back to fold real Spanish-NER PERSON numbers"
echo "into validation_review_summary.md Section 6 (currently dictionary-only)."
