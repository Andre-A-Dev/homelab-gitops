#!/bin/bash
# =============================================================================
# deploy-handler.sh - Mnemosyne deploy, triggered by the Gitea Actions job
# "deploy" (needs: validate) via webhook hook "mnemosyne-deploy".
#
# Contract:
#   $1      full commit SHA that passed validation
#   stdout  returned verbatim as the HTTP response body (synchronous hook)
#   exit    0 = ok/noop, non-zero = webhook answers HTTP 500 -> red job + ntfy
#
# Replaces the handler lost in f4b0322 (2026-06-16). Between then and
# 2026-10-13 every push to Mnemosyne was pull-only: the shared handler logged
# "Unknown host" as a WARNING, exited 0, and Gitea showed a green tick.
# Design rules derived from that:
#   - nothing goes to /dev/null; every docker error lands in the response
#   - a deploy is only "ok" after the touched containers are verified running
#   - anything needing a human is reported as manual=..., never just skipped
#
# Checkout is fast-forwarded to exactly the validated SHA, never past it, so
# an unvalidated newer commit cannot ride along with an older green run.
#
# Not handled (reported as manual): gitea, gitea-runner, systemd units.
# =============================================================================

set -euo pipefail

REPO="/home/andre/homelab-infra"
STACKS_DIR="/home/andre/stacks"
PLAN="$REPO/mnemosyne/scripts/deploy_plan.py"
LOCK="/run/lock/mnemosyne-deploy.lock"
# Long enough for a crash-looping container to show "restarting".
VERIFY_WAIT=20

log() { printf 'time="%s" level=%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "${*:2}"; }
die() { log error "$@"; log error result=failed; exit 1; }
join() { local IFS=,; echo "$*"; }

# Any command that aborts the script via errexit still ends with a parseable
# result line - an unexplained HTTP 500 is the next-worst thing to silence.
trap 'log error event=unexpected_exit line=$LINENO cmd="$BASH_COMMAND"; log error result=failed' ERR

TARGET="${1:-}"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || die event=bad_request msg="expected full commit SHA, got '${TARGET}'"
SHORT="${TARGET:0:7}"

# Two pushes in quick succession must not interleave their checkouts.
exec 9>"$LOCK"
flock -w 600 9 || die event=lock_timeout sha="$SHORT"

cd "$REPO"
git fetch --quiet origin main || die event=fetch_failed sha="$SHORT"

if ! git merge-base --is-ancestor "$TARGET" origin/main; then
    die event=sha_not_on_main sha="$SHORT"
fi

# Already at or past the target: a later green run deployed it, or this is a
# re-run. Nothing to do - and that is a success, not a skip.
if git merge-base --is-ancestor "$TARGET" HEAD; then
    log info event=noop sha="$SHORT" head="$(git rev-parse --short HEAD)"
    log info result=noop sha="$SHORT"
    exit 0
fi

BEFORE=$(git rev-parse HEAD)
git merge --ff-only --quiet "$TARGET" \
    || die event=ff_failed sha="$SHORT" msg="local checkout diverged from main - inspect by hand"
log info event=checkout sha="$SHORT" before="${BEFORE:0:7}"

CHANGED=$(git diff --name-only "$BEFORE" "$TARGET")

DEPLOYED=()
MANUAL=()
FAILED=()

if grep -q '^mnemosyne/systemd/' <<< "$CHANGED"; then
    log warn event=manual_required what=systemd_units \
        msg="install changed units by hand, then daemon-reload"
    MANUAL+=("systemd")
fi

PLAN_OUT=$(python3 "$PLAN" <<< "$CHANGED") || {
    # The plan still lists every stack it could resolve; config-error lines
    # mark the ones it could not. Continue so the rest is deployed and reported.
    log error event=plan_partial sha="$SHORT"
}

# errexit is suspended inside functions called from an `if` or `||` context -
# including their subshells. Every docker call below is therefore chained
# explicitly; relying on set -e here would silently ignore failures.
# Return codes: 0 deployed, 1 failed, 3 inactive but containers still exist,
# 4 inactive and clean (nothing to run).
deploy_stack() {
    local stack="$1" compose="$2" recreate="$3" build="$4"
    [ "$recreate" = "-" ] && recreate=""
    [ "$build" = "-" ] && build=""

    (
        cd "$STACKS_DIR/$stack" || exit 1

        # Every service behind an inactive profile: `up -d` would fail with
        # "no service selected" (deploy 05dd2fd, 2026-10-15). That is a state,
        # not an error. A profile does not stop a running container, though -
        # leftovers are reported, never removed automatically.
        local services project leftovers
        services=$(docker compose config --services) || exit 1
        if [ -z "$services" ]; then
            project=$(docker compose config --format json \
                      | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])') || exit 1
            leftovers=$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}}' \
                        | paste -sd, -)
            if [ -n "$leftovers" ]; then
                log warn event=stack_inactive stack="$stack" leftovers="$leftovers" \
                    msg="all services behind a profile, containers still exist - docker compose rm -sf"
                exit 3
            fi
            log info event=stack_inactive stack="$stack"
            exit 4
        fi

        if [ "$compose" = "1" ]; then
            log info event=compose_up stack="$stack"
            docker compose up -d 2>&1 || exit 1
        fi
        if [ -n "$build" ]; then
            log info event=build stack="$stack" services="${build// /,}"
            # shellcheck disable=SC2086 # service list is word-split on purpose
            docker compose build $build 2>&1 || exit 1
        fi
        if [ -n "$recreate" ]; then
            log info event=force_recreate stack="$stack" services="${recreate// /,}"
            # shellcheck disable=SC2086 # service list is word-split on purpose
            docker compose up -d --force-recreate $recreate 2>&1 || exit 1
        fi
    )
}

