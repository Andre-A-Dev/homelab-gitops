# Runbook

Common operational procedures. No explanations — just the commands.

---

## Update a Stack

```bash
cd ~/stacks/<stack>
docker compose pull
docker compose up -d
docker compose logs -f --tail 50
```

---

## Reload Caddy Config

Use after editing the Caddyfile. If the editor saves atomically (new file + rename), the bind-mount inode breaks — `reload` will silently read the stale config. Recreate the container instead (`down` + `up -d`); `restart` keeps the old inode too.

```bash
# Safe for all cases
cd ~/stacks/caddy && docker compose down && docker compose up -d

# Only if you're certain the file was edited in-place (e.g. sed -i)
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
docker logs caddy --tail 20
```

---

## Add a New Internal Service to Caddy

1. Add a block to `~/stacks/caddy/Caddyfile`:

```
service.home {
  tls internal
  reverse_proxy <container-name>:<internal-port>
}
```

2. Add the service to the `caddy_proxy` network in its `docker-compose.yml`:

```yaml
networks:
  caddy_proxy:
    external: true
```

3. Add a DNS record in Pi-hole: **Local DNS → DNS Records** → `service.home` → `192.168.1.10`

4. Restart Caddy:

```bash
cd ~/stacks/caddy && docker compose down && docker compose up -d
```

---

## Run Backup Manually

```bash
sudo /usr/local/bin/backup-services.sh
tail -50 /var/log/backup-services.log
```

Back up a single service (e.g. after a config change mid-week):

```bash
sudo /usr/local/bin/backup-services.sh --only=vaultwarden
```

Force all services regardless of change detection:

```bash
sudo /usr/local/bin/backup-services.sh --force --overwrite
```

Skip the restic offsite trigger for this run only (e.g. bandwidth needed elsewhere):

```bash
sudo /usr/local/bin/backup-services.sh --no-offsite
```

---

## Check Offsite Backup (restic, Hetzner Storage Box)

Last runs and their result (backup trigger around 02:20, fallback timer 06:00):

```bash
systemctl list-timers restic-offsite.timer restic-maintenance.timer --no-pager
journalctl -u restic-offsite --since -2d --no-pager | grep -E "backup OK|ERROR|SKIP|finished"
journalctl -u restic-maintenance --since -8d --no-pager | grep -E "OK|ERROR|finished"
```

Metrics (script view and systemd view; `exit_code` 0 and a fresh timestamp = fine):

```bash
grep -v "^#" /var/lib/node_exporter/textfile_collector/restic_offsite.prom
grep -v "^#" /var/lib/node_exporter/textfile_collector/restic_offsite_unit.prom
grep -v "^#" /var/lib/node_exporter/textfile_collector/restic_maintenance.prom
```

Exit codes of `restic-offsite.sh`: 0 ok · 1 config/preflight error · 2 backup failed · 3 backup ok, `forget` failed (retention stalled) · 75 skipped, repo lock held by another restic run (counts as success).

Run the offsite backup now, through the same unit as the nightly trigger:

```bash
sudo systemctl start --no-block restic-offsite.service
journalctl -u restic-offsite -f
```

Run restic commands by hand — always as root with the same environment the units use. `R` is a shell function for this session:

```bash
sudo -i
R() { ( set -a; . /etc/restic/restic-offsite.env; set +a
        restic -o "sftp.command=$RESTIC_SFTP_COMMAND" "$@" ); }

R snapshots                  # list snapshots
R ls latest /mnt/backup      # top level of the newest snapshot
R stats latest               # size of the newest snapshot
```

Maintenance commands (`forget`, `prune`, `unlock`, `check`) must hold the shared lock, or they can collide with a running backup:

```bash
flock -n /var/run/restic-repo.lock bash -c 'set -a; . /etc/restic/restic-offsite.env; set +a
  restic -o "sftp.command=$RESTIC_SFTP_COMMAND" check' || echo "lock held or check failed"
```

Repository locked (`repository is already locked by PID …`): the next `restic-offsite.sh` run clears stale locks itself (`restic unlock`, not `--remove-all`). Only intervene if a lock is still reported after that run.

---

## Verify Backup

