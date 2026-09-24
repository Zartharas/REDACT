#!/usr/bin/env bash
# Builds and runs the French-NER OpenPII evaluation in Docker -- the one
# piece of this validation pass that needs real internet access this
# project's own sandbox doesn't have (see Dockerfile.fr_ner and
# src/fr_ner.py for why). Near-identical to run_meddocan_ner.sh, with only
# the image/model/results-file names swapped.
#
# Usage: run this script from ANYWHERE -- it cd's to the repo root itself,
# so you don't need to be in any particular directory first.
#
#   bash validation/real_data/run_fr_ner.sh
#
# What it does:
#   1. Builds redact-fr-ner from Dockerfile.fr_ner (downloads spaCy's
#      fr_core_news_md French model at build time -- needs real internet,
#      ~5-10 min the first time, cached after that as long as the image
#      isn't removed).
#   2. Runs it with the WHOLE repo mounted read-only at /app, so
#      evaluate_fr.py sees the same src/, validation/real_data/datasets/,
#      fr_detect.py, and fr_ner.py you have locally -- no separate copy to
#      keep in sync.
#   3. Prints results to your terminal AND tees them to
#      validation/real_data/fr_ner_results.txt, so you can paste that
#      file's contents back for the write-up to be updated with real (not
#      dictionary-only) PERSON numbers.
#
# Requires: Docker Desktop (or another Docker runtime) installed and
# running locally. Nothing else -- the image installs its own Python
# dependencies, it does not touch your host Python environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RESULTS_FILE="${SCRIPT_DIR}/fr_ner_results.txt"

echo "Repo root: ${REPO_ROOT}"
echo "Building redact-fr-ner (downloads fr_core_news_md -- first run only, ~5-10 min)..."
docker build -f "${SCRIPT_DIR}/Dockerfile.fr_ner" -t redact-fr-ner "${REPO_ROOT}"

echo
echo "Running evaluate_fr.py --with-ner inside the container..."
echo "(results also being saved to ${RESULTS_FILE})"
echo

docker run --rm \
    -v "${REPO_ROOT}:/app:ro" \
    redact-fr-ner \
    python /app/validation/real_data/evaluate_fr.py --with-ner --diagnose \
    | tee "${RESULTS_FILE}"

echo
echo "Done. Full results saved to: ${RESULTS_FILE}"
echo "Share that file's contents back to fold real French-NER PERSON numbers"
echo "into the French write-up (currently dictionary-only)."
