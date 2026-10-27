#!/bin/bash
# =============================================================================
# restore-services.sh
# =============================================================================
# Interactively restores selected services from a backup snapshot created by
# backup-services.sh.
#
# Workflow:
#   1. Select a backup snapshot from the available dated directories
#   2. Toggle individual services or all at once in an interactive menu
#   3. Confirm the selection — no data is touched before explicit confirmation
#   4. Affected containers are stopped, data is restored, containers restart
#
# Archives store absolute source paths without the leading "/", so every
# file-based restore is `tar -x... -C /` into the live bind-mount path.
#
# Special handling:
#   Vaultwarden — the consistent `sqlite3 .backup` copy wins over the live
#                db files inside the data archive
#   Nextcloud  — maintenance mode on/off around the restore, DB container
#                stays up for the SQL import while the app container is down
#   Ghost      — app stopped, MySQL dump imported into the running ghost-db
#   Immich     — uploads only; the DB needs an empty database (see RUNBOOK)
#   Calibre    — calibre-web is stopped during library restore to prevent
#                read/write conflicts on the bind-mounted library path
#   Syncthing  — host service (syncthing@andre), not a container
#   Stack cfg  — ~/stacks/ is a symlink; tar extracts to the symlink target
#                (homelab-infra/mnemosyne/stacks/). Symlink must be in place.
#
# Usage:
#   sudo /usr/local/bin/restore-services.sh
#
# Exit codes:
#   0 — restore completed (check output for per-service status)
#   1 — aborted by user or fatal pre-flight error
# =============================================================================


# ── Configuration ──────────────────────────────────────────────────────────────

# Override via `BACKUP_DIR=/path sudo -E ./restore-services.sh` for testing
# against an alternate source (e.g. a downloaded offsite copy) without
# touching the default. Destination paths for restored data are unaffected —
# they are always the live production paths (see restore_* functions below).
BACKUP_DIR="${BACKUP_DIR:-/mnt/backup}"
STACKS_DIR="/home/andre/stacks"
ENV_NEXTCLOUD="$STACKS_DIR/nextcloud/.env"
ENV_GHOST="$STACKS_DIR/ghost/.env"
SYNCTHING_UNIT="syncthing@andre.service"
LOG="/var/log/restore-services.log"


# ── Colors ─────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ERRORS=0
RESTORED=0
CURRENT_STEP=0
TOTAL_STEPS=0


# ── Spinner ────────────────────────────────────────────────────────────────────
# Identical pattern to backup-services.sh and verify-backup.sh for consistency.

SPINNER_PID=""
SPINNER_CHARS="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STEP_START_TIME=0

spinner_start() {
  # Stop any previous spinner first — restore functions call spinner_start
  # several times in a row (stopping/restoring/starting) without an explicit
  # spinner_stop between them. Without this, each call leaked the previous
  # background process, and multiple spinners fought over the same terminal
  # line — visible as flickering "Starting/Stopping" text on long operations.
  spinner_stop
  local msg="$1"
  (
    local i=0
    local start_time
    start_time=$(date +%s)
    while true; do
      local char="${SPINNER_CHARS:$((i % ${#SPINNER_CHARS})):1}"
      local elapsed=$(( $(date +%s) - start_time ))
      local time_str
      if [ "$elapsed" -ge 60 ]; then
        time_str="$(( elapsed / 60 ))m $(( elapsed % 60 ))s"
      else
        time_str="${elapsed}s"
      fi
      printf "\r  ${CYAN}%s${RESET}  %s... %s" "$char" "$msg" "$time_str"
      sleep 0.5
      ((i++))
    done
  ) &
  SPINNER_PID=$!
  disown "$SPINNER_PID"
}

spinner_stop() {
  if [ -n "$SPINNER_PID" ]; then
    kill "$SPINNER_PID" 2>/dev/null
    wait "$SPINNER_PID" 2>/dev/null
    printf "\r\033[K"
    SPINNER_PID=""
  fi
}

# Formats seconds as "Xm Ys" (>= 60s) or "Xs" (< 60s).
format_duration() {
  local secs="$1"
  if [ "$secs" -ge 60 ]; then
    echo "$(( secs / 60 ))m $(( secs % 60 ))s"
  else
    echo "${secs}s"
  fi
}


# ── Logging ────────────────────────────────────────────────────────────────────

strip_ansi() {
  sed -r 's/\x1B\[[0-9;]*[mGKHF]//g'
}

log_file() {
  echo "$1" | strip_ansi >> "$LOG"
}

# Run a command — output goes to log only (spinner shows on terminal instead).
run() {
  "$@" >> "$LOG" 2>&1
  return $?
}

# Run tar — suppress the "Removing leading /" notice that tar prints by default.
run_tar() {
  "$@" 2>&1 | grep -v "Removing leading" | strip_ansi >> "$LOG"
  return "${PIPESTATUS[0]}"
}


# ── Result helpers ─────────────────────────────────────────────────────────────