# Flags containers that crash-loop, exited non-zero, or report unhealthy.
# "health: starting" passes - the healthcheck simply has not run yet.
verify_stack() {
    local stack="$1" bad
    bad=$(cd "$STACKS_DIR/$stack" \
          && docker compose ps -a --format '{{.Name}} {{.State}} {{.ExitCode}} {{.Health}}' \
          | awk '$2=="restarting" || ($2=="exited" && $3!="0") || $4=="unhealthy"') || return 1
    if [ -n "$bad" ]; then
        log error event=verify_failed stack="$stack" containers="$(awk '{print $1}' <<< "$bad" | paste -sd, -)"
        return 1
    fi
    log info event=verified stack="$stack"
}

while IFS=$'\t' read -r stack compose recreate build note unmapped; do
    [ -n "$stack" ] || continue
    # Changed files that no running service mounts or builds from never reach
    # a container (a script used by a systemd timer, a config whose mount was
    # removed). Reported independently of the note: when the compose file
    # changed too, the deploy would otherwise look complete while part of the
    # commit went nowhere.
    if [ -n "$unmapped" ] && [ "$unmapped" != "-" ]; then
        log warn event=unmapped_files stack="$stack" files="$unmapped" \
            msg="changed, but no running service mounts or builds from them"
        MANUAL+=("$stack:unmapped")
    fi
    case "$note" in
        manual|no-compose)
            log warn event=manual_required stack="$stack" reason="$note"
            MANUAL+=("$stack")
            ;;
        config-error)
            log error event=config_error stack="$stack"
            FAILED+=("$stack")
            ;;
        no-service-affected)
            log info event=nothing_to_restart stack="$stack"
            ;;
        deploy)
            # `|| rc=$?` keeps errexit and the ERR trap out of this branch.
            rc=0
            deploy_stack "$stack" "$compose" "$recreate" "$build" || rc=$?
            case "$rc" in
                0) DEPLOYED+=("$stack") ;;
                3) MANUAL+=("$stack:inactive-leftovers") ;;
                4) ;;   # inactive and clean - nothing to deploy, nothing to verify
                *) log error event=deploy_failed stack="$stack"
                   FAILED+=("$stack") ;;
            esac
            ;;
        *)
            log error event=unknown_plan_note stack="$stack" note="$note"
            FAILED+=("$stack")
            ;;
    esac
done <<< "$PLAN_OUT"

if [ ${#DEPLOYED[@]} -gt 0 ]; then
    sleep "$VERIFY_WAIT"
    for stack in "${DEPLOYED[@]}"; do
        verify_stack "$stack" || FAILED+=("$stack")
    done
fi

RESULT=ok LEVEL=info
[ ${#FAILED[@]} -eq 0 ] || { RESULT=failed; LEVEL=error; }

# Single summary line - the Actions job parses manual="..." from it.
log "$LEVEL" result="$RESULT" sha="$SHORT" \
    applied="\"$(join "${DEPLOYED[@]}")\"" \
    manual="\"$(join "${MANUAL[@]}")\"" \
    failed="\"$(join "${FAILED[@]}")\""

# `|| exit` keeps the ERR trap from printing a second, misleading result line.
[ "$RESULT" = ok ] || exit 1
