#!/bin/sh
# Send a CI result to ntfy.
#
# Usage: notify.sh <success|failure> <title>
#
# The topic is read from NTFY_TOPIC and never hardcoded. ntfy.sh has no
# authentication: anyone who knows a topic name can read it AND publish to it,
# so a topic committed to a repository that gets mirrored to GitHub is a topic
# a stranger can post fake "validation passed" messages to. It belongs in a
# CI secret (Gitea Actions, GitHub Actions).
#
# Used by validate.yml in .gitea/ and .github/ (env-drift.yml was merged into
# it on 2026-10-23).
# Being a .sh file, it is covered by this repo's own shellcheck and
# executable-bit gates.

set -eu

STATUS="${1:?usage: notify.sh <success|failure> <title>}"
TITLE="${2:?usage: notify.sh <success|failure> <title>}"

if [ -z "${NTFY_TOPIC:-}" ]; then
    # Deliberately not fatal: a missing notification secret should not turn a
    # green pipeline red. But it must be visible in the log, because a
    # notification channel that quietly stopped working looks exactly like a
    # pipeline that never failed again.
    echo "! NTFY_TOPIC is empty - no notification sent"
    exit 0
fi

case "$STATUS" in
    success) PRIORITY="low";  TAGS="white_check_mark" ;;
    failure) PRIORITY="high"; TAGS="warning" ;;
    *) echo "x unknown status '$STATUS'" >&2; exit 2 ;;
esac

# ${VAR:0:7} is a bashism. The runner image is Alpine, so a step can end up in
# busybox ash where that expansion is not portable - the notification then
# carries an empty commit or fails outright.
SHORT_SHA=$(echo "${GITHUB_SHA:-unknown}" | cut -c1-7)
BRANCH="${GITHUB_REF_NAME:-unknown}"

# --fail-with-body and -S instead of a bare -s: a failed notification used to
# produce no output whatsoever. || true keeps a delivery problem from masking
# the actual job result.
curl -sS --fail-with-body --max-time 10 \
    -H "Title: ${TITLE}" \
    -H "Priority: ${PRIORITY}" \
    -H "Tags: ${TAGS}" \
    -d "Branch: ${BRANCH} - Commit: ${SHORT_SHA}" \
    "https://ntfy.sh/${NTFY_TOPIC}" \
    || echo "! ntfy delivery failed - read the run log directly"