ok()   { local e=$(( $(date +%s) - STEP_START_TIME )); spinner_stop; echo -e "  ${GREEN}✓ OK${RESET}   $1 ($(format_duration $e))"; log_file "  OK   $1 ($(format_duration $e))"; ((RESTORED++)); return 0; }
fail() { local e=$(( $(date +%s) - STEP_START_TIME )); spinner_stop; echo -e "  ${RED}✗ FAIL${RESET} $1 ($(format_duration $e))"; log_file "  FAIL $1 ($(format_duration $e))"; ((ERRORS++)); return 0; }
warn() { spinner_stop; echo -e "  ${YELLOW}⚠ WARN${RESET} $1"; log_file "  WARN $1"; }
info() { echo -e "         ${CYAN}$1${RESET}"; log_file "         $1"; }

# Section header — matches the visual style of backup-services.sh step().
step() {
  STEP_START_TIME=$(date +%s)
  ((CURRENT_STEP++))
  local label="$1"
  local prefix="[${CURRENT_STEP}/${TOTAL_STEPS}] ${label} "
  local pad_width=$(( 62 - ${#prefix} ))
  [ "$pad_width" -lt 1 ] && pad_width=1
  local pad
  pad=$(printf '%0.s─' $(seq 1 "$pad_width"))
  echo ""
  echo -e "${CYAN}${BOLD}[${CURRENT_STEP}/${TOTAL_STEPS}]${RESET} ${BOLD}${label}${RESET} ${CYAN}${pad}${RESET}"
  log_file ""
  log_file "[${CURRENT_STEP}/${TOTAL_STEPS}] ${label}"
  spinner_start "$label"
}


# ── Service registry ──────────────────────────────────────────────────────────
# Ordered list used for menu display and restore execution sequence.

SERVICE_IDS=(
  vaultwarden
  caddy
  calibre-library
  calibre-web
  kosync
  syncthing
  aegis
  gitea
  gitea-runner
  ghost
  nextcloud
  immich
  jobiris
  grafana
  exporter-tokens
  stack-configs
)

declare -A SERVICE_LABEL=(
  [vaultwarden]="Vaultwarden"
  [caddy]="Caddy TLS certificates"
  [calibre-library]="Calibre Library"
  [calibre-web]="Calibre-Web config"
  [kosync]="KOSync"
  [syncthing]="Syncthing"
  [aegis]="Aegis 2FA backup"
  [gitea]="Gitea"
  [gitea-runner]="Gitea Act Runner"
  [ghost]="Ghost"
  [nextcloud]="Nextcloud"
  [immich]="Immich (uploads only)"
  [jobiris]="JobIris"
  [grafana]="Grafana"
  [exporter-tokens]="Exporter tokens"
  [stack-configs]="Stack configs"
)

# Backup files that must exist in the snapshot for each service.
declare -A SERVICE_FILES=(
  [vaultwarden]="vaultwarden-data.tar.gz vaultwarden-db.sqlite3"
  [caddy]="caddy-data.tar.gz"
  [calibre-library]="calibre-library.tar.gz"
  [calibre-web]="calibre-web-config.tar.gz"
  [kosync]="kosync-data.tar.gz"
  [syncthing]="syncthing-obsidian.tar.gz syncthing-config.tar.gz"
  [aegis]="aegis-backup.tar.gz"
  [gitea]="gitea-data.tar.gz"
  [gitea-runner]="gitea-runner.tar.gz"
  [ghost]="ghost-content.tar.gz ghost-db.sql"
  [nextcloud]="nextcloud-data.tar nextcloud-db.sql"
  [immich]="immich-upload.tar"
  [jobiris]="jobiris.tar.gz"
  [grafana]="grafana-data.tar.gz"
  [exporter-tokens]="exporter-tokens.tar.gz"
  [stack-configs]="stacks-config.tar.gz"
)

# Initialize all services as deselected (0 = off, 1 = on).
declare -A SELECTED=()
for _id in "${SERVICE_IDS[@]}"; do
  SELECTED[$_id]=0
done


# ── Snapshot selection ─────────────────────────────────────────────────────────

BACKUP_DATE=""
BACKUP_PATH=""

select_snapshot() {
  # Collect all YYYY-MM-DD directories, newest first.
  local snapshots=()
  while IFS= read -r -d '' dir; do
    snapshots+=("$(basename "$dir")")
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type d \
    -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' -print0 | sort -rz)

  if [ "${#snapshots[@]}" -eq 0 ]; then
    echo -e "${RED}  ERROR: No backup snapshots found in $BACKUP_DIR${RESET}"
    exit 1
  fi

  echo ""
  echo -e "${BOLD}  Available snapshots:${RESET}"
  echo ""
  for i in "${!snapshots[@]}"; do
    local date="${snapshots[$i]}"
    local size
    size=$(du -sh "$BACKUP_DIR/$date" 2>/dev/null | cut -f1)
    local tag=""
    [ "$i" -eq 0 ] && tag="  ${CYAN}← latest${RESET}"
    printf "  ${BOLD}[%2d]${RESET}  %s   %5s%b\n" "$((i+1))" "$date" "$size" "$tag"
  done
  echo ""

  local choice
  while true; do
    read -rp "  Select snapshot [1-${#snapshots[@]}], default 1: " choice
    choice="${choice:-1}"
    if [[ "$choice" =~ ^[0-9]+$ ]] \
       && [ "$choice" -ge 1 ] \
       && [ "$choice" -le "${#snapshots[@]}" ]; then
      BACKUP_DATE="${snapshots[$((choice-1))]}"
      BACKUP_PATH="$BACKUP_DIR/$BACKUP_DATE"
      break
    fi
    echo -e "  ${YELLOW}Invalid — enter a number between 1 and ${#snapshots[@]}.${RESET}"
  done
}


# ── Service selection menu ─────────────────────────────────────────────────────

count_selected() {
  local n=0
  for id in "${SERVICE_IDS[@]}"; do
    [ "${SELECTED[$id]}" -eq 1 ] && ((n++))
  done
  echo "$n"
}

# Print the toggle menu. The cursor is left on the line after the last line
# printed so the caller can save/restore position for in-place redrawing.
draw_menu() {
  echo ""
  echo -e "${BOLD}  Select services to restore${RESET}  (snapshot: ${CYAN}${BACKUP_DATE}${RESET})"
  echo ""

  local i=1
  for id in "${SERVICE_IDS[@]}"; do
    local label="${SERVICE_LABEL[$id]}"
    local files="${SERVICE_FILES[$id]}"
    local n_files
    n_files=$(echo "$files" | wc -w)
    local file_label
    [ "$n_files" -eq 1 ] && file_label="1 file" || file_label="${n_files} files"

    if [ "${SELECTED[$id]}" -eq 1 ]; then
      printf "  ${BOLD}[%2d]${RESET}  ${GREEN}[✓]${RESET}  %-32s${CYAN}(%s)${RESET}\n" \
        "$i" "$label" "$file_label"
    else
      printf "  ${BOLD}[%2d]${RESET}  [ ]  %-32s${CYAN}(%s)${RESET}\n" \
        "$i" "$label" "$file_label"
    fi
    ((i++))
  done

  local n_sel
  n_sel=$(count_selected)
  echo ""
  printf "  ${BOLD}[ a]${RESET}  Toggle all   ${BOLD}[ q]${RESET}  Quit\n"
  echo ""
  echo -e "  ──────────────────────────────────────────────────────"
  if [ "$n_sel" -gt 0 ]; then
    printf "  ${GREEN}%d service(s) selected${RESET} — press Enter to confirm\n" "$n_sel"
  else
    printf "  No services selected\n"
  fi
}

service_selection_menu() {
  # Save cursor position before drawing so we can redraw in place on each toggle.
  tput sc
  draw_menu

  while true; do
    printf "  > "
    local input
    read -r input

    # Restore cursor to saved position and clear everything below it.
    tput rc
    tput ed

    case "$input" in
      "")
        if [ "$(count_selected)" -eq 0 ]; then
          draw_menu
          echo -e "  ${YELLOW}Select at least one service before confirming.${RESET}"
          tput sc
          continue
        fi
        # Selection confirmed — clear the menu before continuing.
        tput rc
        tput ed
        break
        ;;
      a|A)
        # If anything is selected, deselect all; otherwise select all.
        if [ "$(count_selected)" -gt 0 ]; then
          for id in "${SERVICE_IDS[@]}"; do SELECTED[$id]=0; done
        else
          for id in "${SERVICE_IDS[@]}"; do SELECTED[$id]=1; done
        fi
        ;;
      q|Q)
        tput rc
        tput ed
        echo ""
        echo -e "  ${YELLOW}Aborted.${RESET}"
        echo ""
        exit 0
        ;;
      *)
        if [[ "$input" =~ ^[0-9]+$ ]] \
           && [ "$input" -ge 1 ] \
           && [ "$input" -le "${#SERVICE_IDS[@]}" ]; then
          local id="${SERVICE_IDS[$((input-1))]}"
          [ "${SELECTED[$id]}" -eq 1 ] && SELECTED[$id]=0 || SELECTED[$id]=1
        fi
        ;;
    esac

    tput sc
    draw_menu
  done
}


