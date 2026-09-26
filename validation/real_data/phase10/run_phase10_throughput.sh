#!/usr/bin/env bash
# PCCF phase 10B: per-layer throughput in the redact-pccf-multilang image (single thread).
#   bash validation/real_data/phase10/run_phase10_throughput.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../.." && pwd)"
P5="${REPO}/validation/real_data/phase5"
OUT="${HERE}/throughput"; mkdir -p "${OUT}"
source "${REPO}/validation/real_data/hf_token_prompt.sh"
docker build -f "${P5}/Dockerfile.pccf_multilang" -t redact-pccf-multilang "${P5}"
docker run --rm ${HF_ENV} -e PYTHONUNBUFFERED=1 -v "${REPO}:/src:ro" -v "${OUT}:/out" -v "${P5}/docker_run/hf_cache:/root/.cache/huggingface" \
  -e OMP_NUM_THREADS=1 redact-pccf-multilang bash -c "
  set -e; rm -rf /work/repo && cp -r /src /work/repo && cd /work/repo
  python validation/real_data/phase10/pccf_phase10_throughput.py --n 2000 --out /out
" 2>&1 | tee "${OUT}/run.log"
