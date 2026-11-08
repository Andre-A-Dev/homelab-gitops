#!/bin/bash
# backup-zephyros.sh — backs up Pi-hole, Unbound and misc configs to Mnemosyne via Tailscale

MNEMOSYNE_IP="100.x.x.x"       # Tailscale IP of Mnemosyne — update after setup
BACKUP_USER="<user>"
# On Mnemosyne this key is forced into `rrsync -wo /mnt/codex/backups/zephyros`
# (zephyros/README.md): paths are relative to that directory, and no shell
# command runs. Mnemosyne's backup-services.sh prunes old copies.
DATE=$(date +%F)
BACKUP_DIR="/tmp/zephyros-backup-$DATE"
LOG="/var/log/backup-zephyros.log"
SSH_KEY="/home/<user>/.ssh/backup_key"
CADDY_DIR="/home/<user>/homelab-infra/zephyros/caddy"

echo "=== Backup started: $(date) ===" >> $LOG

mkdir -p "$BACKUP_DIR/pihole" "$BACKUP_DIR/unbound" "$BACKUP_DIR/misc"

# Pi-hole config
echo "Saving: Pi-hole..." >> $LOG
# Pi-hole v6 keeps local DNS records in pihole.toml; custom.list no longer exists.
sudo cp /etc/pihole/pihole.toml "$BACKUP_DIR/pihole/" 2>> $LOG

# Unbound config
echo "Saving: Unbound..." >> $LOG
sudo cp /etc/unbound/unbound.conf.d/pi-hole.conf "$BACKUP_DIR/unbound/" 2>> $LOG

# Misc (msmtp, apticron, pihole6-exporter unit + drop-in + env with the Pi-hole API password)
echo "Saving: Misc..." >> $LOG
sudo cp /etc/msmtprc "$BACKUP_DIR/misc/" 2>> $LOG
# apticron.conf is a link into the repo; only the recipients are host-local
sudo cp /etc/apticron/apticron.local.conf "$BACKUP_DIR/misc/" 2>> $LOG
sudo cp /etc/systemd/system/pihole6-exporter.service "$BACKUP_DIR/misc/" 2>> $LOG
sudo cp -r /etc/systemd/system/pihole6-exporter.service.d "$BACKUP_DIR/misc/" 2>> $LOG
sudo cp /etc/pihole6-exporter.env "$BACKUP_DIR/misc/" 2>> $LOG

# Caddy: the internal CA. Caddyfile and compose are in Git; the CA is not, and
# losing it means re-importing a new root certificate on every client device.
# Absolute path: the script runs from root's crontab, where ~ is /root - the
# old ~/stacks/caddy path never matched and every backup's caddy/ was empty.
echo "Saving: Caddy CA..." >> $LOG
mkdir -p "$BACKUP_DIR/caddy"
sudo cp -a "$CADDY_DIR/data/caddy/pki" "$BACKUP_DIR/caddy/" 2>> $LOG
# Fix permissions for rsync
sudo chown -R "<user>:<user>" "$BACKUP_DIR"

# Transfer to Mnemosyne via Tailscale
echo "Transferring to Mnemosyne ($MNEMOSYNE_IP)..." >> $LOG
rsync -az --delete \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$BACKUP_DIR/" \
    "$BACKUP_USER@$MNEMOSYNE_IP:$DATE/" >> $LOG 2>&1

# Clean up local temp dir
rm -rf "$BACKUP_DIR"

echo "=== Backup finished: $(date) ===" >> $LOG
