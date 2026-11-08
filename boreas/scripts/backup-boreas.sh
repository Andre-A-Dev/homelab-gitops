#!/bin/bash
# =============================================================================
# backup-boreas.sh  →  /usr/local/bin/backup-boreas.sh (symlink into the repo)
# =============================================================================
# Pushes Boreas' configuration to Mnemosyne (/mnt/codex/backups/boreas/<date>/).
# Runs nightly from root's crontab. backup-services.sh on Mnemosyne archives
# the newest of these directories into /mnt/backup, from where restic ships it
# offsite, and fails its "Pi configs" step when the newest one is too old.
# On Mnemosyne the key is forced into `rrsync -wo /mnt/codex/backups/boreas`
# (boreas/README.md): remote paths are relative to that directory, no shell
# command runs, and backup-services.sh prunes old copies.
#
# What is NOT copied from /etc/pihole, and why:
#   pihole-FTL.db*      query log (~1 GB), written continuously - a copy taken
#                       while FTL runs is not consistent, and it is history,
#                       not configuration. It made every backup 1.1 GB.
#   gravity_old.db,     previous gravity generation and its backups - the
#   gravity_backups/    current gravity.db holds the lists, groups and domains
#   listsCache/,        caches Pi-hole re-downloads
#   macvendor.db
#
# Exit code: 0 if every copy succeeded, 1 otherwise (details in the log).
# =============================================================================

BACKUP_HOST="<user>@192.168.1.10"
DATE=$(date +%Y-%m-%d)
LOG="/var/log/backup-boreas.log"
SSH_KEY="/home/<user>/.ssh/backup_key"

FAILED=0

# Runs rsync and records a failure instead of letting it vanish in the log.
# --mkpath creates the dated directory (no shell for mkdir behind rrsync).
copy() {
  local label="$1"
  shift
  echo "Backing up $label..." >> "$LOG"
  if ! rsync -az --mkpath -e "ssh -i $SSH_KEY" "$@" 2>> "$LOG"; then
    echo "ERROR: $label failed" >> "$LOG"
    FAILED=1
  fi
}

echo "=== Backup started: $(date) ===" >> "$LOG"

# --delete-excluded: a second run on the same day writes into the same dated
# directory - without it, a query log copied by an older version stays there.
copy "Pi-hole" \
  --delete --delete-excluded \
  --exclude='pihole-FTL.db*' \
  --exclude='gravity_old.db' \
  --exclude='gravity_backups/' \
  --exclude='listsCache/' \
  --exclude='macvendor.db' \
  /etc/pihole/ \
  "$BACKUP_HOST:$DATE/pihole/"

copy "Unbound" \
  /etc/unbound/unbound.conf.d/ \
  "$BACKUP_HOST:$DATE/unbound/"

# pihole6-exporter: the unit and its drop-in are not in the repo and the env
# file holds the Pi-hole API password - without them, the exporter has to be
# rebuilt by hand. No trailing slash: rsync copies the drop-in directory itself.
# apticron: apticron.conf is a link into the repo; only the recipients in
# apticron.local.conf are host-local.
copy "msmtp, apticron, pihole6-exporter" \
  /etc/msmtprc \
  /etc/apticron/apticron.local.conf \
  /etc/pihole6-exporter.env \
  /etc/systemd/system/pihole6-exporter.service \
  /etc/systemd/system/pihole6-exporter.service.d \
  "$BACKUP_HOST:$DATE/misc/"

if [ "$FAILED" -ne 0 ]; then
  echo "=== Backup FAILED: $(date) ===" >> "$LOG"
  echo "" >> "$LOG"
  exit 1
fi
echo "=== Backup completed: $(date) ===" >> "$LOG"
echo "" >> "$LOG"
