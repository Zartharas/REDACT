#!/usr/bin/env bash
# Task #10: real-data validation of field-gated NER (detect.detect_all_field_gated,
# the actual function src/service.py/src/pipeline.py call by default now),
# against real, unmodified Loghub log files -- not this project's own
# synthetic corpus.
#
# Run from the repo root:
#   chmod +x run_real_data_field_gate_check.sh
#   ./run_real_data_field_gate_check.sh
set -euo pipefail

git push origin main   # publish today's fields.py fix + validation extension first

cd validation/real_data
bash download_loghub.sh   # fetches OpenSSH/Linux/Thunderbird/OpenStack/Zookeeper into datasets/
python inject_and_evaluate.py
