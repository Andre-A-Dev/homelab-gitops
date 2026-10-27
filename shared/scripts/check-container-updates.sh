#!/bin/bash
# =============================================================================
# check-container-updates.sh
# Check all running Docker containers for available image updates.
#
# How it works:
#   - Fetches the manifest digest from the registry via skopeo (metadata only,
#     no image layers are downloaded)
#   - Compares the remote digest against the locally stored digest
#   - Prints a color-coded status table with compose file paths for updates
#   - Shows each container's update class (label homelab.update-class, see
#     wiki Update-Strategy): A = stateful, verify the backup before updating;
#     B = stateless; C = local build, whose base image this check cannot see
#
# Dependencies: skopeo, docker
#   Install: sudo apt install skopeo
#
# Usage:
#   check-container-updates            # check all running containers
#   check-container-updates --quiet    # show only containers with updates
#
# Installation (run on Mnemosyne):
#   sudo ln -sf ~/homelab-infra/shared/scripts/check-container-updates.sh \
#               /usr/local/bin/check-container-updates
# =============================================================================

set -uo pipefail

# --- CLI flags ----------------------------------------------------------------
QUIET=false
for arg in "$@"; do
    [[ "$arg" == "--quiet" ]] && QUIET=true
done

# --- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# --- Counters / result tracking -----------------------------------------------
COUNT_OK=0
COUNT_UPDATE=0
COUNT_SKIP=0

# Compose dirs of local builds (class C) - their base images are never
# checked here, so the footer reminds to rebuild them with --pull.
declare -A LOCAL_BUILD_DIRS=()

# Associative array: container name → compose working dir
# Must be initialized with =() — a bare declare leaves it unset, and
# ${#UPDATES_MAP[@]} then trips set -u when no updates were found.
declare -A UPDATES_MAP=()

# --- Column widths (pure ASCII — avoids printf width miscounting) -------------
COL_NAME=26
COL_CLASS=5
COL_IMAGE=44

# --- Auto-detect host architecture --------------------------------------------
case "$(uname -m)" in
    aarch64) HOST_ARCH="arm64" ;;
    armv7l)  HOST_ARCH="arm"   ;;
    x86_64)  HOST_ARCH="amd64" ;;
    *)       HOST_ARCH="$(uname -m)" ;;
esac

# =============================================================================
# check_deps — abort early if required tools are missing
# =============================================================================
check_deps() {
    local missing=()
    command -v skopeo >/dev/null 2>&1 || missing+=("skopeo")
    command -v docker >/dev/null 2>&1 || missing+=("docker")

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${RED}Error: missing dependencies: ${missing[*]}${NC}"
        echo -e "Install with:  sudo apt install ${missing[*]}"
        exit 1
    fi
}

