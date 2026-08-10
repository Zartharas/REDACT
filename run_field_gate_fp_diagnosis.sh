#!/usr/bin/env bash
# Root-causes the real-data precision regression found in Task #10:
# field-gated (header-stripped) measured WORSE precision than naive on
# real Loghub OpenSSH/Linux text (0.974->0.778 and 0.920->0.797), with
# essentially zero recall gain, contradicting the synthetic-corpus result.
# This dumps the exact candidate text sent to NER for each new false
# positive so we can see the actual mechanism, not guess at it.
#
# Run from the repo root:
#   chmod +x run_field_gate_fp_diagnosis.sh
#   ./run_field_gate_fp_diagnosis.sh
set -euo pipefail

git push origin main   # publish the diagnostic script + doc corrections first

cd validation/real_data
echo "=== Linux ==="
python diagnose_field_gate_false_positives.py --dataset Linux --max-examples 15
echo
echo "=== OpenSSH ==="
python diagnose_field_gate_false_positives.py --dataset OpenSSH --max-examples 15
