#!/usr/bin/env bash
# PCCF phase 5: fetch + evaluate ALL target languages (fr ru id ko zh in)
# in one go, on your Mac, with Docker Desktop running:
#
#   bash validation/real_data/phase5/run_all_languages.sh              # fetch missing languages, then evaluate all
#   REFETCH=1 bash validation/real_data/phase5/run_all_languages.sh    # re-download every language
#   LANGS=ko,zh bash validation/real_data/phase5/run_all_languages.sh  # only these languages
#
# What it writes (nothing else in your repo is touched):
#   validation/real_data/datasets/large/            fetched data + MANIFEST.json + SCHEMA_SAMPLE_<lang>.json
#   validation/real_data/phase5/docker_run/         run.log, results .txt/.json, TROUBLESHOOTING_*.json
# The repo is mounted READ-ONLY for the evaluation and copied inside the
# container, so a broken language cannot corrupt your checkout. Each
# language is isolated: an exception in one is recorded in
# TROUBLESHOOTING_*.json and the others still run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../.." && pwd)"
DATA="${REPO}/validation/real_data/datasets/large"
OUT="${HERE}/docker_run"
LANGS="${LANGS:-fr,ru,de,it,nl,pt,fi,tr,hi,te,ar,ar_wiki,ja,id,id_hf,ko,zh,in,ko_kdpii,ko_legal}"
mkdir -p "${DATA}" "${OUT}"

if ! docker image inspect redact-pccf >/dev/null 2>&1; then
  echo "== building base image redact-pccf"
  docker build -f "${REPO}/validation/real_data/Dockerfile.pccf" -t redact-pccf "${REPO}"
fi
# amendment 5: persist the Hugging Face cache on the host (models download once,
# not every run) and pass HF_TOKEN through ONLY if it is set in your shell.
# The token value never appears in this script, the image, or the logs.
HF_CACHE="${OUT}/hf_cache"; mkdir -p "${HF_CACHE}"
HF_ENV=""; if [[ -n "${HF_TOKEN:-}" ]]; then HF_ENV="-e HF_TOKEN"; fi
echo "== building redact-pccf-multilang"
docker build -f "${HERE}/Dockerfile.pccf_multilang" -t redact-pccf-multilang "${HERE}"

# (no associative arrays: macOS ships bash 3.2)
file_for() {
  case "$1" in
    fr) echo OpenPII_FR_large.jsonl ;; ru) echo RU_PII_large.jsonl ;; id) echo ID_OpenPII_large.jsonl ;;
    ko) echo KO_BCCard_large.jsonl ;; zh) echo ZH_piibench_large.jsonl ;; in) echo IN_indiapii_large.jsonl ;;
    ko_kdpii) echo KO_KDPII_large.jsonl ;;
    ar) echo AR_sitr_large.jsonl ;; ar_wiki) echo AR_wikiann_large.jsonl ;; tr) echo TR_kvkk_large.jsonl ;; id_hf) echo ID_OpenPII_large.jsonl ;;
    de|it|nl|pt|fi|hi|te|ja) echo "$(echo "$1" | tr '[:lower:]' '[:upper:]')_OpenPII_large.jsonl" ;;
    ko_legal) echo ../restricted/KO_LEGAL_K-LegalDeID.jsonl ;;
    *) echo "unknown language: $1" >&2; exit 1 ;;
  esac
}
TOFETCH=""
for l in ${LANGS//,/ }; do
  if [[ "$l" == "ko_legal" ]]; then
    # Not downloadable: K-LegalDeID must be obtained from its authors (CC BY-NC-SA, research-only).
    if [[ ! -s "${DATA}/$(file_for "$l")" ]]; then
      echo "== ko_legal: no data yet. Get K-LegalDeID from the authors, then run:"
      echo "     python validation/real_data/phase5/convert_k_legaldeid.py <files-or-folder>"
      echo "   (the other languages still run; ko_legal will show 'no data file')"
    fi
    continue
  fi
  if [[ -n "${REFETCH:-}" || ! -s "${DATA}/$(file_for "$l")" ]]; then TOFETCH="${TOFETCH:+${TOFETCH},}${l}"; fi
done
if [[ -n "${TOFETCH}" ]]; then
  echo "== fetching: ${TOFETCH}"
  docker run --rm ${HF_ENV} -v "${HF_CACHE}:/root/.cache/huggingface" -v "${REPO}:/src:ro" -v "${DATA}:/out" redact-pccf-multilang \
    python /src/validation/real_data/phase4/fetch_large_multilang.py --out /out --only "${TOFETCH}" 2>&1 | tee "${OUT}/fetch.log"
else
  echo "== all requested languages already fetched (REFETCH=1 to force)"
fi

# amendment 5: two runs at once share docker_run/cache and overwrite each other's files
# (a fixed container name catches older runs too, whose image tag has since moved)
if [[ -n "$(docker ps -q --filter name=redact-pccf-phase5-eval)$(docker ps -q --filter ancestor=redact-pccf-multilang)" ]]; then
  echo "!! another redact-pccf-multilang container is already running:"
  docker ps --filter ancestor=redact-pccf-multilang
  echo "   wait for it, or stop it with: docker stop \$(docker ps -q --filter ancestor=redact-pccf-multilang)"
  exit 1
fi
echo "== evaluating: ${LANGS}"
# amendment 5: caches persist on the host in ${OUT}/cache (seeded once from output/phase5)
mkdir -p "${OUT}/cache"
if [[ -z "$(ls -A "${OUT}/cache" 2>/dev/null)" && -d "${REPO}/output/phase5" ]]; then
  cp "${REPO}"/output/phase5/*.json "${OUT}/cache/" 2>/dev/null || true
fi
docker run --rm --name redact-pccf-phase5-eval ${HF_ENV} -v "${HF_CACHE}:/root/.cache/huggingface" -e PCCF_PHASE5_CACHE=/cache -v "${REPO}:/src:ro" -v "${OUT}:/out" -v "${OUT}/cache:/cache" \
  redact-pccf-multilang bash -c "
  set -e
  rm -rf /work/repo && cp -r /src /work/repo && cd /work/repo
  S=validation/real_data/phase5/pccf_phase5_all_languages.py
  python -m pytest -q tests/test_pccf.py
  python \$S --build-cache --langs ${LANGS} --budget 1e9
  python \$S --langs ${LANGS}
  cp validation/real_data/phase5/pccf_phase5_*_results.* validation/real_data/phase5/TROUBLESHOOTING_*.json /out/
" 2>&1 | tee "${OUT}/run.log"
echo
echo "Done. Results + TROUBLESHOOTING in: ${OUT}"
