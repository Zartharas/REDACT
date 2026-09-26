#!/usr/bin/env bash
# F5: fetch larger FR/RU PII samples on YOUR Mac (Hugging Face is blocked
# from Claude's workspaces). Reuses the redact-pccf image built by
# run_pccf_docker.sh. Writes ONLY to validation/real_data/datasets/large/.
#   bash validation/real_data/phase4/fetch_large_multilang.sh            # both
#   bash validation/real_data/phase4/fetch_large_multilang.sh --only ru  # Russian only
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../.." && pwd)"
OUT="${REPO}/validation/real_data/datasets/large"
mkdir -p "${OUT}"
docker run --rm -v "${REPO}:/src:ro" -v "${OUT}:/out" redact-pccf \
  bash -c "pip install -q 'datasets>=2.19' && python /src/validation/real_data/phase4/fetch_large_multilang.py --out /out $*"
echo "Done: ${OUT}  (see MANIFEST.json)"
