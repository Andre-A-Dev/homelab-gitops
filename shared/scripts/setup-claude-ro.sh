#!/usr/bin/env bash
#
# setup-claude-ro.sh — Provision a read-only SSH user for Claude Code
#
# Usage  : sudo ./setup-claude-ro.sh [--keys <file>]
#          sudo ./setup-claude-ro.sh --remove
#          add --dry-run to print the actions without changing anything
#
# Keys   : one line per workstation, default shared/ssh/claude-ro.keys next to
#          this script's repo checkout. Format:
#            from="<tailscale-ip>/32" ssh-ed25519 AAAA... claude-ro@<workstation>
#
# Idempotent: safe to re-run. Re-running replaces authorized_keys with the
# current keys file, so adding or revoking a workstation = edit file, re-run.
#
# Why: Claude Code runs commands as the local user and inherits its SSH keys.
# Verification against live hosts only needs read access, so it gets its own
# user whose blast radius is limited by the host, not by prompt rules.
#
# What the user can do:
#   - read the systemd journal (systemd-journal group) and /var/log (adm group)
#   - read world-readable files (repo checkouts, configs)
#   - sudo without password, but ONLY: docker ps, docker logs
#
# What it deliberately cannot do:
#   - docker group membership (docker socket = root)
#   - docker inspect / exec / compose (inspect prints env vars = secrets)
#   - password login, PTY, agent/port/X11 forwarding (authorized_keys "restrict")
#   - logins with a key from any address other than its from= list

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RO_USER="claude-ro"
SUDOERS_FILE="/etc/sudoers.d/90-claude-ro"

KEYS_FILE="$(dirname "$(readlink -f "$0")")/../ssh/claude-ro.keys"
REMOVE=0
DRY_RUN=0

log()  { echo -e "\033[1;32m[claude-ro]\033[0m $*"; }
warn() { echo -e "\033[1;33m[claude-ro]\033[0m $*"; }
run()  { if [[ $DRY_RUN -eq 1 ]]; then echo "  would run: $*"; else "$@"; fi; }

usage() {
    sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keys)    KEYS_FILE="${2:-}"; shift 2 ;;
        --remove)  REMOVE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
    echo "Run as root: sudo $0 ..." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------
if [[ $REMOVE -eq 1 ]]; then
    if [[ -f "$SUDOERS_FILE" ]]; then
        log "Removing $SUDOERS_FILE..."
        run rm -f "$SUDOERS_FILE"
    fi
    if id "$RO_USER" &>/dev/null; then
        log "Removing user '$RO_USER' and its home..."
        run userdel --remove "$RO_USER"
    else
        log "User '$RO_USER' does not exist."
    fi
    exit 0
fi

if [[ ! -r "$KEYS_FILE" ]]; then
    echo "Keys file not readable: $KEYS_FILE (pass --keys <file>)" >&2
    exit 1
fi

# Only from= is accepted as option: restrict is added below, and anything
# else (command=, permitopen=, ...) would widen or bypass it
KEY_RE='^from="[0-9a-fA-F.:/,]+" (ssh-ed25519|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+ [^ ]+$'
AUTH_LINES=()
lineno=0
while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ ! "$line" =~ $KEY_RE ]]; then
        echo "$KEYS_FILE:$lineno: expected 'from=\"<cidr>[,...]\" ssh-ed25519 <key> <comment>'" >&2
        exit 1
    fi
    AUTH_LINES+=("restrict,$line")
