#!/bin/bash
# Deploys pump-alerts.yml from Gitea to Prometheus.
# Validates rules with promtool before deploying — aborts on invalid YAML.
# Output goes to stdout: webhook logs it, see `journalctl -u webhook`.

GITEA_TOKEN=$(grep PUMP_GITEA_TOKEN "/home/<user>/stacks/monitoring/.env" | cut -d= -f2)
REPO_URL="https://git.home/<user>/pump_alerts"
OUTFILE="/home/<user>/stacks/monitoring/prometheus/pump-alerts.yml"
TMPFILE="/tmp/pump-alerts-check.yml"

# Fresh shallow clone per run: no stale checkout to deploy from when a pull
# fails, and nothing left on disk afterwards.
REPO_DIR=$(mktemp -d)
trap 'rm -rf "$REPO_DIR"' EXIT

echo "=== pump-alerts deploy: $(date) ==="

# The token travels as an HTTP header set through the environment, so it
# lands neither in the clone's .git/config nor in the process list.
AUTH=$(printf '<user>:%s' "$GITEA_TOKEN" | base64 -w0)
if ! GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
     GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH" \
     git clone --quiet --depth 1 "$REPO_URL" "$REPO_DIR" 2>&1; then
    echo "CLONE FAILED — aborting deploy, keeping current pump-alerts.yml"
    echo "=== pump-alerts deploy aborted: $(date) ==="
    exit 1
fi

# Validate before deploying — copy to temp location inside Prometheus container
cp "$REPO_DIR/alerts.yml" "$TMPFILE"
docker cp "$TMPFILE" prometheus:/tmp/pump-alerts-check.yml 2>&1

if ! docker exec prometheus promtool check rules /tmp/pump-alerts-check.yml 2>&1; then
    echo "VALIDATION FAILED — aborting deploy, keeping current pump-alerts.yml"
    echo "=== pump-alerts deploy aborted: $(date) ==="
    exit 1
fi

echo "Validation passed"

# Deploy validated file
cp "$REPO_DIR/alerts.yml" "$OUTFILE" && echo "Copied pump-alerts.yml"

# Reload Prometheus
docker exec prometheus wget -qO- --post-data="" http://localhost:9090/-/reload 2>&1
echo "Prometheus reloaded"
echo "=== pump-alerts deploy finished: $(date) ==="