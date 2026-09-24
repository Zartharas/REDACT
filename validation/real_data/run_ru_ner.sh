#!/usr/bin/env bash
# Builds and runs the Russian-NER PII evaluation in Docker -- the one piece
# of this validation pass that needs real internet access this project's
# own sandbox doesn't have (see Dockerfile.ru_ner and src/ru_ner.py for
# why). Near-identical to run_fr_ner.sh/run_meddocan_ner.sh, with only the
# image/model/results-file names swapped.
#
# Usage: run this script from ANYWHERE -- it cd's to the repo root itself,
# so you don't need to be in any particular directory first.
#
#   bash validation/real_data/run_ru_ner.sh
#
# What it does:
#   1. Builds redact-ru-ner from Dockerfile.ru_ner (downloads spaCy's
#      ru_core_news_md Russian model at build time -- needs real internet,
#      ~5-10 min the first time, cached after that as long as the image
#      isn't removed).
#   2. Runs it with the WHOLE repo mounted read-only at /app, so
#      evaluate_ru.py sees the same src/, validation/real_data/datasets/,
#      ru_detect.py, and ru_ner.py you have locally -- no separate copy to
#      keep in sync.
#   3. Prints results to your terminal AND tees them to
#      validation/real_data/ru_ner_results.txt, so you can paste that
#      file's contents back for the write-up to be updated with real (not
#      dictionary-only) PERSON numbers.
#
# Requires: Docker Desktop (or another Docker runtime) installed and
# running locally. Nothing else -- the image installs its own Python
# dependencies, it does not touch your host Python environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RESULTS_FILE="${SCRIPT_DIR}/ru_ner_results.txt"

echo "Repo root: ${REPO_ROOT}"
echo "Building redact-ru-ner (downloads ru_core_news_md -- first run only, ~5-10 min)..."
docker build -f "${SCRIPT_DIR}/Dockerfile.ru_ner" -t redact-ru-ner "${REPO_ROOT}"

echo
echo "Running evaluate_ru.py --with-ner inside the container..."
echo "(results also being saved to ${RESULTS_FILE})"
echo

docker run --rm \
    -v "${REPO_ROOT}:/app:ro" \
    redact-ru-ner \
    python /app/validation/real_data/evaluate_ru.py --with-ner --diagnose \
    | tee "${RESULTS_FILE}"

echo
echo "Done. Full results saved to: ${RESULTS_FILE}"
echo "Share that file's contents back to fold real Russian-NER PERSON numbers"
echo "into the Russian write-up (currently dictionary-only)."