# ── Archive resolution ─────────────────────────────────────────────────────────
# backup-services.sh skips unchanged services and writes a .SKIPPED marker
# containing the date of the last real archive instead of a new archive.
# resolve_archive returns the path to the actual file — either in BACKUP_PATH
# directly, or in the referenced older snapshot. Returns empty string if neither
# the file nor a valid marker exists.
resolve_archive() {
  local filename="$1"
  local direct="$BACKUP_PATH/$filename"
  local marker="$BACKUP_PATH/${filename}.SKIPPED"

  if [ -f "$direct" ]; then
    echo "$direct"
    return 0
  fi

  if [ -f "$marker" ]; then
    local ref_date
    ref_date=$(cat "$marker" | tr -d '[:space:]')
    local ref_file="$BACKUP_DIR/$ref_date/$filename"
    if [ -n "$ref_date" ] && [ -f "$ref_file" ]; then
      echo "$ref_file"
      return 0
    fi
  fi

  echo ""
  return 1
}


# ── Pre-restore file check ─────────────────────────────────────────────────────
# Verify all required backup files exist before touching any live data.
# Follows .SKIPPED markers to older snapshots when a service was not backed up
# on the selected date because no files had changed.

check_backup_files() {
  echo ""
  echo -e "${BOLD}  Checking backup files...${RESET}"
  local missing=0
  for id in "${SERVICE_IDS[@]}"; do
    [ "${SELECTED[$id]}" -ne 1 ] && continue
    for file in ${SERVICE_FILES[$id]}; do
      local resolved
      resolved=$(resolve_archive "$file")
      if [ -z "$resolved" ]; then
        # Check if a marker exists but the referenced archive is gone
        local marker="$BACKUP_PATH/${file}.SKIPPED"
        if [ -f "$marker" ]; then
          local ref_date
          ref_date=$(cat "$marker" | tr -d '[:space:]')
          echo -e "  ${RED}  MISSING${RESET}  $file  ${YELLOW}(referenced snapshot $ref_date not found — may have been pruned)${RESET}"
        else
          echo -e "  ${RED}  MISSING${RESET}  $file"
        fi
        ((missing++))
      elif [ "$resolved" != "$BACKUP_PATH/$file" ]; then
        # File resolved via .SKIPPED marker — show where it came from
        local ref_date
        ref_date=$(basename "$(dirname "$resolved")")
        echo -e "  ${CYAN}  SKIPPED${RESET}  $file  ${CYAN}(using $ref_date)${RESET}"
      fi
    done
  done
  if [ "$missing" -gt 0 ]; then
    echo ""
    echo -e "  ${RED}${BOLD}$missing file(s) missing in snapshot $BACKUP_DATE — aborting.${RESET}"
    echo ""
    exit 1
  fi
  echo -e "  ${GREEN}  All required files present.${RESET}"
}


