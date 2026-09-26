#!/usr/bin/env bash
# Runs INSIDE the Dockerfile.pccf container. Copies the read-only repo at
# /src to /work, deletes every PCCF cache so all layer outputs are
# recomputed from scratch, reruns phases 1-3 plus the unit tests, then
# compares against the committed results. Everything lands in /out.
set -euo pipefail
rm -rf /work/repo && cp -r /src /work/repo && cd /work/repo
rm -f output/pccf_*.json
R=validation/real_data
{
  echo "== unit tests"; python -m pytest -q tests/test_pccf.py
  echo "== phase 1";   python $R/pccf_feasibility_meddocan.py --build-cache && python $R/pccf_feasibility_meddocan.py
  echo "== phase 2";   python $R/pccf_phase2_meddocan.py --build-stress-cache && python $R/pccf_phase2_meddocan.py
  echo "== phase 3 T1/T4"; python $R/pccf_phase3_multilang.py --build-cache && python $R/pccf_phase3_multilang.py
  echo "== phase 3 T2";    python $R/pccf_phase3_freetext_es.py --build-cache && python $R/pccf_phase3_freetext_es.py
  echo "== phase 3 T3";    until python $R/pccf_phase3_telemetry.py --build-cache | tee /dev/stderr | grep -q complete; do :; done
                           python $R/pccf_phase3_telemetry.py
  echo "== phase 3 T5";    python $R/pccf_phase3_alert_tier.py
  echo "== phase 3 T7";    until python $R/pccf_phase3b_message_text.py --build-cache | tee /dev/stderr | grep -q complete; do :; done
                           python $R/pccf_phase3b_message_text.py
} 2>&1 | tee /out/run.log
mkdir -p /out/results && cp $R/pccf_*_results.* /out/results/
python $R/compare_pccf_results.py /src/$R /work/repo/$R | tee /out/compare.txt
