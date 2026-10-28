#!/bin/bash
# =============================================================================
# backup-boreas.sh  →  /usr/local/bin/backup-boreas.sh (symlink into the repo)
# =============================================================================
# Pushes Boreas' configuration to Mnemosyne (/mnt/codex/backups/boreas/<date>/).
# Runs nightly from root's crontab. backup-services.sh on Mnemosyne archives
# the newest of these directories into /mnt/backup, from where restic ships it
# offsite, and fails its "Pi configs" step when the newest one is too old.
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
BACKUP_PATH="/mnt/codex/backups/boreas"
DATE=$(date +%Y-%m-%d)
LOG="/var/log/backup-boreas.log"
SSH_KEY="/home/<user>/.ssh/backup_key"
RETENTION_DAYS=14

FAILED=0

# Runs rsync and records a failure instead of letting it vanish in the log.
copy() {
  local label="$1"
  shift
  echo "Backing up $label..." >> "$LOG"
  if ! rsync -az -e "ssh -i $SSH_KEY" "$@" 2>> "$LOG"; then
    echo "ERROR: $label failed" >> "$LOG"
    FAILED=1
  fi
}

echo "=== Backup started: $(date) ===" >> "$LOG"

if ! ssh -i "$SSH_KEY" "$BACKUP_HOST" "mkdir -p $BACKUP_PATH/$DATE" 2>> "$LOG"; then
  echo "ERROR: cannot create $BACKUP_PATH/$DATE on Mnemosyne - aborting" >> "$LOG"
  exit 1
fi

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
  "$BACKUP_HOST:$BACKUP_PATH/$DATE/pihole/"

copy "Unbound" \
  /etc/unbound/unbound.conf.d/ \
  "$BACKUP_HOST:$BACKUP_PATH/$DATE/unbound/"

# pihole6-exporter: the unit is not in the repo and the env file holds the
# Pi-hole API password - without both, the exporter has to be rebuilt by hand.
copy "msmtp, apticron, pihole6-exporter" \
  /etc/msmtprc \
  /etc/apticron/apticron.conf \
  /etc/pihole6-exporter.env \
  /etc/systemd/system/pihole6-exporter.service \
  "$BACKUP_HOST:$BACKUP_PATH/$DATE/misc/"

echo "Cleaning up backups older than $RETENTION_DAYS days..." >> "$LOG"
ssh -i "$SSH_KEY" "$BACKUP_HOST" \
  "find $BACKUP_PATH -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +$RETENTION_DAYS -exec rm -rf {} +" 2>> "$LOG"

if [ "$FAILED" -ne 0 ]; then
  echo "=== Backup FAILED: $(date) ===" >> "$LOG"
  echo "" >> "$LOG"
  exit 1
fi
echo "=== Backup completed: $(date) ===" >> "$LOG"
echo "" >> "$LOG"
