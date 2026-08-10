#!/usr/bin/env bash
# Task #10 re-run, after fixing the dangling key= excision bug found by
# diagnose_field_gate_false_positives.py. Same script as before -- the
# datasets/ folder from the first run is reused (no re-download needed).
#
# Run from the repo root:
#   chmod +x run_real_data_field_gate_check.sh
#   ./run_real_data_field_gate_check.sh
set -euo pipefail

git push origin main   # publish the key= excision fix first

cd validation/real_data
if [ ! -d datasets ]; then
    bash download_loghub.sh
fi
python inject_and_evaluate.py
