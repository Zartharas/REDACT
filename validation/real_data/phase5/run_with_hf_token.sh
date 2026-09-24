#!/usr/bin/env bash
# Run the phase-5 sweep with a Hugging Face token typed at a hidden prompt.
#
#   bash validation/real_data/phase5/run_with_hf_token.sh
#   LANGS=bg,pl bash validation/real_data/phase5/run_with_hf_token.sh   # subset
#
# The token is never echoed, never written to disk or shell history, and is
# passed only to this one run (docker gets it by name via `-e HF_TOKEN`, so the
# value does not appear in `ps` output). Press Enter without typing to run
# without a token. Works with macOS bash 3.2.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -t 0 ]]; then
  echo "Run this from an interactive terminal (it needs to prompt for the token)." >&2
  exit 1
fi

printf "Hugging Face token (input hidden, Enter to skip): "
IFS= read -rs TOKEN
printf "\n"

if [[ -n "${TOKEN}" && "${TOKEN}" != hf_* ]]; then
  echo "That doesn't look like a Hugging Face token (they start with hf_). Aborting." >&2
  unset TOKEN
  exit 1
fi

cleanup() { unset TOKEN HF_TOKEN 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [[ -n "${TOKEN}" ]]; then
  echo "Token received (${#TOKEN} characters). Starting the run..."
  HF_TOKEN="${TOKEN}" bash "${HERE}/run_all_languages.sh"
else
  echo "No token. Starting the run without one (may hit Hugging Face rate limits)..."
  bash "${HERE}/run_all_languages.sh"
fi
