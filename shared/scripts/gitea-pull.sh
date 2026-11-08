#!/bin/bash
# Cron fallback for the Gitea webhook. Fetches, and when upstream has new
# commits runs gitea-webhook-handler.sh, which pulls and applies the host
# actions. A plain pull here would move HEAD without applying anything, and
# the next webhook would then find no change to apply.
# Runs unattended via cron — requires SSH key auth (no passphrase).

REPO_DIR="$HOME/homelab-infra"
LOG_TAG="gitea-pull"
HANDLER="$REPO_DIR/shared/scripts/gitea-webhook-handler.sh"

cd "$REPO_DIR" || { logger -t "$LOG_TAG" "ERROR: repo dir not found: $REPO_DIR"; exit 1; }

if ! RESULT=$(git fetch --quiet 2>&1); then
    logger -t "$LOG_TAG" "ERROR: git fetch failed: $RESULT"
    exit 1
fi

LOCAL=$(git rev-parse --short HEAD)
UPSTREAM=$(git rev-parse --short '@{u}')
if [ "$LOCAL" = "$UPSTREAM" ]; then
    exit 0 # Nothing to log — no noise for the common case
fi

logger -t "$LOG_TAG" "INFO: upstream at $UPSTREAM, local at $LOCAL - running the webhook handler"
# The handler writes its own log (/var/log/webhook-handler.log) and copies it
# to stdout; that copy is dropped so cron has nothing to mail. stderr stays.
if "$HANDLER" cron > /dev/null; then
    logger -t "$LOG_TAG" "INFO: handler finished, now at $(git rev-parse --short HEAD)"
else
    logger -t "$LOG_TAG" "ERROR: handler failed (exit $?), see /var/log/webhook-handler.log"
    exit 1
fi
