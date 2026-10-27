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

log "INFO: Webhook received — starting pull"

cd "$REPO_DIR"

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
    if echo "$CHANGED" | grep -q '^boreas/'; then
        if echo "$CHANGED" | grep -q 'pihole6-exporter.service'; then
            log "INFO: Reloading pihole6-exporter"
            sudo systemctl daemon-reload
            sudo systemctl restart pihole6-exporter \
              && log "INFO: pihole6-exporter restarted" \
              || log "ERROR: pihole6-exporter restart failed"
        fi
    fi
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
