#!/usr/bin/env bash
# Clean-room recomputation of PCCF phases 5-10A from the saved detection caches,
# compared with the committed results (floats within 0.005; verdicts exact).
# Phases 1-3 are covered by run_pccf_docker.sh. Detection layers are NOT re-run
# here (that is run_all_languages.sh); this checks every number derived from them.
#   bash validation/real_data/run_pccf_repro_p5to10.sh
set -euo pipefail
RD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${RD}/../.." && pwd)"
P5="${RD}/phase5"
OUT="${RD}/docker_repro_p5to10"; mkdir -p "${OUT}"
docker build -f "${P5}/Dockerfile.pccf_multilang" -t redact-pccf-multilang "${P5}"
docker run --rm -v "${REPO}:/src:ro" -v "${OUT}:/out" -v "${P5}/docker_run/cache:/cache:ro" \
  -e PCCF_PHASE5_CACHE=/cache redact-pccf-multilang bash -c '
  set -e
  rm -rf /work/repo && cp -r /src /work/repo && cd /work/repo/validation/real_data
  rm -rf phase6/results phase7/results phase8/results phase9/results phase10/results
  L=$(grep -o "LANGS=\"\${LANGS:-[^}]*" phase5/run_all_languages.sh | sed "s/.*:-//")
  python phase5/pccf_phase5_all_languages.py --langs "$L" > /out/phase5.log
  until python phase6/pccf_phase6_split_diagnostic.py --budget 1e9 | tail -1 | grep -q complete; do :; done
  python phase6/pccf_phase6_split_diagnostic.py --langs tl_gl --budget 1e9 > /dev/null
  python phase7/pccf_phase7_baselines.py --budget 1e9 > /dev/null
  python phase7/pccf_phase7_baselines.py --b --budget 1e9 > /dev/null
  python phase8/pccf_phase8.py --budget 1e9 > /dev/null
  python phase9/pccf_phase9.py --budget 1e9 > /dev/null
  until python phase10/pccf_phase10_aci_stream.py | tail -1 | grep -q complete; do :; done
  mkdir -p /out/new/phase5 /out/new/phase6 /out/new/phase7 /out/new/phase8 /out/new/phase9 /out/new/phase10
  cp phase5/pccf_phase5_*_results.json /out/new/phase5/
  for p in phase6 phase7 phase8 phase9 phase10; do cp -r $p/results /out/new/$p/; done
' 2>&1 | tee "${OUT}/run.log"
# Compare: every committed JSON under phase6-10 results, plus the latest phase-5 results file.
cd "${RD}"
P5REF=$(ls -t phase5/docker_run/pccf_phase5_*tl_gl_results.json | head -1)
mkdir -p "${OUT}/ref/phase5" && cp "${P5REF}" "${OUT}/ref/phase5/"
NEWP5="${OUT}/new/phase5/$(basename "${P5REF}")"
[ -f "${NEWP5}" ] || { echo "phase-5 recomputation missing: ${NEWP5}"; exit 1; }
RELS="phase5/$(basename "${P5REF}")"
for f in phase6/results/*.json phase7/results/*.json phase8/results/*.json phase9/results/*.json phase10/results/*.json; do
  RELS="${RELS} ${f}"; mkdir -p "${OUT}/ref/$(dirname "$f")"; cp "$f" "${OUT}/ref/$f"
done
python3 compare_repro_json.py "${OUT}/ref" "${OUT}/new" ${RELS} | tee "${OUT}/compare.txt"
