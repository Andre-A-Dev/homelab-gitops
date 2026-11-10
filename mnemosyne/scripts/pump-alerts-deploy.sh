#!/bin/bash
# Deploys pump-alerts.yml from Gitea to Prometheus.
# Validates rules with promtool before deploying — aborts on invalid YAML.
# Output goes to stdout: webhook logs it, see `journalctl -u webhook`.
# An aborted deploy also posts to the pump warning ntfy topic: the webhook
# answers Gitea before this script runs, so nobody would see the failure.

ENV_FILE="/home/<user>/stacks/monitoring/.env"
GITEA_TOKEN=$(grep PUMP_GITEA_TOKEN "$ENV_FILE" | cut -d= -f2)
NTFY_TOPIC=$(grep '^NTFY_PUMP_WARNING=' "$ENV_FILE" | cut -d= -f2)
REPO_URL="https://git.home/<user>/pump_alerts"
OUTFILE="/home/<user>/stacks/monitoring/prometheus/pump-alerts.yml"
TMPFILE="/tmp/pump-alerts-check.yml"

# Fresh shallow clone per run: no stale checkout to deploy from when a pull
# fails, and nothing left on disk afterwards.
REPO_DIR=$(mktemp -d)
trap 'rm -rf "$REPO_DIR"' EXIT

# $1: log line, $2: what the household reads on ntfy
fail() {
    echo "$1"
    if [ -n "$NTFY_TOPIC" ]; then
        curl -sS --max-time 10 -H "Title: Pump alert change not applied" \
            -d "$2 The previous pump alerts stay active." \
            "https://ntfy.sh/$NTFY_TOPIC" > /dev/null \
            || echo "ntfy notification failed"
    else
        echo "NTFY_PUMP_WARNING not set — no notification sent"
    fi
    echo "=== pump-alerts deploy aborted: $(date) ==="
    exit 1
}

echo "=== pump-alerts deploy: $(date) ==="

# The token travels as an HTTP header set through the environment, so it
# lands neither in the clone's .git/config nor in the process list.
AUTH=$(printf '<user>:%s' "$GITEA_TOKEN" | base64 -w0)
if ! GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
     GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH" \
     git clone --quiet --depth 1 "$REPO_URL" "$REPO_DIR" 2>&1; then
    fail "CLONE FAILED — aborting deploy, keeping current pump-alerts.yml" \
         "The home server could not fetch the pump_alerts repository. Please tell the person who looks after the home server."
fi

# Validate before deploying — copy to temp location inside Prometheus container.
# A failed docker cp would leave the previous run's file there for promtool.
cp "$REPO_DIR/alerts.yml" "$TMPFILE"
if ! docker cp "$TMPFILE" prometheus:/tmp/pump-alerts-check.yml 2>&1; then
    fail "DOCKER CP FAILED — aborting deploy, keeping current pump-alerts.yml" \
         "The home server could not check the new alerts.yml. Please tell the person who looks after the home server."
fi

if ! docker exec prometheus promtool check rules /tmp/pump-alerts-check.yml 2>&1; then
    fail "VALIDATION FAILED — aborting deploy, keeping current pump-alerts.yml" \
         "The latest change to alerts.yml contains an error. Please undo or fix the last change."
fi

echo "Validation passed"

# Deploy validated file
if ! cp "$REPO_DIR/alerts.yml" "$OUTFILE"; then
    fail "COPY FAILED — pump-alerts.yml not updated" \
         "The home server could not save the new alerts.yml. Please tell the person who looks after the home server."
fi
echo "Copied pump-alerts.yml"

# Reload Prometheus. wget exits non-zero on an HTTP error status; a failed
# reload keeps the running rules in memory, but the new file is already on
# disk and is loaded on the next Prometheus start.
if ! docker exec prometheus wget -qO- --post-data="" http://localhost:9090/-/reload 2>&1; then
    fail "RELOAD FAILED — pump-alerts.yml copied, Prometheus kept its running config" \
         "The home server could not load the new alerts.yml. Please tell the person who looks after the home server."
fi
echo "Prometheus reloaded"
echo "=== pump-alerts deploy finished: $(date) ==="
