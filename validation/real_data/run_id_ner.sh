#!/usr/bin/env bash
# Builds and runs the Indonesian-NER PII evaluation in Docker -- the one
# piece of this validation pass that needs real internet access this
# project's own sandbox doesn't have (see Dockerfile.id_ner and
# src/id_ner.py for why, including why this Dockerfile installs
# transformers+torch instead of spacy, unlike the other three languages').
#
# IMPORTANT, READ FIRST: this script will fail immediately unless
# validation/real_data/datasets/OpenPII_ID_raw.jsonl already exists.
# As of this script's writing, IT DOES NOT -- see prepare_id_dataset.py
# for the confirmed, disclosed data-access blocker (this project's
# sandbox cannot reliably stage rows from the one Indonesian-covering
# dataset found) and the exact commands to stage it yourself with real
# internet access. Run that staging step FIRST, then this script.
#
# Usage: run this script from ANYWHERE -- it cd's to the repo root itself,
# so you don't need to be in any particular directory first.
#
#   bash validation/real_data/run_id_ner.sh
#
# What it does:
#   1. Builds redact-id-ner from Dockerfile.id_ner (downloads
#      cahya/bert-base-indonesian-NER's weights at build time -- needs
#      real internet, likely longer than the spaCy models' ~5-10 min
#      given torch's own install size, cached after that as long as the
#      image isn't removed).
#   2. Runs it with the WHOLE repo mounted read-only at /app, so
#      evaluate_id.py sees the same src/, validation/real_data/datasets/,
#      id_detect.py, and id_ner.py you have locally -- no separate copy
#      to keep in sync.
#   3. Prints results to your terminal AND tees them to
#      validation/real_data/id_ner_results.txt, so you can paste that
#      file's contents back for the write-up to be updated with real
#      (not dictionary-only) PERSON numbers.
#
# Requires: Docker Desktop (or another Docker runtime) installed and
# running locally, AND validation/real_data/datasets/OpenPII_ID_raw.jsonl
# already staged (see above). Nothing else -- the image installs its own
# Python dependencies, it does not touch your host Python environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RESULTS_FILE="${SCRIPT_DIR}/id_ner_results.txt"
DATA_FILE="${SCRIPT_DIR}/datasets/OpenPII_ID_raw.jsonl"

if [ ! -f "${DATA_FILE}" ]; then
    echo "ERROR: ${DATA_FILE} not found." >&2
    echo "This is a known, disclosed blocker -- see prepare_id_dataset.py" >&2
    echo "for why this sandbox couldn't stage it and the exact commands" >&2
    echo "to stage it yourself with real internet access. Run those first." >&2
    exit 1
fi

echo "Repo root: ${REPO_ROOT}"
echo "Building redact-id-ner (downloads cahya/bert-base-indonesian-NER -- first run only)..."
docker build -f "${SCRIPT_DIR}/Dockerfile.id_ner" -t redact-id-ner "${REPO_ROOT}"

echo
echo "Running evaluate_id.py --with-ner inside the container..."
echo "(results also being saved to ${RESULTS_FILE})"
echo

docker run --rm \
    -v "${REPO_ROOT}:/app:ro" \
    redact-id-ner \
    python /app/validation/real_data/evaluate_id.py --with-ner --diagnose \
    | tee "${RESULTS_FILE}"

echo
echo "Done. Full results saved to: ${RESULTS_FILE}"
echo "Share that file's contents back to fold real Indonesian-NER PERSON"
echo "numbers into the Indonesian write-up (currently dictionary-only, or"
echo "entirely unmeasured if OpenPII_ID_raw.jsonl was just staged for the"
echo "first time)."