# ── Confirmation prompt ────────────────────────────────────────────────────────
# No data is modified until the user types "yes" here.

confirm_restore() {
  echo ""
  echo -e "${YELLOW}${BOLD}  ┌─ WARNING ──────────────────────────────────────────────────┐${RESET}"
  echo -e "${YELLOW}${BOLD}  │  This will OVERWRITE existing data with backup content.    │${RESET}"
  echo -e "${YELLOW}${BOLD}  │  Affected containers will be stopped during the restore.   │${RESET}"
  echo -e "${YELLOW}${BOLD}  └────────────────────────────────────────────────────────────┘${RESET}"
  echo ""
  echo -e "  Snapshot : ${BOLD}${BACKUP_DATE}${RESET}  ($(du -sh "$BACKUP_PATH" | cut -f1))"
  echo -e "  Services :"
  for id in "${SERVICE_IDS[@]}"; do
    [ "${SELECTED[$id]}" -eq 1 ] \
      && echo -e "    ${CYAN}•${RESET} ${SERVICE_LABEL[$id]}"
  done
  echo ""
  read -rp "  Type 'yes' to proceed: " confirm
  if [ "$confirm" != "yes" ]; then
    echo ""
    echo -e "  ${YELLOW}Aborted.${RESET}"
    echo ""
    exit 0
  fi
}


# ── Container helpers ──────────────────────────────────────────────────────────

container_stop() {
  # Silently ignore errors — container may already be stopped.
  docker stop "$1" >> "$LOG" 2>&1 || true
}

container_start() {
  docker start "$1" >> "$LOG" 2>&1 || true
}

# For optional containers (seasonal exporters): stop only what runs, and start
# only that again - never start a container a compose profile switched off.
container_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}


# ── Restore functions ─────────────────────────────────────────────────────────
# Each function is self-contained: it stops the relevant container(s), restores
# data, and restarts. A failed restore still restarts the container so the
# service doesn't stay down indefinitely after a partial failure.

restore_vaultwarden() {
  step "Vaultwarden"

  spinner_start "Stopping Vaultwarden"
  container_stop vaultwarden

  # Data directory first (attachments, sends, keys), but without the db files:
  # the archive holds the live db.sqlite3 plus -wal/-shm as tar read them, i.e.
  # possibly torn. Extracting them after the copy below used to overwrite the
  # consistent database with exactly that.
  spinner_start "Restoring data directory"
  run_tar tar -xzf "$BACKUP_PATH/vaultwarden-data.tar.gz" -C / \
    --exclude='*/db.sqlite3' --exclude='*/db.sqlite3-wal' --exclude='*/db.sqlite3-shm'
  local rc_data=$?

  # Then the consistent `sqlite3 .backup` copy. Stale -wal/-shm from the live
  # database must go, or SQLite replays a foreign WAL onto the restored file.
  spinner_start "Restoring database"
  rm -f /mnt/vault/vaultwarden/data/db.sqlite3-wal /mnt/vault/vaultwarden/data/db.sqlite3-shm
  run cp "$BACKUP_PATH/vaultwarden-db.sqlite3" /mnt/vault/vaultwarden/data/db.sqlite3
  local rc_db=$?

  spinner_start "Starting Vaultwarden"
  container_start vaultwarden

  if [ "$rc_data" -eq 0 ] && [ "$rc_db" -eq 0 ]; then
    ok "Vaultwarden restored from $BACKUP_DATE"
  else
    fail "Vaultwarden — restore failed (data: $rc_data, db: $rc_db)"
  fi
}

