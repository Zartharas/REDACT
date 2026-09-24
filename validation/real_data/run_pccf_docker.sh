#!/usr/bin/env bash
# PCCF clean-room reproduction (T6). Run from anywhere on a machine with
# Docker Desktop (or another Docker runtime) running:
#
#   bash validation/real_data/run_pccf_docker.sh
#
# First run: about 10-15 min to build (4 spaCy models, about 1 GB), then
# about 15-25 min to run. The repo is mounted READ-ONLY; results go to
# validation/real_data/docker_repro/ (run.log, results/, compare.txt).
# PASS means identical verdicts and every metric within +/-0.005 of the
# committed results.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUT="${SCRIPT_DIR}/docker_repro"
mkdir -p "${OUT}"
# Apple Silicon: the spaCy and numpy wheels exist for linux/arm64, so no
# emulation is needed. To force x86_64 parity instead, add
# --platform linux/amd64 to both commands below.
docker build -f "${SCRIPT_DIR}/Dockerfile.pccf" -t redact-pccf "${REPO_ROOT}"
docker run --rm -v "${REPO_ROOT}:/src:ro" -v "${OUT}:/out" redact-pccf
echo
echo "Done. Verdict: $(tail -1 "${OUT}/compare.txt")"
echo "Full log: ${OUT}/run.log   Comparison: ${OUT}/compare.txt"