done < "$KEYS_FILE"
if [[ ${#AUTH_LINES[@]} -eq 0 ]]; then
    echo "No keys in $KEYS_FILE." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. User (no password, not in sudo/docker)
# ---------------------------------------------------------------------------
if id "$RO_USER" &>/dev/null; then
    log "User '$RO_USER' already exists."
else
    log "Creating user '$RO_USER'..."
    run useradd --create-home --shell /bin/bash --comment "Claude Code read-only" "$RO_USER"
fi
# useradd leaves the password locked; enforce it for pre-existing users too
run passwd --lock "$RO_USER"

for grp in docker sudo; do
    if id -nG "$RO_USER" 2>/dev/null | grep -qw "$grp"; then
        warn "Removing '$RO_USER' from group '$grp'..."
        run gpasswd --delete "$RO_USER" "$grp"
    fi
done

# ---------------------------------------------------------------------------
# 2. Log read access
# ---------------------------------------------------------------------------
for grp in systemd-journal adm; do
    if getent group "$grp" >/dev/null; then
        log "Adding '$RO_USER' to group '$grp'..."
        run usermod -aG "$grp" "$RO_USER"
    else
        warn "Group '$grp' not found, skipped."
    fi
done

# ---------------------------------------------------------------------------
# 3. authorized_keys (restricted)
# ---------------------------------------------------------------------------
if id "$RO_USER" &>/dev/null; then
    RO_HOME="$(getent passwd "$RO_USER" | cut -d: -f6)"
else
    RO_HOME="/home/$RO_USER"   # dry run before the user exists
fi
log "Writing $RO_HOME/.ssh/authorized_keys (${#AUTH_LINES[@]} key(s) from $KEYS_FILE)..."
run install -d -m 700 -o "$RO_USER" -g "$RO_USER" "$RO_HOME/.ssh"
if [[ $DRY_RUN -eq 1 ]]; then
    for line in "${AUTH_LINES[@]}"; do echo "  would write: $line"; done
else
    printf '%s\n' "${AUTH_LINES[@]}" > "$RO_HOME/.ssh/authorized_keys"
    chown "$RO_USER":"$RO_USER" "$RO_HOME/.ssh/authorized_keys"
    chmod 600 "$RO_HOME/.ssh/authorized_keys"
fi

# ---------------------------------------------------------------------------
# 4. sudoers whitelist: docker ps / docker logs only
# ---------------------------------------------------------------------------
DOCKER_BIN="$(command -v docker || true)"
if [[ -z "$DOCKER_BIN" ]]; then
    log "No docker on this host, no sudoers drop-in needed."
    [[ -f "$SUDOERS_FILE" ]] && run rm -f "$SUDOERS_FILE"
else
    log "Writing $SUDOERS_FILE for $DOCKER_BIN ps/logs..."
    SUDOERS_CONTENT="# Managed by setup-claude-ro.sh — read-only docker for Claude Code.
# No inspect (prints env = secrets), no exec, no compose.
Cmnd_Alias CLAUDE_RO_DOCKER = $DOCKER_BIN ps, $DOCKER_BIN ps *, $DOCKER_BIN logs *
$RO_USER ALL=(root) NOPASSWD: CLAUDE_RO_DOCKER"
    if [[ $DRY_RUN -eq 1 ]]; then
        while IFS= read -r line; do echo "  would write: $line"; done <<< "$SUDOERS_CONTENT"
    else
        tmp="$(mktemp)"
        echo "$SUDOERS_CONTENT" > "$tmp"
        # Never install a broken sudoers file: it can lock out sudo entirely
        visudo -cf "$tmp"
        install -m 440 -o root -g root "$tmp" "$SUDOERS_FILE"
        rm -f "$tmp"
    fi
fi

# ---------------------------------------------------------------------------
# 5. sshd access filters
# ---------------------------------------------------------------------------
if command -v sshd >/dev/null && [[ $DRY_RUN -eq 0 ]]; then
    # No match is the normal case (no filters), so grep's exit 1 is expected
    filters="$(sshd -T | grep -iE '^(allowusers|allowgroups)' || true)"
    if [[ -n "$filters" ]]; then
        warn "sshd has access filters, '$RO_USER' may need to be added:"
        echo "$filters"
    fi
fi

log "Done. Test from each workstation: ssh <host>-ro 'id; sudo -n docker ps --format {{.Names}} | head -3'"