restore_caddy() {
  step "Caddy TLS certificates"

  spinner_start "Stopping Caddy"
  container_stop caddy

  spinner_start "Restoring TLS data"
  run_tar tar -xzf "$BACKUP_PATH/caddy-data.tar.gz" -C /
  local rc=$?

  spinner_start "Starting Caddy"
  container_start caddy

  [ "$rc" -eq 0 ] \
    && ok "Caddy TLS restored from $BACKUP_DATE" \
    || fail "Caddy — archive extraction failed"
}

restore_calibre_library() {
  step "Calibre Library"

  local archive
  archive=$(resolve_archive "calibre-library.tar.gz")

  # calibre-web holds the library path open; stop it to avoid conflicts.
  spinner_start "Stopping Calibre-Web"
  container_stop calibre-web

  spinner_start "Restoring library"
  run_tar tar -xzf "$archive" -C /
  local rc=$?

  spinner_start "Starting Calibre-Web"
  container_start calibre-web

  [ "$rc" -eq 0 ] \
    && ok "Calibre Library restored from $(basename "$(dirname "$archive")")" \
    || fail "Calibre Library — archive extraction failed"
}

restore_calibre_web() {
  step "Calibre-Web config"

  spinner_start "Stopping Calibre-Web"
  container_stop calibre-web

  # Bind mount /mnt/codex/calibre-web-config. This used to unpack into the
  # named volume calibre-web-config, which the container no longer uses -
  # the restore reported success and changed nothing.
  spinner_start "Restoring config"
  run_tar tar -xzf "$BACKUP_PATH/calibre-web-config.tar.gz" -C /
  local rc=$?

  spinner_start "Starting Calibre-Web"
  container_start calibre-web

  [ "$rc" -eq 0 ] \
    && ok "Calibre-Web config restored from $BACKUP_DATE" \
    || fail "Calibre-Web — archive extraction failed"
}

restore_kosync() {
  step "KOSync"

  spinner_start "Stopping KOSync"
  container_stop kosync

  # Bind mount /mnt/codex/kosync/data (formerly the named volume kosync-data).
  spinner_start "Restoring data"
  run_tar tar -xzf "$BACKUP_PATH/kosync-data.tar.gz" -C /
  local rc=$?

  spinner_start "Starting KOSync"
  container_start kosync

  [ "$rc" -eq 0 ] \
    && ok "KOSync restored from $BACKUP_DATE" \
    || fail "KOSync — archive extraction failed"
}

restore_syncthing() {
  step "Syncthing"

  # Syncthing runs on the host. `docker stop syncthing` used to fail silently,
  # so files were replaced underneath a running Syncthing.
  spinner_start "Stopping Syncthing"
  run systemctl stop "$SYNCTHING_UNIT"

  spinner_start "Restoring Obsidian vault"
  run_tar tar -xzf "$BACKUP_PATH/syncthing-obsidian.tar.gz" -C /
  local rc_vault=$?

  spinner_start "Restoring Syncthing config"
  run_tar tar -xzf "$BACKUP_PATH/syncthing-config.tar.gz" -C /
  local rc_cfg=$?

  spinner_start "Starting Syncthing"
  run systemctl start "$SYNCTHING_UNIT"

  if [ "$rc_vault" -eq 0 ] && [ "$rc_cfg" -eq 0 ]; then
    ok "Syncthing restored from $BACKUP_DATE"
  else
    fail "Syncthing — partial restore failure (vault: $rc_vault, config: $rc_cfg)"
  fi
}

restore_aegis() {
  step "Aegis 2FA backup"

  # Aegis backup is a plain directory — no container to stop.
  # The encrypted JSON file is restored to the Syncthing-watched folder
  # so it syncs back to Android automatically on next Syncthing run.
  spinner_start "Restoring Aegis backup"
  run_tar tar -xzf "$BACKUP_PATH/aegis-backup.tar.gz" -C /
  local rc=$?

  [ "$rc" -eq 0 ] \
    && ok "Aegis restored from $BACKUP_DATE" \
    || fail "Aegis — archive extraction failed"
}

