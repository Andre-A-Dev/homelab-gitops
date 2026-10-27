#!/usr/bin/env bash
# deploy-prompt.sh — copy prompt.sh to all Pis and hook it into ~/.bashrc
# Idempotent: re-running only updates ~/.prompt.sh, the .bashrc hook is added once.
# Run from the repo root: ./scripts/deploy-prompt.sh [host ...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/prompt.sh"
USER_NAME="andre"
HOSTS=("$@")
(( ${#HOSTS[@]} == 0 )) && HOSTS=(mnemosyne boreas hephaestus zephyros)

failed=()
for h in "${HOSTS[@]}"; do
  echo "==> ${h}"
  if ! scp -q "$SRC" "${USER_NAME}@${h}:~/.prompt.sh"; then
    echo "    copy failed" >&2
    failed+=("$h"); continue
  fi
  # Append hook only if not present yet; remote script via stdin avoids quoting issues
  if ! ssh "${USER_NAME}@${h}" 'bash -s' <<'REMOTE'
if grep -qF '.prompt.sh' ~/.bashrc; then
  echo "    hook exists"
else
  printf '\n%s\n' '[ -f ~/.prompt.sh ] && . ~/.prompt.sh  # host-specific prompt (homelab-infra/scripts/prompt.sh)' >> ~/.bashrc
  echo "    hook added"
fi

# Login shells read only the FIRST existing file of .bash_profile/.bash_login/.profile.
# If that file doesn't load .bashrc, the hook above never runs on SSH login.
for f in ~/.bash_profile ~/.bash_login; do
  if [ -e "$f" ]; then
    if ! grep -q 'bashrc' "$f"; then
      echo "    ERROR: $f exists but does not source ~/.bashrc - prompt won't load on login" >&2
      exit 2
    fi
    break
  fi
done
REMOTE
  then
    echo "    remote check failed" >&2
    failed+=("$h")
  fi
done

if (( ${#failed[@]} > 0 )); then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "Done. Open a new SSH session to see the prompt."
