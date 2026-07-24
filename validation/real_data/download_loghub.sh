#!/bin/bash
# Downloads the five Loghub datasets used in the real-data validation
# (paper Section 5.7). Source: logpai/loghub, Zhu et al., ISSRE 2023.
# These are real, unmodified system logs, not REDACT's own synthetic corpus.
set -e
mkdir -p datasets
for d in OpenSSH Linux Thunderbird OpenStack Zookeeper; do
    echo "Fetching ${d}..."
    curl -sL "https://raw.githubusercontent.com/logpai/loghub/master/${d}/${d}_2k.log" \
        -o "datasets/${d}_2k.log"
done
echo "Done. Five files in datasets/, ~2,000 lines each."