# =============================================================================
# print_row NAME CLASS IMAGE STATUS_TEXT STATUS_COLOR [PREFIX]
# Prints one fixed-width table row.
#
# Emoji are NOT inside printf format strings — printf counts bytes, not
# display columns, so emoji break alignment. Status is appended via echo -e.
# =============================================================================
print_row() {
    local name="$1"
    local class="${2:-?}"
    local image_display="$3"
    local status_text="$4"
    local status_color="$5"
    local prefix="${6:-  }"

    local name_col="${name:0:${COL_NAME}}"
    local image_col="${image_display:0:${COL_IMAGE}}"
    [[ ${#name}          -gt $COL_NAME  ]] && name_col="${name:0:$((COL_NAME-1))}…"
    [[ ${#image_display} -gt $COL_IMAGE ]] && image_col="${image_display:0:$((COL_IMAGE-1))}…"

    printf "  ${BOLD}%-${COL_NAME}s${NC}  %-${COL_CLASS}s  ${DIM}%-${COL_IMAGE}s${NC}  " \
        "$name_col" "$class" "$image_col"
    echo -e "${status_color}${prefix}${status_text}${NC}"
}

# =============================================================================
# get_compose_info CONTAINER_NAME
# Prints two lines: compose working dir path and service name.
# Docker Compose sets these labels on every container it manages:
#   com.docker.compose.project.working_dir  → stack directory
#   com.docker.compose.service              → service name within the stack
# Returns empty lines for containers not managed by Compose.
# =============================================================================
get_compose_info() {
    local container="$1"
    docker inspect "$container" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}
{{index .Config.Labels "com.docker.compose.service"}}' \
        2>/dev/null
}

# =============================================================================
# get_local_digest IMAGE
# Returns the manifest digest from Docker's RepoDigests (sha256:...).
# =============================================================================
get_local_digest() {
    local image="$1"
    docker image inspect "$image" \
        --format '{{range .RepoDigests}}{{.}}{{"\n"}}{{end}}' 2>/dev/null \
        | head -1 \
        | cut -d'@' -f2
}

# =============================================================================
# get_remote_digest IMAGE
# Fetches the raw manifest from the registry via skopeo (no layer download).
#
# Digest computation matches Docker's RepoDigests:
#   Multi-arch:  skopeo returns manifest list   → sha256 = list digest  ✓
#   Single-arch: skopeo returns single manifest → sha256 = image digest ✓
#
# Uses a temp file to preserve exact bytes (shell vars strip trailing newlines).
# =============================================================================
get_remote_digest() {
    local image="$1"
    local tmpfile
    tmpfile=$(mktemp) || return 1

    # --raw: manifest metadata only, no layers downloaded.
    # No --override-arch: we want the manifest list digest (= RepoDigests level).
    if ! skopeo inspect --raw "docker://${image}" >"$tmpfile" 2>/dev/null; then
        rm -f "$tmpfile"
        return 1
    fi

    local digest="sha256:$(sha256sum "$tmpfile" | cut -d' ' -f1)"
    rm -f "$tmpfile"
    echo "$digest"
}

# =============================================================================
# remember_local_build NAME - record the compose dir for the class C footer
# =============================================================================
remember_local_build() {
    local workdir
    workdir=$(get_compose_info "$1" | head -1)
    [[ -n "$workdir" ]] && LOCAL_BUILD_DIRS["$workdir"]=1
}

# =============================================================================
# check_container NAME IMAGE CLASS
# =============================================================================
check_container() {
    local name="$1"
    local image="$2"
    local class="$3"

    # Bare image ID — container started without a named registry tag
    if [[ "$image" == sha256:* ]]; then
        $QUIET || print_row "$name" "$class" "(no registry tag)" "no tag" "$BLUE" "- "
        ((COUNT_SKIP++)) || true
        return
    fi

    local local_digest
    local_digest=$(get_local_digest "$image")

    # No RepoDigests at all → definitely a local build (never pulled/pushed)
    if [[ -z "$local_digest" ]]; then
        remember_local_build "$name"
        $QUIET || print_row "$name" "$class" "$image" "local build" "$DIM" "  "
        ((COUNT_SKIP++)) || true
        return
    fi

    local remote_digest
    if ! remote_digest=$(get_remote_digest "$image") || [[ -z "$remote_digest" ]]; then
        # skopeo failed — distinguish local build from genuine registry error.
        #
        # Local builds can still have RepoDigests (e.g. containerd image store),
        # so they reach this point. They have no registry namespace (no slash),
        # with or without an explicit tag:
        #   monitoring-meross-exporter  ← compose-generated name
        #   carousel:latest             ← explicit image: tag on a build
        #
        # Unqualified names that DO exist upstream (caddy:latest, redis:6-alpine)
        # resolve to Docker Hub library images and succeed above, so a failure
        # here means "not in any registry". Namespaced images that fail
        # (pdreker/fritz_exporter:latest) are genuine registry errors.
        if [[ "$image" != */* ]]; then
            remember_local_build "$name"
            $QUIET || print_row "$name" "$class" "$image" "local build" "$DIM" "  "
        else
            $QUIET || print_row "$name" "$class" "$image" "registry error" "$RED" "! "
        fi
        ((COUNT_SKIP++)) || true
        return
    fi

    if [[ "$local_digest" == "$remote_digest" ]]; then
        ((COUNT_OK++)) || true
        $QUIET || print_row "$name" "$class" "$image" "up to date" "$GREEN" "v "
    else
        ((COUNT_UPDATE++)) || true
        # Store workdir and service name (colon-separated) for the footer one-liner
        local compose_info workdir service_name
        compose_info=$(get_compose_info "$name")
        workdir=$(echo "$compose_info" | head -1)
        service_name=$(echo "$compose_info" | tail -1)
        UPDATES_MAP["$name"]="${class}:${workdir}:${service_name}"

        # Always show update rows regardless of --quiet
        print_row "$name" "$class" "$image" "UPDATE AVAILABLE" "$YELLOW" "^ "
        printf "       ${DIM}local  %-19s${NC}\n" "${local_digest:7:19}…"
        printf "       ${DIM}remote %-19s${NC}\n" "${remote_digest:7:19}…"
    fi
}

# =============================================================================
# print_header
# =============================================================================
print_header() {
    $QUIET && return
    local width=$(( COL_NAME + COL_CLASS + COL_IMAGE + 24 ))
    local sep; sep=$(printf '%*s' "$width" '' | tr ' ' '-')
    echo
    echo -e "${BOLD}  Docker Image Update Check${NC}  ${DIM}$(date '+%Y-%m-%d %H:%M:%S')  |  ${HOST_ARCH}${NC}"
    echo -e "  ${DIM}${sep}${NC}"
    printf "  ${BOLD}%-${COL_NAME}s  %-${COL_CLASS}s  %-${COL_IMAGE}s  %s${NC}\n" "CONTAINER" "CLASS" "IMAGE" "STATUS"
    echo -e "  ${DIM}${sep}${NC}"
    echo
}

# =============================================================================
# print_footer
# =============================================================================
print_footer() {
    local total=$(( COUNT_OK + COUNT_UPDATE + COUNT_SKIP ))
    local width=$(( COL_NAME + COL_CLASS + COL_IMAGE + 24 ))
    local sep; sep=$(printf '%*s' "$width" '' | tr ' ' '-')
    echo
    echo -e "  ${DIM}${sep}${NC}"
    printf "  ${GREEN}v  Up to date:${NC}              %d\n" "$COUNT_OK"
    printf "  ${YELLOW}^  Update available:${NC}        %d\n" "$COUNT_UPDATE"
    printf "  ${DIM}   Skipped (local/no tag):${NC}    %d\n" "$COUNT_SKIP"
    printf "  ${DIM}   Total:${NC}                     %d\n" "$total"
    echo -e "  ${DIM}${sep}${NC}"

    if [[ ${#UPDATES_MAP[@]} -gt 0 ]]; then
        echo
        echo -e "  ${YELLOW}${BOLD}Updates available — copy-paste to update:${NC}"
        echo

        # Sort container names for consistent output
        local sorted_names
        sorted_names=$(printf '%s\n' "${!UPDATES_MAP[@]}" | sort)

        while IFS= read -r container; do
            local entry="${UPDATES_MAP[$container]}"
            local class="${entry%%:*}"
            local rest="${entry#*:}"
            local workdir="${rest%%:*}"
            local service="${rest##*:}"

            printf "  ${YELLOW}^${NC}  ${BOLD}%s${NC}  ${DIM}class %s${NC}\n" "$container" "${class:-?}"
            case "$class" in
                A) printf "     ${YELLOW}stateful: sudo verify-backup.sh and read the release notes first${NC}\n" ;;
                B) ;;
                *) printf "     ${YELLOW}no update class label - see wiki Update-Strategy${NC}\n" ;;
            esac

            if [[ -n "$workdir" && -n "$service" ]]; then
                # Full one-liner: cd → pull → up (prune once at the end)
                printf "     ${DIM}cd %s \\\\\n" "$workdir"
                printf "       && docker compose pull %s \\\\\n" "$service"
                printf "       && docker compose up -d %s${NC}\n" "$service"
            elif [[ -n "$workdir" ]]; then
                # Compose-managed but service label missing — generic fallback
                printf "     ${DIM}cd %s \\\\\n" "$workdir"
                printf "       && docker compose pull \\\\\n"
                printf "       && docker compose up -d${NC}\n"
            else
                # Not Compose-managed — no automated update path
                printf "     ${DIM}(not managed by Compose — update manually)${NC}\n"
            fi
            echo
        done <<< "$sorted_names"

        echo
        echo -e "  ${DIM}  docker image prune   # only after the updated services are verified${NC}"
    fi

    if [[ ${#LOCAL_BUILD_DIRS[@]} -gt 0 ]]; then
        echo
        echo -e "  ${BOLD}Local builds (class C)${NC} ${DIM}- base images are not checked here; refresh per stack:${NC}"
        local dir
        while IFS= read -r dir; do
            printf "     ${DIM}cd %s && docker compose build --pull && docker compose up -d${NC}\n" "$dir"
        done < <(printf '%s\n' "${!LOCAL_BUILD_DIRS[@]}" | sort)
    fi
    echo
}

# =============================================================================
# main
# =============================================================================
main() {
    check_deps
    print_header

    local containers
    containers=$(docker ps --format '{{.Names}}|{{.Image}}|{{.Label "homelab.update-class"}}' | sort) || {
        echo -e "${RED}Error: could not list Docker containers${NC}"
        exit 1
    }

    if [[ -z "$containers" ]]; then
        echo -e "  ${DIM}No running containers found.${NC}"
    else
        while IFS='|' read -r name image class; do
            check_container "$name" "$image" "$class"
        done <<< "$containers"
    fi

    print_footer
}

main "$@"