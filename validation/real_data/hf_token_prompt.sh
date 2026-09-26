# Sourced by the PCCF Docker runners. Asks for a Hugging Face token at a hidden
# prompt (Enter to skip) and sets HF_ENV to "-e HF_TOKEN" for `docker run`.
# The token is never echoed, written to disk or put on a command line.
HF_ENV=""
if [[ -z "${HF_TOKEN:-}" && -t 0 ]]; then
  printf "Hugging Face token (input hidden, Enter to skip): "
  IFS= read -rs HF_TOKEN_IN || true
  printf "\n"
  if [[ -n "${HF_TOKEN_IN}" ]]; then
    if [[ "${HF_TOKEN_IN}" != hf_* ]]; then echo "Not a Hugging Face token (must start with hf_). Aborting." >&2; exit 1; fi
    export HF_TOKEN="${HF_TOKEN_IN}"
  fi
  unset HF_TOKEN_IN
fi
if [[ -n "${HF_TOKEN:-}" ]]; then HF_ENV="-e HF_TOKEN"; echo "Using a Hugging Face token (${#HF_TOKEN} characters)."; fi
trap 'unset HF_TOKEN' EXIT
