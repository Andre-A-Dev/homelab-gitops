#!/bin/bash
# Shared webhook handler — runs on Boreas, Hephaestus, Zephyros, and Daidalos.
# Pulls latest changes from Gitea and executes host-specific actions.
# Mnemosyne uses its own handler at mnemosyne/scripts/gitea-webhook-handler.sh

set -euo pipefail

REPO_DIR="$HOME/homelab-infra"
LOG_FILE="/var/log/webhook-handler.log"
HOST=$(hostname | tr '[:upper:]' '[:lower:]')

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$HOST] $*" | tee -a "$LOG_FILE"
}

# No argument: called by the webhook. "cron": called by gitea-pull.sh.
if [ "${1:-}" = "cron" ]; then
    log "INFO: Cron fallback — starting pull"
else
    log "INFO: Webhook received — starting pull"
fi

cd "$REPO_DIR"

# Webhook and cron fallback can fire at the same time: serialize them, so a
# change is pulled and applied once.
exec 9> "$REPO_DIR/.git/webhook-handler.lock"
flock 9

BEFORE=$(git rev-parse HEAD)
git pull --rebase >> "$LOG_FILE" 2>&1
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    log "INFO: Already up to date — nothing to do"
    exit 0
fi

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
log "INFO: Changed files:"
echo "$CHANGED" | while read -r f; do log "  $f"; done

case "$HOST" in

  boreas)
    # Nothing to apply: the pihole6-exporter unit is host-local and its
    # drop-in (boreas/systemd/) is copied by hand (boreas/README.md). Every
    # change under boreas/ is a manual step.
    ;;

  hephaestus)
    if echo "$CHANGED" | grep -q '^hephaestus/'; then
        if echo "$CHANGED" | grep -q 'vcontrold.service'; then
            log "INFO: Reloading vcontrold"
            sudo systemctl daemon-reload
            sudo systemctl restart vcontrold \
              && log "INFO: vcontrold restarted" \
              || log "ERROR: vcontrold restart failed"
        fi
    fi
    ;;

  zephyros)
    # Caddyfile is a single-file bind mount: git replaces it atomically, so a
    # running container keeps the old inode and `caddy reload` reloads the old
    # file. Only recreating the container picks up the new one.
    # No CI validates this Caddyfile, so validate first in a one-off container
    # (sees the new file) - a broken config must not replace a working Caddy.
    if echo "$CHANGED" | grep -q '^zephyros/caddy/'; then
        log "INFO: Validating Caddyfile"
        if (cd "$REPO_DIR/zephyros/caddy" \
              && docker compose run --rm --no-deps -T caddy \
                   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile) \
              >> "$LOG_FILE" 2>&1; then
            log "INFO: Recreating Caddy"
            (cd "$REPO_DIR/zephyros/caddy" && docker compose up -d --force-recreate) \
              >> "$LOG_FILE" 2>&1 \
              && log "INFO: Caddy recreated" \
              || log "ERROR: Caddy recreate failed"
        else
            log "ERROR: Caddyfile invalid - running Caddy left untouched"
        fi
    fi
    # Exporter stack: up -d recreates only services whose definition changed.
    if echo "$CHANGED" | grep -q '^zephyros/monitoring/'; then
        log "INFO: Applying monitoring stack"
        (cd "$REPO_DIR/zephyros/monitoring" && docker compose up -d) \
          >> "$LOG_FILE" 2>&1 \
          && log "INFO: Monitoring stack applied" \
          || log "ERROR: Monitoring stack apply failed"
    fi
    ;;

  daidalos)
    # Nothing to apply: the shared files used here (apticron.conf,
    # system-update) are symlinks into the checkout, so the pull is the
    # deploy. clusters/daidalos/ is reconciled by Flux, not by this handler.
    ;;

  *)
    log "ERROR: no deploy logic for host '$HOST' - pulled, nothing restarted"
    exit 1
    ;;

esac

log "INFO: Done"
