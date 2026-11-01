#!/usr/bin/env bash
# deploy-prompt.sh — copy prompt.sh to all servers and hook it into ~/.bashrc
# of the login user and of root (sudo -i / sudo -s read /root/.bashrc).
# Idempotent: re-running only updates the .prompt.sh copies, each .bashrc hook
# is added once. sudo asks for the password once per host.
# Run from the repo root: ./scripts/deploy-prompt.sh [host ...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/prompt.sh"
USER_NAME="<user>"
HOSTS=("$@")
(( ${#HOSTS[@]} == 0 )) && HOSTS=(mnemosyne boreas hephaestus zephyros daidalos)

# Runs on the host, once as the user and once as root (~ = that account's home)
HOOK_SCRIPT=$(cat <<'REMOTE'
if grep -qF '.prompt.sh' ~/.bashrc; then
  echo "    $(id -un): hook exists"
else
  printf '\n%s\n' '[ -f ~/.prompt.sh ] && . ~/.prompt.sh  # host-specific prompt (homelab-infra/scripts/prompt.sh)' >> ~/.bashrc
  echo "    $(id -un): hook added"
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
)

# root gets its own root-owned copy: root never sources a file the user can write
ROOT_SCRIPT="install -m 0644 -o root -g root ~${USER_NAME}/.prompt.sh /root/.prompt.sh || exit 1
${HOOK_SCRIPT}"

failed=()
for h in "${HOSTS[@]}"; do
  echo "==> ${h}"
  if ! scp -q "$SRC" "${USER_NAME}@${h}:~/.prompt.sh"; then
    echo "    copy failed" >&2
    failed+=("$h"); continue
  fi
  # One connection with a TTY for the sudo password; %q quotes the scripts for
  # the remote bash (login shell on all servers)
  if ! ssh -t "${USER_NAME}@${h}" \
      "bash -c $(printf '%q' "$HOOK_SCRIPT") && sudo bash -c $(printf '%q' "$ROOT_SCRIPT")"; then
    echo "    remote check failed" >&2
    failed+=("$h")
  fi
done

if (( ${#failed[@]} > 0 )); then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "Done. Open a new SSH session to see the prompt."
