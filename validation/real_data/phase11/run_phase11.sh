#!/usr/bin/env bash
# PCCF phase 11 (real documents), end to end in the Docker image:
#   1. fetch the datasets that are missing (TAB, BTC, WNUT-17, GermEval, FactRuEval, Enron,
#      ko_legal_precedents (open, no permission needed);
#      ANERcorp only if you downloaded it manually into datasets/manual/anercorp/),
#   2. run the detection layers into the persistent cache (progress printed),
#   3. evaluate H41/H42 with the phase-5 harness, then H43 + descriptive arms.
#   bash validation/real_data/phase11/run_phase11.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RD="$(cd "${HERE}/.." && pwd)"; REPO="$(cd "${RD}/../.." && pwd)"; P5="${RD}/phase5"
DATA="${RD}/datasets/large"; MANUAL="${RD}/datasets/manual"; OUT="${HERE}/docker_out"
mkdir -p "${OUT}" "${P5}/docker_run/cache" "${P5}/docker_run/hf_cache" "${MANUAL}"
source "${RD}/hf_token_prompt.sh"
if [[ -n "$(docker ps -q --filter name=redact-pccf-)" ]]; then
  echo "!! another PCCF container is running:"; docker ps --filter name=redact-pccf-; exit 1; fi
ROWS="en_tab_redact,en_tab_spacy,en_btc_redact,en_btc_spacy,en_wnut_redact,en_wnut_spacy,de_germeval,ru_factrueval,ar_anercorp,en_enron_redact,en_enron_spacy,ko_legal_precedents"
docker build -q -f "${P5}/Dockerfile.pccf_multilang" -t redact-pccf-multilang "${P5}" >/dev/null
MISSING=""
for pair in tab:EN_TAB btc:EN_BTC wnut:EN_WNUT germeval:DE_GERMEVAL factrueval:RU_FACTRUEVAL enron:EN_ENRON anercorp:AR_ANERCORP ko_legal_precedents:KO_LEGAL_PRECEDENTS; do
  k=${pair%%:*}; f=${pair#*:}_large.jsonl
  [[ -s "${DATA}/${f}" ]] || MISSING="${MISSING:+${MISSING},}${k}"
done
if [[ -n "${MISSING}" ]]; then
  echo "== fetching: ${MISSING}"
  docker run --rm --name redact-pccf-fetch ${HF_ENV} -v "${REPO}:/src:ro" -v "${DATA}:/data" -v "${MANUAL}:/manual:ro" \
    -v "${P5}/docker_run/hf_cache:/root/.cache/huggingface" redact-pccf-multilang \
    python /src/validation/real_data/phase11/fetch_phase11.py --out /data --only "${MISSING}" --anercorp-dir /manual/anercorp --enron-dir /manual/enron \
    2>&1 | grep -v "Generating\|Warning: You are sending" | tee "${OUT}/fetch.log"
fi
echo "== detection layers + evaluation (progress below)"
docker run --rm --name redact-pccf-p11 ${HF_ENV} -v "${REPO}:/src:ro" -v "${OUT}:/out" -v "${P5}/docker_run/cache:/cache" \
  -v "${P5}/docker_run/hf_cache:/root/.cache/huggingface" -e PCCF_PHASE5_CACHE=/cache -e PYTHONUNBUFFERED=1 \
  redact-pccf-multilang bash -c "
  set -e; rm -rf /work/repo && cp -r /src /work/repo && cd /work/repo/validation/real_data
  rm -rf phase11/results
  python phase5/pccf_phase5_all_languages.py --build-cache --langs ${ROWS} --budget 1e9 2>&1 | grep -v Warning
  python phase5/pccf_phase5_all_languages.py --langs ${ROWS} > /out/phase5_eval.log 2>&1
  grep -E '^  H4[12]: |real-document rows' /out/phase5_eval.log || true
  cp phase5/pccf_phase5_*en_tab_redact*_results.* phase5/TROUBLESHOOTING_*en_tab_redact*.json /out/
  echo '== H43 + descriptive arms'
  python phase11/pccf_phase11.py --run
  python phase11/pccf_phase11.py --judge | tail -5
  rm -rf /out/results && cp -r phase11/results /out/results && cp /work/repo/PCCF_PHASE11_RESULTS.md /out/
" 2>&1 | tee "${OUT}/run.log"
cp "${OUT}/PCCF_PHASE11_RESULTS.md" "${REPO}/" && rm -rf "${HERE}/results" && cp -r "${OUT}/results" "${HERE}/results"
echo "Done. Results: PCCF_PHASE11_RESULTS.md and validation/real_data/phase11/"
