#!/usr/bin/env bash
# =============================================================================
# update-hosts.sh — Guided system-update run over all servers
# =============================================================================
# Runs `system-update` (shared/scripts/system-update.sh) on every server via
# SSH, one host at a time, in the order of wiki/Update-Strategy.md ("System
# updates"). Started by hand in the update window, never unattended. Per host:
#   1. system-update --dry-run, then ask: update / skip / quit
#   2. system-update
#   3. if the host reports a required reboot: ask, reboot, wait until it is
#      back with a new boot ID and systemd has finished booting
#   4. a non-zero exit code, failed systemd units or a failed reboot stop the
#      run unless you confirm to continue (default: no)
#
# Writes a Markdown report on exit, also after an abort or Ctrl-C:
#   $UPDATE_REPORT_DIR/System-Update_<YYYY-MM-DD_HHMM>.md   (default: $HOME)
# Package lists come from /var/log/apt/history.log on each host.
#
# Usage (from a workstation, with the admin SSH aliases):
#   ./scripts/update-hosts.sh                  # all servers
#   ./scripts/update-hosts.sh --from boreas    # resume after an abort
#   ./scripts/update-hosts.sh --help
# =============================================================================

set -euo pipefail

# Order and the reason for each position: wiki/Update-Strategy.md. Keep in sync.
HOSTS=(daidalos hephaestus boreas zephyros mnemosyne)

REPORT_DIR="${UPDATE_REPORT_DIR:-$HOME}"
REBOOT_TIMEOUT=600   # seconds a rebooting host has to come back

BOLD="\e[1m"
RESET="\e[0m"
RED="\e[31m"
GREEN="\e[32m"
YELLOW="\e[33m"
CYAN="\e[36m"

# ── Report state ──────────────────────────────────────────────────────────────

RUN_STAMP=""                   # set when the first host starts; empty = no report
RUN_START=""
ABORT_REASON="interrupted or script error"
RUN_COMPLETE=false
declare -A ROW                 # host → summary table row
SECTIONS=""                    # per-host details, Markdown

# ── Helpers ───────────────────────────────────────────────────────────────────

usage() {
    cat << EOF
Usage: $(basename "$0") [--from HOST]

Runs system-update on: ${HOSTS[*]}
  --from HOST   start at HOST, skipping the hosts before it
  --help        show this help

Report directory: \$UPDATE_REPORT_DIR (now: ${REPORT_DIR})
EOF
}

ok()    { echo -e "${GREEN}✔  $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠  $*${RESET}"; }
error() { echo -e "${RED}✖  $*${RESET}"; }

# confirm <question> — true only on an explicit "y"
confirm() {
    local answer
    read -r -p "$1 [y/N] " answer
    [[ "$answer" == [yY] ]]
}

# remote <host> <command> — non-interactive; BatchMode fails instead of prompting
remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" "$2"
}

# failed_units <host> — comma-separated list, empty if none
failed_units() {
    remote "$1" 'systemctl list-units --state=failed --no-legend --plain' \
        | awk '{ printf "%s%s", sep, $1; sep = ", " }'
}

# apt_history_since <host> <start> — the history.log action lines of entries
# that started at or after <start>. <start> comes from the host's own clock in
# history.log format ("2026-11-01  08:01:12"), so clock skew does not matter.
apt_history_since() {
    remote "$1" "awk -v since=$(printf '%q' "$2") -f - /var/log/apt/history.log" << 'AWK'
/^Start-Date: / { keep = (substr($0, 13) >= since) }
keep && /^(Install|Upgrade|Downgrade|Reinstall|Remove|Purge): /
AWK
}

