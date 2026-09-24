#!/usr/bin/env bash
# Clean-room recomputation of PCCF phases 5-10A from the saved detection caches,
# compared with the committed results (floats within 0.005; verdicts exact).
# Phases 1-3 are covered by run_pccf_docker.sh. Detection layers are NOT re-run
# here (that is run_all_languages.sh); this checks every number derived from them.
#   bash validation/real_data/run_pccf_repro_p5to10.sh
# Prints progress per phase. Phase 6 (the slowest) runs in parallel with 7-10.
# The work is CPU-bound: a Hugging Face token does not make it faster (it only
# helps if a model has to be downloaded), but you may enter one at the prompt.
set -euo pipefail
RD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${RD}/../.." && pwd)"
P5="${RD}/phase5"
OUT="${RD}/docker_repro_p5to10"; mkdir -p "${OUT}"
source "${RD}/hf_token_prompt.sh"
if [[ -n "$(docker ps -q --filter name=redact-pccf-repro)" ]]; then
  echo "!! a reproduction container is already running:"; docker ps --filter name=redact-pccf-repro; exit 1
fi
docker build -q -f "${P5}/Dockerfile.pccf_multilang" -t redact-pccf-multilang "${P5}" >/dev/null
echo "== running phases 5-10 in a clean container (progress below; phase 6 runs in parallel)"
docker run --rm --name redact-pccf-repro ${HF_ENV} -v "${REPO}:/src:ro" -v "${OUT}:/out" -v "${P5}/docker_run/cache:/cache:ro" \
  -v "${P5}/docker_run/hf_cache:/root/.cache/huggingface" -e PCCF_PHASE5_CACHE=/cache -e PYTHONUNBUFFERED=1 \
  redact-pccf-multilang bash -c '
  set -e
  ts() { date +%H:%M:%S; }
  rm -rf /work/repo && cp -r /src /work/repo && cd /work/repo/validation/real_data
  rm -rf phase6/results phase7/results phase8/results phase9/results phase10/results
  L=$(grep -o "LANGS=\"\${LANGS:-[^}]*" phase5/run_all_languages.sh | sed "s/.*:-//")
  echo "[$(ts)] phase 6 started in background (split diagnostic, 50 seeds x 2 protocols)"
  ( python phase6/pccf_phase6_split_diagnostic.py --budget 1e9 > /out/phase6.log 2>&1
    python phase6/pccf_phase6_split_diagnostic.py --langs tl_gl --budget 1e9 >> /out/phase6.log 2>&1
    echo "[$(ts)] phase 6 done" ) &
  P6=$!
  echo "[$(ts)] phase 5: re-evaluating $(echo "$L" | tr , " " | wc -w) rows"
  python phase5/pccf_phase5_all_languages.py --langs "$L" > /out/phase5.log 2>&1
  echo "[$(ts)] phase 5 done: $(grep -E "H2[2389]|H3[34]" /out/phase5.log | tr -s " " | tr "\n" ";")"
  echo "[$(ts)] phase 7 / 7b";  python phase7/pccf_phase7_baselines.py --budget 1e9 > /out/phase7.log 2>&1
  python phase7/pccf_phase7_baselines.py --b --budget 1e9 >> /out/phase7.log 2>&1
  echo "[$(ts)] phase 8";       python phase8/pccf_phase8.py --budget 1e9 > /out/phase8.log 2>&1
  echo "[$(ts)] phase 9";       python phase9/pccf_phase9.py --budget 1e9 > /out/phase9.log 2>&1
  echo "[$(ts)] phase 10A";     until python phase10/pccf_phase10_aci_stream.py 2>>/out/phase10.log | tail -1 | grep -q complete; do :; done
  echo "[$(ts)] waiting for phase 6 ($(ls phase6/results 2>/dev/null | wc -l)/40 result files so far)"
  wait $P6
  mkdir -p /out/new/phase5
  cp phase5/pccf_phase5_*_results.json /out/new/phase5/
  for p in phase6 phase7 phase8 phase9 phase10; do mkdir -p /out/new/$p && cp -r $p/results /out/new/$p/; done
  echo "[$(ts)] all phases done"
' 2>&1 | tee "${OUT}/run.log"
echo "== comparing with the committed results"
cd "${RD}"
P5REF=$(ls -t phase5/docker_run/pccf_phase5_*tl_gl_results.json | head -1)
mkdir -p "${OUT}/ref/phase5" && cp "${P5REF}" "${OUT}/ref/phase5/"
[ -f "${OUT}/new/phase5/$(basename "${P5REF}")" ] || { echo "phase-5 recomputation missing"; exit 1; }
RELS="phase5/$(basename "${P5REF}")"
for f in phase6/results/*.json phase7/results/*.json phase8/results/*.json phase9/results/*.json phase10/results/*.json; do
  RELS="${RELS} ${f}"; mkdir -p "${OUT}/ref/$(dirname "$f")"; cp "$f" "${OUT}/ref/$f"
done
python3 compare_repro_json.py "${OUT}/ref" "${OUT}/new" ${RELS} | tee "${OUT}/compare.txt" | grep -v "^ok  "
