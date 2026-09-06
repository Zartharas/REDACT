#!/bin/bash
# Fetches the five raw Loghub 2,000-line samples inject_and_evaluate.py's
# own module docstring has always told you to fetch first -- except this
# script never actually existed until now. A real gap, found while
# researching additional real-world corpora to strengthen the paper
# before resubmission (2026-09): the docstring's instruction was correct,
# the file it named was not, so anyone following it exactly (including a
# fresh chat with no memory of this project) would hit a missing-file
# error with no way to resolve it from the repo alone.
#
# Source: Loghub (Zhu et al., ISSRE 2023), https://github.com/logpai/loghub
# -- the free, no-request-form 2,000-line "_2k.log" samples each system's
# own GitHub directory publishes directly (not the full multi-GB raw logs,
# which for some systems need a Zenodo request form; the 2k samples don't).
#
# MUST be run on a machine with real internet access, not this project's
# sandbox -- same disclosed limitation as prepare_cloudtrail_dataset.py
# (raw.githubusercontent.com is blocked by the sandbox's network
# allowlist). Confirmed reachable from outside the sandbox by fetching
# OpenStack_2k.log directly during this same research pass.
#
# Usage:
#   cd validation/real_data
#   ./download_loghub.sh
set -euo pipefail

mkdir -p datasets
cd datasets

# OpenSSH, Linux, Thunderbird: consumed by build_user_field_corpus() via
# USER_FIELD_DATASETS -- each has a real "user X" / "for user X"
# authentication field this project injects a synthetic identity into
# (see inject_and_evaluate.py's own module docstring for why: the real
# values in these fields are attacker-guessed usernames or fixed system
# accounts, not real people, so testing PERSON detection against them
# directly would test a fabricated scenario).
#
# OpenStack, Zookeeper: consumed by build_ip_only_corpus() via
# IP_ONLY_DATASETS -- used AS-IS, no injection, because these two contain
# genuinely real, unmodified IP addresses in connection/API log lines
# (confirmed directly: OpenStack_2k.log line 1 has "10.11.10.1" embedded
# in a real nova-api request log, not a placeholder or substituted value).
# This is the condition that closes a gap this project's own README
# discloses about itself: the CloudTrail sample's sourceIPAddress field
# is real-SHAPED but publisher-anonymized (run through a format-preserving
# substitution tool before release), so it can't validate IP detection
# against a genuinely real, unmodified value the way these two can.
DATASETS=(OpenSSH Linux Thunderbird OpenStack Zookeeper)
BASE_URL="https://raw.githubusercontent.com/logpai/loghub/master"

for name in "${DATASETS[@]}"; do
    out="${name}_2k.log"
    if [ -f "$out" ]; then
        echo "${out} already present, skipping."
        continue
    fi
    url="${BASE_URL}/${name}/${name}_2k.log"
    echo "Downloading ${url} ..."
    curl -sfL "$url" -o "$out" || {
        echo "FAILED to download ${name}_2k.log from ${url}."
        echo "Loghub's directory layout may have changed since this script was"
        echo "written (2026-09) -- check https://github.com/logpai/loghub/tree/master/${name}"
        echo "for the current filename/path and adjust BASE_URL or DATASETS above."
        exit 1
    }
    lines=$(wc -l < "$out" | tr -d ' ')
    echo "  -> ${out} (${lines} lines)"
done

echo ""
echo "All five Loghub samples present in $(pwd)."
echo "Next step: python3 inject_and_evaluate.py (run from validation/real_data/)"