# summarize_history <counts|list> — reads apt_history_since output on stdin.
#   counts: "<upgraded> <installed> <removed>"
#   list:   Markdown, one block per action
# Line format: "Upgrade: libfoo:arm64 (1.0-1, 1.0-2), bar:arm64 (2.1, 2.2)"
summarize_history() {
    awk -v mode="$1" '
    {
        action = substr($0, 1, index($0, ":") - 1)
        n = split(substr($0, index($0, ": ") + 2), items, /\), /)
        for (i = 1; i <= n; i++) {
            item = items[i]
            sub(/\)$/, "", item)
            name = item; sub(/ \(.*/, "", name); sub(/:[^:]*$/, "", name)
            ver = item;  sub(/^[^(]*\(/, "", ver)
            if (action == "Upgrade" || action == "Downgrade") sub(/, /, " → ", ver)
            lines[action] = lines[action] "- `" name "` " ver "\n"
            count[action]++
        }
    }
    END {
        if (mode == "counts") {
            print count["Upgrade"] + 0, count["Install"] + 0, count["Remove"] + count["Purge"]
            exit
        }
        split("Upgrade Install Downgrade Reinstall Remove Purge", order, " ")
        for (i = 1; i <= 6; i++)
            if (order[i] in count)
                printf "**%s** (%d)\n\n%s\n", order[i], count[order[i]], lines[order[i]]
    }'
}

# wait_for_boot <host> <old boot ID> — true once the host answers with a new one
wait_for_boot() {
    local deadline=$(( SECONDS + REBOOT_TIMEOUT )) id
    while (( SECONDS < deadline )); do
        sleep 10
        # Connection errors are expected while the host is down; the loop
        # ends on a new boot ID or the deadline, so they are not shown.
        id=$(remote "$1" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null) || continue
        [[ "$id" != "$2" ]] && return 0
    done
    return 1
}

# ── Per host ──────────────────────────────────────────────────────────────────

# update_host <host> — returns 1 when the run should stop
update_host() {
    local h="$1"
    local boot_id kernel_before kernel_after start answer rc=0
    local failed_before failed_after history counts upgraded installed removed
    local reboot="not required" reboot_pkgs state rrc t0
    local -a problems=()

    echo
    echo -e "${BOLD}${CYAN}══ ${h} ══${RESET}"

    if ! boot_id=$(remote "$h" 'cat /proc/sys/kernel/random/boot_id'); then
        error "${h} is not reachable via SSH"
        ROW[$h]="| ${h} | unreachable | - | - | - | - | - |"
        SECTIONS+=$'\n'"## ${h}"$'\n\n'"Not reachable via SSH, nothing done."$'\n'
        confirm "Continue with the next host?" && return 0
        ABORT_REASON="${h} not reachable"
        return 1
    fi
    kernel_before=$(remote "$h" 'uname -r')
    failed_before=$(failed_units "$h")

    ssh -t "$h" system-update --dry-run || warn "dry-run exited with $?"

    read -r -p "Update ${h}? [y]es / [s]kip / [q]uit " answer
    case "$answer" in
        [yY]) ;;
        [sS])
            ROW[$h]="| ${h} | skipped | - | - | - | - | ${kernel_before} |"
            return 0 ;;
        *)
            ABORT_REASON="quit before ${h}"
            return 1 ;;
    esac

    start=$(remote "$h" "date '+%Y-%m-%d  %H:%M:%S'")
    ssh -t "$h" system-update || rc=$?
    (( rc != 0 )) && problems+=("system-update exited with ${rc}")

    history=$(apt_history_since "$h" "$start")
    counts=$(summarize_history counts <<< "$history")
    read -r upgraded installed removed <<< "$counts"

    if remote "$h" 'test -f /var/run/reboot-required'; then
        reboot_pkgs=$(remote "$h" 'if [ -f /var/run/reboot-required.pkgs ]; then cat /var/run/reboot-required.pkgs; fi' \
            | awk '{ printf "%s%s", sep, $1; sep = ", " }')
        reboot="required${reboot_pkgs:+ (${reboot_pkgs})}, not done"
        if confirm "Reboot ${h} now?"; then
            t0=$SECONDS
            rrc=0
            ssh -t "$h" sudo systemctl reboot || rrc=$?
            # 255: ssh lost the connection because the host went down - expected
            if (( rrc != 0 && rrc != 255 )); then
                problems+=("reboot command failed with ${rrc}")
                reboot="required, reboot command failed"
            elif wait_for_boot "$h" "$boot_id"; then
                # is-system-running exits non-zero for anything but "running";
                # the state string is checked below, so the exit code is not needed
                state=$(remote "$h" 'timeout 300 systemctl is-system-running --wait') || :
                reboot="done, back after $(( SECONDS - t0 )) s, ${state:-state unknown}"
                [[ "$state" != "running" ]] && problems+=("system state after reboot: ${state:-unknown}")
            else
                problems+=("not back within ${REBOOT_TIMEOUT} s after the reboot")
                reboot="done, not back within ${REBOOT_TIMEOUT} s"
            fi
        fi
    fi

    if [[ "$reboot" == "done, not back"* ]]; then
        kernel_after="unknown"
        failed_after="unknown"
    else
        kernel_after=$(remote "$h" 'uname -r')
        failed_after=$(failed_units "$h")
        [[ -n "$failed_after" ]] && problems+=("failed units: ${failed_after}")
    fi

    local kernel="$kernel_before"
    [[ "$kernel_after" != "$kernel_before" ]] && kernel="${kernel_before} → ${kernel_after}"
    local result="ok"
    (( ${#problems[@]} > 0 )) && result="problems"

    local reboot_col
    case "$reboot" in
        "not required") reboot_col="-" ;;
        "done, back"*)  reboot_col="done" ;;
        *"not done")    reboot_col="pending" ;;
        *)              reboot_col="failed" ;;
    esac

    ROW[$h]="| ${h} | ${result} | ${upgraded} | ${installed} | ${removed} | ${reboot_col} | ${kernel} |"

    SECTIONS+=$'\n'"## ${h}"$'\n\n'
    SECTIONS+="- system-update: exit ${rc}, started ${start} (host clock)"$'\n'
    SECTIONS+="- Failed units: ${failed_after:-none} (before the run: ${failed_before:-none})"$'\n'
    SECTIONS+="- Reboot: ${reboot}"$'\n'
    SECTIONS+="- Kernel: ${kernel}"$'\n'
    local p stop=false
    for p in "${problems[@]}"; do
        SECTIONS+="- **Problem:** ${p}"$'\n'
    done

    if (( ${#problems[@]} > 0 )); then
        for p in "${problems[@]}"; do error "$p"; done
        if confirm "Continue with the next host anyway?"; then
            SECTIONS+="- Continued with the next host despite the problems."$'\n'
        else
            ABORT_REASON="stopped after problems on ${h}"
            stop=true
        fi
    else
        ok "${h} done"
    fi

    # Package list last, so it lands in the report on a stop as well
    if [[ -n "$history" ]]; then
        SECTIONS+=$'\n'"$(summarize_history list <<< "$history")"$'\n'
    else
        SECTIONS+=$'\n'"No package changes."$'\n'
    fi

    ! $stop
}

# ── Report ────────────────────────────────────────────────────────────────────

write_report() {
    [[ -z "$RUN_STAMP" ]] && return 0   # no host started (--help, bad arguments)

    local file="${REPORT_DIR}/System-Update_${RUN_STAMP}.md"
    [[ -e "$file" ]] && file="${file%.md}_$$.md"   # never overwrite a report

    local result="complete"
    $RUN_COMPLETE || result="aborted (${ABORT_REASON})"

    local h
    {
        echo "# System update $(date -d "@${RUN_START}" '+%Y-%m-%d')"
        echo
        echo "- Run: $(date -d "@${RUN_START}" '+%Y-%m-%d %H:%M') – $(date '+%H:%M') from $(uname -n) (\`scripts/update-hosts.sh\`)"
        echo "- Result: ${result}"
        echo "- Backups verified before the run: confirmed"
        echo
        echo "| Host | Result | Upgraded | Installed | Removed | Reboot | Kernel |"
        echo "|---|---|---|---|---|---|---|"
        for h in "${HOSTS[@]}"; do
            echo "${ROW[$h]:-| ${h} | not reached | - | - | - | - | - |}"
        done
        echo -n "$SECTIONS"
    } > "$file"

    echo
    ok "Report written → ${file}"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    local from="" i start_index=0

    while (( $# > 0 )); do
        case "$1" in
            --from)    from="${2:-}"; shift 2 || { usage; exit 2; } ;;
            -h|--help) usage; exit 0 ;;
            *)         usage; exit 2 ;;
        esac
    done

    if [[ -n "$from" ]]; then
        start_index=-1
        for i in "${!HOSTS[@]}"; do
            [[ "${HOSTS[$i]}" == "$from" ]] && start_index=$i
        done
        if (( start_index < 0 )); then
            error "Unknown host: ${from} (known: ${HOSTS[*]})"
            exit 2
        fi
    fi

    if [[ ! -d "$REPORT_DIR" || ! -w "$REPORT_DIR" ]]; then
        error "Report directory ${REPORT_DIR} is missing or not writable (UPDATE_REPORT_DIR)"
        exit 1
    fi

    # backup-services runs at 02:00, verify-backup at 04:00 (wiki Update-Strategy)
    local now
    now=$(date +%H%M)
    if (( 10#$now >= 200 && 10#$now < 430 )); then
        warn "02:00-04:30 is the backup window; a reboot or Docker restart breaks the dumps."
        confirm "Start anyway?" || exit 1
    fi

    confirm "Backups fresh and verified (sudo verify-backup.sh on Mnemosyne)?" || exit 1

    RUN_START=$(date +%s)
    RUN_STAMP=$(date -d "@${RUN_START}" '+%Y-%m-%d_%H%M')
    trap write_report EXIT
    trap 'exit 130' INT TERM

    for (( i = 0; i < start_index; i++ )); do
        ROW[${HOSTS[$i]}]="| ${HOSTS[$i]} | not in this run (--from ${from}) | - | - | - | - | - |"
    done

    for (( i = start_index; i < ${#HOSTS[@]}; i++ )); do
        update_host "${HOSTS[$i]}" || exit 1
    done

    RUN_COMPLETE=true
    echo
    ok "All hosts done. Next: container updates on Mnemosyne (wiki/Update-Strategy.md)."
}

# Guarded so the functions can be sourced for testing
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