Full integrity check (opens every archive, runs SQLite and MariaDB checks):

```bash
sudo /usr/local/bin/verify-backup.sh
```

Quick check — existence and size only, skips `tar -tzf` (faster, used by cron):

```bash
sudo /usr/local/bin/verify-backup.sh --quick
```

Verify a specific date instead of the latest:

```bash
sudo /usr/local/bin/verify-backup.sh --date=2026-06-01
```

Check backup health via Prometheus metrics (non-zero = problem):

```bash
cat /var/lib/node_exporter/textfile_collector/backup.prom | grep -v "^#"
cat /var/lib/node_exporter/textfile_collector/backup_verify.prom | grep -v "^#"
```

---

## Restore from Backup

> Requires the backup HDD (WD Red 4 TB) to be mounted at `/mnt/backup`. Check with
> `mountpoint -q /mnt/backup && echo mounted || echo NOT MOUNTED`.
> Backup HDD lost as well? Pull the data from offsite first: [Restore from Offsite (restic)](#restore-from-offsite-restic).

Interactive restore — presents a snapshot selector and per-service toggle menu.
No data is touched until you type `yes`:

```bash
sudo /usr/local/bin/restore-services.sh
```

Every archive stores the absolute source path without the leading `/`; the
script extracts with `-C /` straight into the live bind mounts. A different
source directory (e.g. data restored from restic into a scratch path) works
via `sudo BACKUP_DIR=/path /usr/local/bin/restore-services.sh` — only the
default `/mnt/backup` has to be a mountpoint.

Notable per-service behavior:

- **Vaultwarden** — the consistent `sqlite3 .backup` copy replaces the
  database; live `-wal`/`-shm` files are removed first.
- **Nextcloud** — DB container stays up for the SQL import; only the app
  container is stopped. Maintenance mode is toggled around the restore.
- **Ghost** — app stopped, `ghost-db.sql` imported into the running `ghost-db`.
- **Immich** — **uploads only.** The database needs the manual procedure below.
- **Syncthing** — stops and starts the host service `syncthing@andre`.
  Other devices may still hold newer files and sync them back afterwards.
- **Exporter tokens** — restarts only the exporters that were running
  (Midea is seasonal).
- **Calibre** — `calibre-web` is stopped during library restore to avoid
  read/write conflicts.
- **Stack configs** — extracted via the `~/stacks/` symlink, which overwrites
  the homelab-infra working tree.
- **Skipped snapshots** — if a service has no archive on the selected date, the
  script follows the `.SKIPPED` marker to the last real archive automatically.

### Restore a single service

At the service selection menu, toggle only the service you need — press its
number, then Enter to confirm.

### After a Nextcloud restore

Run a file scan to sync the DB with the restored data:

```bash
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ maintenance:mode --off  # if still on
```

### After restoring stack configs

The homelab-infra working tree has been overwritten. Restart affected stacks:

```bash
cd ~/stacks/<affected-stack> && docker compose up -d
```

### Pi configs (Boreas, Zephyros)

Not in the restore menu - the target is another host. `pi-configs.tar.gz` holds the newest backup each Pi pushed to `/mnt/codex/backups/<host>/<date>/` (Pi-hole config incl. `gravity.db`, Unbound, msmtp, apticron, pihole6-exporter unit and env; Zephyros also its Caddy CA). Without the query log `pihole-FTL.db`.

```bash
mkdir -p /tmp/pi && tar -xzf /mnt/backup/<YYYY-MM-DD>/pi-configs.tar.gz -C /tmp/pi
ls /tmp/pi/mnt/codex/backups/          # boreas/<date>/, zephyros/<date>/
# copy the needed files to the Pi (scp), put them in place with sudo, then:
#   sudo systemctl restart pihole-FTL unbound pihole6-exporter
```

### Immich database (manual)

`immich-db.sql` comes from `pg_dumpall` without `--clean`: it creates the role
and the database itself. Imported into the running, populated database every
statement fails with "already exists" — and `psql` still exits 0. Immich only
supports restoring into an **empty** database. Untested end to end; adapted from
the Immich docs to this dump.

```bash
cd ~/stacks/immich
set -a; . ./.env; set +a                     # DB_USERNAME, DB_DATABASE_NAME
docker compose down

# Keep the current database aside instead of deleting it
sudo mv /mnt/codex/immich/db /mnt/codex/immich/db.before-restore
sudo mkdir /mnt/codex/immich/db

# Fresh database container only; wait until it accepts connections
docker compose create && docker start immich-db
until docker exec immich-db pg_isready -U "$DB_USERNAME" >/dev/null 2>&1; do sleep 2; done

# The fresh init created the database already - drop it, the dump recreates it
docker exec immich-db psql -U "$DB_USERNAME" -d postgres -c "DROP DATABASE \"$DB_DATABASE_NAME\";"

# Import. The search_path rewrite is from the Immich restore docs.
sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" \
  /mnt/backup/<YYYY-MM-DD>/immich-db.sql \
  | docker exec -i immich-db psql -U "$DB_USERNAME" -d postgres 2>&1 | tee /tmp/immich-restore.log | grep ERROR
# Expected: only 'role "<DB_USERNAME>" already exists'. Anything else: stop and check.

docker compose up -d
```

If it fails: `docker compose down`, put `db.before-restore` back, `docker compose up -d`.

---

## Restore from Offsite (restic)

For the case where the Pi and/or the backup HDD are gone. Untested end to end — the steps follow restic's documented behaviour and the scripts.

Needed from outside Mnemosyne: repository address (`RESTIC_REPOSITORY`), the restic password, and an SSH key accepted by the Storage Box.

1. Prepare a drive and mount it at `/mnt/backup` (ext4, UUID in `/etc/fstab`, `nofail`).
2. Install restic, then restore the newest day as root. The snapshot stores absolute paths, so `--target /` writes straight back to `/mnt/backup/<YYYY-MM-DD>/`:

```bash
export RESTIC_REPOSITORY='sftp:<user>@<user>.your-storagebox.de:restic-repo'
export RESTIC_PASSWORD_FILE=/path/to/password
SFTP='sftp.command=ssh -i /path/to/key -p 23 <user>@<user>.your-storagebox.de -s sftp'

restic -o "$SFTP" snapshots
restic -o "$SFTP" ls latest /mnt/backup                     # pick the newest date
restic -o "$SFTP" restore latest --target / --include /mnt/backup/<YYYY-MM-DD>
```

3. Check for `.SKIPPED` markers in that directory. Each one names the date of the last real archive of that service — restore those dates too (same command, other `--include`).
4. Continue with [Restore from Backup](#restore-from-backup).

Single file without a full restore, e.g. a database dump:

```bash
restic -o "$SFTP" dump latest /mnt/backup/<YYYY-MM-DD>/vaultwarden-db.sqlite3 > /tmp/vaultwarden-db.sqlite3
```

---

## Export Grafana Dashboards

Run after making changes to dashboards in the Grafana UI:

```bash
~/homelab-infra/mnemosyne/scripts/export-grafana-dashboards.sh
```

Then commit and push the updated JSON files from Windows.

---

## Import Caddy Root Certificate on a New Device

Extract the certificate from the running container:

```bash
docker cp caddy:/data/caddy/pki/authorities/local/root.crt ~/caddy-root.crt
```

**Windows:** Double-click `caddy-root.crt` → Install → Local Machine → Trusted Root Certification Authorities.

**Android:** Copy via Syncthing or ADB → Settings → Security → Install certificate → CA certificate. Use Brave or Chrome for `.home` access — Firefox on Android uses its own certificate store and ignores the system CA.

**CachyOS:** `trust extract-compat` after adding the cert to the system trust store. Electron apps (VSCodium, Bitwarden desktop) ignore the system CA — add to the NSS database: `certutil -d sql:$HOME/.pki/nssdb -A -t "CT,," -n "Caddy Local CA" -i ~/caddy-root.crt`.

---

## Change Pi-hole Password

```bash
# On Boreas
ssh youruser@192.168.1.11
sudo pihole setpassword

# On Zephyros (via Tailscale)
ssh youruser@100.y.y.y
sudo pihole setpassword
```

Update the exporter and restart:

```bash
sudo nano /etc/pihole6-exporter.env
sudo systemctl restart pihole6-exporter
sudo systemctl status pihole6-exporter
```

---

## Check Webhook Status

```bash
# Handler log
tail -30 /var/log/webhook-handler.log

# Webhook service
journalctl -u webhook -f
sudo systemctl status webhook
```

---

## Restart a Stuck Container

```bash
docker ps -a                          # find the container
docker restart <container-name>
docker logs <container-name> --tail 50
```

---

## Recover from Docker Layer Cache Corruption

Symptom: containers exit with code 255, `RWLayer ... is unexpectedly nil` in logs after an unclean reboot.

```bash
cd ~/stacks/<stack>
docker compose down && docker compose up -d
```

If that fails (corruption extends into the content store):

```bash
docker system prune -af
docker compose pull
docker compose up -d
```

---

## Check All Prometheus Targets

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data['data']['activeTargets']:
    print(t['labels'].get('job'), t['labels'].get('instance'), t['health'], t.get('lastError',''))
"
```

---

## Mnemosyne Full Reboot

```bash
sudo reboot
```

Services with `restart: unless-stopped` come back automatically. Check afterwards:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
systemctl status syncthing@youruser
systemctl status tailscaled
systemctl status webhook
systemctl status fan-metrics.timer
```

---

## Immich

### Check ML job queue

```bash
docker logs immich-machine-learning --tail 50
```

### Restart ML worker (if stuck)

```bash
cd ~/stacks/immich
docker compose restart immich-machine-learning
```

### Re-run Smart Search / Face Detection

Use the Immich web UI: **Administration → Jobs → Smart Search / Face Detection → Run All**.

> ML jobs run at concurrency 1. Let them complete overnight on the Pi 5 — do not raise concurrency.

### Check Immich database

```bash
docker exec -it immich-postgres psql -U postgres immich -c "\dt"
```

---

## Fan Monitoring (Mnemosyne)

### Check current fan level

```bash
cat /sys/class/thermal/cooling_device0/cur_state   # 0–4
cat /sys/class/thermal/thermal_zone0/temp           # millidegrees Celsius
```

### Check textfile collector output

```bash
cat /var/lib/node_exporter/textfile_collector/fan.prom
```

### Check systemd timer

```bash
systemctl status fan-metrics.timer
systemctl status fan-metrics.service
journalctl -u fan-metrics.service --since "1 hour ago"
```

### Run manually

```bash
sudo /usr/local/bin/fan-metrics.sh
```

---

## Shelly Exporter

### Check exporter health

```bash
curl -s http://localhost:9117/health
curl -s http://localhost:9117/metrics | grep shelly_device_online
```

### Restart

```bash
cd ~/stacks/monitoring
docker compose restart shelly-exporter
docker compose logs shelly-exporter -f --tail 30
```

### Add a new device

Edit `~/stacks/monitoring/.env` and append to `SHELLY_DEVICES`:

```
SHELLY_DEVICES=existing-device:192.168.x.x:2,new-device:192.168.x.x:3
```

Format: `name:host:gen` — gen `1` for Gen1, `2` or `3` for Gen2/3.

```bash
docker compose up -d
curl -s http://localhost:9117/metrics | grep shelly_device_online
```

---

## Midea Exporter

### Check exporter health

```bash
curl -s http://localhost:9116/metrics | grep midea_device_online
docker logs midea-exporter --tail 30
```

### Restart

```bash
cd ~/stacks/monitoring
docker compose restart midea-exporter
docker compose logs midea-exporter -f --tail 30
```

### Reset cached credentials (force re-discovery)

Needed if the device was re-paired in the MSmartHome app or the cache is
corrupt. Requires the device to have internet access again for this one run.

```bash
cd ~/stacks/monitoring
rm /mnt/codex/midea-exporter/device_creds.json
docker compose restart midea-exporter
docker logs midea-exporter --tail 20
# Expect: "no cached creds -- running one-time cloud discovery (needs internet)"
```

---

## Prusa Exporter

### Check exporter health

Port `9118` is not published to the host (Prometheus scrapes it over the internal Docker network only), so check from inside the container:

```bash
docker exec prusa-exporter python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9118/metrics').read().decode())" | grep -E '^prusa_'
docker logs prusa-exporter --tail 30
```

Expect `prusa_up 1` and one `prusa_printer_state{...state="..."} 1`.

### Restart

```bash
cd ~/stacks/monitoring
docker compose restart prusa-exporter
docker compose logs prusa-exporter -f --tail 30
```

### Rotate PrusaLink password

The password is printer-generated (`Settings → Network → PrusaLink` on the touchscreen) and can only be regenerated, not freely chosen.

```bash
# 1. Printer: Settings → Network → PrusaLink → regenerate password
# 2. Update PRUSA_PASSWORD in the monitoring stack .env
cd ~/stacks/monitoring
docker compose up -d prusa-exporter
docker compose logs prusa-exporter --tail 20
```

Symptom of a stale password: `prusa_up 0` while the printer is reachable on the network.

---

## Alertmanager

### Check firing alerts

```bash
curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool
```

### Check Alertmanager status

```bash
cd ~/stacks/monitoring
docker compose logs alertmanager --tail 30
```

### Silence an alert (via UI)

Open `https://alertmanager.home` → **Silences** → **New Silence**. Set matcher, duration, and comment.

### Silence an alert (via CLI)

```bash
# List active silences
curl -s http://localhost:9093/api/v2/silences | python3 -m json.tool

# Delete a silence by ID
curl -X DELETE http://localhost:9093/api/v2/silences/<silence-id>
```

### Restart Alertmanager

```bash
cd ~/stacks/monitoring
docker compose restart alertmanager
docker compose logs alertmanager --tail 20
```

---

## Gitea Actions CI

### Check runner status

```bash
cd ~/stacks/gitea-runner
docker compose logs -f --tail 30
```

### Re-register runner (after token rotation)

```bash
cd ~/stacks/gitea-runner
docker compose down
# Update GITEA_RUNNER_REGISTRATION_TOKEN in .env
docker compose up -d
docker compose logs -f --tail 20
# Watch for: "runner registered successfully"
```

### Re-run a failed workflow

Gitea web UI → Repository → Actions → select failed run → **Re-run jobs**.

---

## Viessmann Heating

### Check vcontrold status

```bash
ssh youruser@192.168.1.13
sudo systemctl status vcontrold
sudo tail -30 /var/log/vcontrold.log
```

### Read current heating values

```bash
vclient -h 127.0.0.1:3002 -c "getTempA,getTempKist,getBetriebArtM1,getNeigungM1,getNiveauM1,getPumpeStatusM1,getBrennerStatus"
```

### Read all status values via API

Only while the control API is enabled (see below). With the API off, use `vclient` or the exporter output.

```bash
# On Hephaestus; viessmann.home is disabled in Caddy
curl -s http://127.0.0.1:8081/status | python3 -m json.tool
```

### Check Viessmann exporter output

```bash
cat /var/lib/node_exporter/textfile_collector/viessmann.prom | grep -v "^#"
```

### Restart vcontrold after USB adapter reconnect

```bash
sudo systemctl restart vcontrold
sudo systemctl status vcontrold
vclient -h 127.0.0.1:3002 -c getDevType
# Expected: V200KW2 ID=2098
```

### Enable / disable Viessmann Control API

> **Currently disabled on purpose:** writing values to the boiler produced errors. Find the cause before enabling it again. Enabling also needs the `viessmann.home` blocks uncommented in `mnemosyne/stacks/caddy/Caddyfile` and `zephyros/caddy/Caddyfile`.

```bash
# Enable
sudo systemctl enable viessmann-api
sudo systemctl start viessmann-api

# Disable (removes write access to heating)
sudo systemctl stop viessmann-api
sudo systemctl disable viessmann-api
```

> The API controls Neigung, Niveau and temperature setpoints. vcontrold and the Prometheus exporter continue running regardless — monitoring is unaffected.

### KW2 pump not starting after mode change

The KW2 state machine occasionally does not activate the heating circuit pump after a remote mode change. Workaround:

```bash
# Reset cycle via vclient
vclient -h 127.0.0.1:3002 -c "setBetriebArtM1 ABSCHALT"
sleep 10
vclient -h 127.0.0.1:3002 -c "setBetriebArtM1 H+WW"
```