restore_gitea() {
  step "Gitea"

  local archive
  archive=$(resolve_archive "gitea-data.tar.gz")

  spinner_start "Stopping Gitea"
  container_stop gitea

  spinner_start "Restoring Gitea data"
  run_tar tar -xzf "$archive" -C /
  local rc=$?

  spinner_start "Starting Gitea"
  container_start gitea

  [ "$rc" -eq 0 ] \
    && ok "Gitea restored from $(basename "$(dirname "$archive")")" \
    || fail "Gitea — archive extraction failed"
}

restore_gitea_runner() {
  step "Gitea Act Runner"

  spinner_start "Stopping runner"
  container_stop gitea-act-runner

  spinner_start "Restoring runner state"
  run_tar tar -xzf "$BACKUP_PATH/gitea-runner.tar.gz" -C /
  local rc=$?

  spinner_start "Starting runner"
  container_start gitea-act-runner

  [ "$rc" -eq 0 ] \
    && ok "Gitea Act Runner restored from $BACKUP_DATE" \
    || fail "Gitea Act Runner — archive extraction failed"
}

restore_ghost() {
  step "Ghost"

  if [ ! -f "$ENV_GHOST" ]; then
    fail "Ghost — .env not found: $ENV_GHOST"
    return
  fi
  # shellcheck source=/dev/null
  source "$ENV_GHOST"

  local content_archive db_dump
  content_archive=$(resolve_archive "ghost-content.tar.gz")
  db_dump=$(resolve_archive "ghost-db.sql")
  local content_date db_date
  content_date=$(basename "$(dirname "$content_archive")")
  db_date=$(basename "$(dirname "$db_dump")")
  # Newer DB than content is normal (dump runs nightly, content only on change).
  if [[ "$db_date" < "$content_date" ]]; then
    warn "Ghost — DB ($db_date) is older than the content archive ($content_date)"
  fi

  # Same pattern as Nextcloud: the app is stopped, the DB container stays up
  # for the import. The dump comes from mysqldump with its default
  # DROP TABLE IF EXISTS, so it replaces the tables in place.
  spinner_start "Stopping Ghost"
  container_stop ghost

  spinner_start "Restoring content"
  run_tar tar -xzf "$content_archive" -C /
  local rc_files=$?

  spinner_start "Restoring database"
  docker exec -i ghost-db mysql -u ghost -p"$GHOST_DB_PASSWORD" ghost \
    < "$db_dump" >> "$LOG" 2>&1
  local rc_db=$?

  spinner_start "Starting Ghost"
  container_start ghost

  if [ "$rc_files" -eq 0 ] && [ "$rc_db" -eq 0 ]; then
    ok "Ghost restored from $content_date"
  else
    fail "Ghost — restore failed (files: $rc_files, db: $rc_db)"
  fi
}

restore_nextcloud() {
  step "Nextcloud"

  # Load MYSQL_PASSWORD from the stack env file.
  if [ ! -f "$ENV_NEXTCLOUD" ]; then
    fail "Nextcloud — .env not found: $ENV_NEXTCLOUD"
    return
  fi
  # shellcheck source=/dev/null
  source "$ENV_NEXTCLOUD"

  # Resolve archive paths — may point to an older snapshot if this date was skipped.
  local data_archive db_dump
  data_archive=$(resolve_archive "nextcloud-data.tar")
  db_dump=$(resolve_archive "nextcloud-db.sql")
  local data_date db_date
  data_date=$(basename "$(dirname "$data_archive")")
  db_date=$(basename "$(dirname "$db_dump")")

  # The DB is dumped every night, the file archive only when files changed, so
  # a newer DB than data archive is the normal case. A DB OLDER than the data
  # is not - only possible after a failed dump - so warn about that one.
  if [[ "$db_date" < "$data_date" ]]; then
    warn "Nextcloud — DB ($db_date) is older than the data archive ($data_date)"
  fi

  # Enable maintenance mode while the app container is still running so occ
  # can reach the database. The trap ensures it's lifted even on early exit.
  spinner_start "Enabling maintenance mode"
  run docker exec -u www-data nextcloud php occ maintenance:mode --on

  local maintenance_active=1
  # shellcheck disable=SC2064
  trap "[ \$maintenance_active -eq 1 ] && \
        docker exec -u www-data nextcloud php occ maintenance:mode --off \
        >> \"$LOG\" 2>&1; \
        echo '  maintenance mode force-disabled by trap' >> \"$LOG\"" RETURN

  # Stop the app container — DB and Redis stay up during the restore.
  spinner_start "Stopping Nextcloud app"
  container_stop nextcloud

  # Nextcloud data is archived uncompressed (tar, not tar.gz).
  spinner_start "Restoring data files (may take several minutes)"
  run_tar tar -xf "$data_archive" -C /
  local rc_files=$?

  # Import the MariaDB dump into the running DB container via stdin.
  spinner_start "Restoring database"
  docker exec -i nextcloud-db mariadb \
    -u nextcloud -p"$MYSQL_PASSWORD" nextcloud \
    < "$db_dump" >> "$LOG" 2>&1
  local rc_db=$?

  spinner_start "Starting Nextcloud app"
  container_start nextcloud

  spinner_start "Disabling maintenance mode"
  run docker exec -u www-data nextcloud php occ maintenance:mode --off
  maintenance_active=0
  trap - RETURN

  if [ "$rc_files" -eq 0 ] && [ "$rc_db" -eq 0 ]; then
    ok "Nextcloud restored from $data_date"
  else
    fail "Nextcloud — restore failed (files: $rc_files, db: $rc_db)"
  fi
}

restore_immich() {
  step "Immich"

  local upload_archive upload_date
  upload_archive=$(resolve_archive "immich-upload.tar")
  upload_date=$(basename "$(dirname "$upload_archive")")

  # Only the server is stopped so it can't write to the upload directory
  # mid-restore.
  spinner_start "Stopping Immich server"
  container_stop immich-server

  spinner_start "Restoring upload directory (may take a while)"
  run_tar tar -xf "$upload_archive" -C /
  local rc_files=$?

  spinner_start "Starting Immich server"
  container_start immich-server

  # The database is deliberately NOT restored here. immich-db.sql comes from
  # pg_dumpall without --clean: it creates the role and the database. Piped
  # into the running, populated database (what this function used to do),
  # every statement fails with "already exists", psql still exits 0, and the
  # restore reported success without changing anything. Immich documents a
  # restore into an empty database only - manual procedure in RUNBOOK.md.
  if [ "$rc_files" -eq 0 ]; then
    warn "Immich — uploads restored from $upload_date; database NOT restored (manual, see RUNBOOK.md)"
    ok "Immich uploads restored from $upload_date"
  else
    fail "Immich — upload archive extraction failed"
  fi
}

restore_jobiris() {
  step "JobIris"

  # The board holds the SQLite archive open; the monitor runs as a one-shot
  # from systemd timers (jobiris-monitor-*.timer) and can be ignored for the
  # few seconds this takes.
  spinner_start "Stopping JobIris board"
  container_stop jobiris-board

  spinner_start "Restoring data"
  run_tar tar -xzf "$BACKUP_PATH/jobiris.tar.gz" -C /
  local rc=$?

  spinner_start "Starting JobIris board"
  container_start jobiris-board

  [ "$rc" -eq 0 ] \
    && ok "JobIris restored from $BACKUP_DATE" \
    || fail "JobIris — archive extraction failed"
}

restore_grafana() {
  step "Grafana"

  spinner_start "Stopping Grafana"
  container_stop grafana

  # Bind mount /mnt/codex/grafana. This used to unpack into a named volume
  # grafana-data that does not exist - docker created an empty one and the
  # restore reported success.
  spinner_start "Restoring data"
  run_tar tar -xzf "$BACKUP_PATH/grafana-data.tar.gz" -C /
  local rc=$?

  spinner_start "Starting Grafana"
  container_start grafana

  [ "$rc" -eq 0 ] \
    && ok "Grafana restored from $BACKUP_DATE" \
    || fail "Grafana — archive extraction failed"
}

restore_exporter_tokens() {
  step "Exporter tokens"

  # tado, netatmo and midea in one archive. Midea is seasonal (profile), so
  # only exporters running right now are stopped and started again.
  local running=()
  local name
  for name in tado-exporter netatmo-exporter midea-exporter; do
    container_running "$name" && running+=("$name")
  done

  spinner_start "Stopping exporters"
  for name in "${running[@]}"; do container_stop "$name"; done

  spinner_start "Restoring token stores"
  run_tar tar -xzf "$BACKUP_PATH/exporter-tokens.tar.gz" -C /
  local rc=$?

  spinner_start "Starting exporters"
  for name in "${running[@]}"; do container_start "$name"; done

  [ "$rc" -eq 0 ] \
    && ok "Exporter tokens restored from $BACKUP_DATE" \
    || fail "Exporter tokens — archive extraction failed"
}

restore_stack_configs() {
  step "Stack configs"

  # ~/stacks/ is a symlink to ~/homelab-infra/mnemosyne/stacks/.
  # tar follows the symlink and extracts to the real path, so this will
  # overwrite the homelab-infra working tree. The symlink must exist.
  warn "Stack configs — overwrites homelab-infra/mnemosyne/stacks/ via symlink."

  spinner_start "Restoring stack configs"
  run_tar tar -xzf "$BACKUP_PATH/stacks-config.tar.gz" -C /
  local rc=$?

  [ "$rc" -eq 0 ] \
    && ok "Stack configs restored from $BACKUP_DATE" \
    || fail "Stack configs — archive extraction failed"
}


# ── Summary box ───────────────────────────────────────────────────────────────

print_summary() {
  local status_text status_color
  if [ "$ERRORS" -eq 0 ]; then
    status_text="✓ All $RESTORED service(s) restored successfully"
    status_color="$GREEN"
  else
    status_text="✗ $ERRORS failure(s) — check $LOG"
    status_color="$RED"
  fi

  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
  printf "${BOLD}║${RESET}  %-52s${BOLD}║${RESET}\n" "Summary"
  echo -e "${BOLD}╠══════════════════════════════════════════════════════╣${RESET}"
  printf "${BOLD}║${RESET}  %-52s${BOLD}║${RESET}\n" "Snapshot : $BACKUP_DATE"
  printf "${BOLD}║${RESET}  %-52s${BOLD}║${RESET}\n" "Restored : $RESTORED service(s)"

  if [ "$ERRORS" -gt 0 ]; then
    printf "${BOLD}║${RESET}  ${RED}%-52s${RESET}${BOLD}║${RESET}\n" "Errors   : $ERRORS"
  else
    printf "${BOLD}║${RESET}  ${GREEN}%-52s${RESET}${BOLD}║${RESET}\n" "Errors   : 0"
  fi

  echo -e "${BOLD}╠══════════════════════════════════════════════════════╣${RESET}"
  printf "${BOLD}║${RESET}  ${status_color}%-52s${RESET}${BOLD}║${RESET}\n" "$status_text"
  echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
  echo ""
}


# ── Main ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
printf "${BOLD}║${RESET}  %-52s${BOLD}║${RESET}\n" "Mnemosyne Restore — $(date '+%Y-%m-%d %H:%M')"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"

# ── Pre-flight checks ─────────────────────────────────────────────────────────

# The default path must be a mountpoint: an unmounted /mnt/backup is an empty
# directory on the root disk and would read as "no snapshots". An explicit
# BACKUP_DIR override (e.g. a restic restore in a scratch directory) only
# has to exist.
if [ "$BACKUP_DIR" = "/mnt/backup" ]; then
  if ! mountpoint -q "$BACKUP_DIR"; then
    echo -e "${RED}${BOLD}  ERROR: $BACKUP_DIR is not mounted. Is the backup HDD (WD Red) connected?${RESET}"
    exit 1
  fi
elif [ ! -d "$BACKUP_DIR" ]; then
  echo -e "${RED}${BOLD}  ERROR: BACKUP_DIR=$BACKUP_DIR does not exist.${RESET}"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo -e "${RED}${BOLD}  ERROR: Docker is not running.${RESET}"
  exit 1
fi

# ── Interactive steps ─────────────────────────────────────────────────────────

select_snapshot

echo ""
echo -e "  ${BOLD}Snapshot :${RESET} $BACKUP_DATE"
echo -e "  ${BOLD}Path     :${RESET} $BACKUP_PATH"
echo -e "  ${BOLD}Size     :${RESET} $(du -sh "$BACKUP_PATH" | cut -f1)"

service_selection_menu
check_backup_files
confirm_restore

# ── Execute selected restores ─────────────────────────────────────────────────

# Count selected services so step() can show [N/TOTAL] progress.
for id in "${SERVICE_IDS[@]}"; do
  [ "${SELECTED[$id]}" -eq 1 ] && ((TOTAL_STEPS++))
done

{
  echo ""
  echo "=== Restore started: $(date) ==="
  echo "Snapshot: $BACKUP_DATE"
} >> "$LOG"

echo ""
echo -e "${BOLD}  Starting restore — $TOTAL_STEPS service(s)...${RESET}"

[ "${SELECTED[vaultwarden]}"     -eq 1 ] && restore_vaultwarden
[ "${SELECTED[caddy]}"           -eq 1 ] && restore_caddy
[ "${SELECTED[calibre-library]}" -eq 1 ] && restore_calibre_library
[ "${SELECTED[calibre-web]}"     -eq 1 ] && restore_calibre_web
[ "${SELECTED[kosync]}"          -eq 1 ] && restore_kosync
[ "${SELECTED[syncthing]}"       -eq 1 ] && restore_syncthing
[ "${SELECTED[aegis]}"           -eq 1 ] && restore_aegis
[ "${SELECTED[gitea]}"           -eq 1 ] && restore_gitea
[ "${SELECTED[gitea-runner]}"    -eq 1 ] && restore_gitea_runner
[ "${SELECTED[ghost]}"           -eq 1 ] && restore_ghost
[ "${SELECTED[nextcloud]}"       -eq 1 ] && restore_nextcloud
[ "${SELECTED[immich]}"          -eq 1 ] && restore_immich
[ "${SELECTED[jobiris]}"         -eq 1 ] && restore_jobiris
[ "${SELECTED[grafana]}"         -eq 1 ] && restore_grafana
[ "${SELECTED[exporter-tokens]}" -eq 1 ] && restore_exporter_tokens
[ "${SELECTED[stack-configs]}"   -eq 1 ] && restore_stack_configs

# ── Summary ───────────────────────────────────────────────────────────────────

print_summary

{
  echo "=== Restore finished: $(date) ==="
  echo "Restored: $RESTORED   Errors: $ERRORS"
} >> "$LOG"

[ "$ERRORS" -eq 0 ] && exit 0 || exit 1