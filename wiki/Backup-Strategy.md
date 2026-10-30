# Backup Strategy

## Principle: 3-2-1

**3** copies of the data -- **2** different media -- **1** copy offsite. Until all three conditions are met, it is not a backup.

| Copy | Location | Medium |
|---|---|---|
| 1 -- Live data | Mnemosyne `/mnt/codex`, `/mnt/vault` | USB SSD |
| 2 -- Local backup | Mnemosyne `/mnt/backup/<YYYY-MM-DD>/` | USB HDD (WD Red 4 TB, ext4), next to the Pi |
| 3 -- Offsite | Hetzner Storage Box, restic repository | Encrypted and deduplicated by restic |

Copy 2 does not survive fire, theft or a power surge at the Pi. Only copy 3 does.

---

## What Gets Backed Up

`backup-services.sh` declares every source path in `SOURCE_PATHS`; this table follows it.

| Service | Source | Artifact | Method |
|---|---|---|---|
| Vaultwarden | `/mnt/vault/vaultwarden/data/` | `vaultwarden-data.tar.gz`, `vaultwarden-db.sqlite3` | tar + `sqlite3 .backup` |
| Caddy TLS | `/mnt/vault/caddy/` | `caddy-data.tar.gz` | tar |
| Nextcloud | `/mnt/codex/nextcloud/data/` + MariaDB | `nextcloud-data.tar`, `nextcloud-db.sql` | Maintenance mode, `mariadb-dump`, uncompressed tar |
| Immich | `/mnt/codex/immich/upload/` + PostgreSQL | `immich-upload.tar`, `immich-db.sql` | `pg_dumpall`, uncompressed tar |
| Ghost | `/mnt/codex/ghost/content/` + MySQL | `ghost-content.tar.gz`, `ghost-db.sql` | `mysqldump`, tar |
| Gitea | `/mnt/codex/gitea/data/` | `gitea-data.tar.gz` | tar |
| Gitea Act Runner | `/mnt/codex/gitea/runner/` | `gitea-runner.tar.gz` | tar |
| Calibre library | `/mnt/codex/calibre-library/` | `calibre-library.tar.gz` | tar |
| Calibre-Web config | `/mnt/codex/calibre-web-config/` | `calibre-web-config.tar.gz` | tar |
| KOSync | `/mnt/codex/kosync/data/` | `kosync-data.tar.gz` | tar |
| Syncthing | `/mnt/codex/syncthing/obsidian/` + config | `syncthing-obsidian.tar.gz`, `syncthing-config.tar.gz` | tar |
| Aegis | `/mnt/codex/syncthing/aegis/` | `aegis-backup.tar.gz` | tar |
| Grafana | `/mnt/codex/grafana/` | `grafana-data.tar.gz` | tar |
| JobIris | `/mnt/vault/jobiris/` | `jobiris.tar.gz` | tar |
| Exporter tokens | tado, midea, netatmo under `/mnt/codex/*-exporter/` | `exporter-tokens.tar.gz` | tar |
| Pi configs | newest of `/mnt/codex/backups/boreas/<date>/` and `…/zephyros/<date>/` | `pi-configs.tar.gz` | tar; the Pis push these themselves (`backup-boreas.sh` nightly, `backup-zephyros.sh` weekly). The step fails when Boreas' newest backup is older than 2 days or Zephyros' older than 8 |
| Stack configs | `~/stacks/` | `stacks-config.tar.gz` | tar with `-h` (follows the symlink) |

**Stack configs are critically underrated.** The `docker-compose.yml` and `Caddyfile` are small files, but without them a restore takes hours instead of minutes. The archive also contains every stack's `.env`, which makes it the most sensitive file in the set.

**Databases are dumped, never tarred live.** Running `tar` on a live database directory produces corrupt backups. Nextcloud runs in maintenance mode during its dump, with a `trap ... EXIT` so maintenance mode is always disabled even if the script crashes. Photo and video data is archived uncompressed (`.tar`) -- it does not benefit from gzip.

**Deliberately not backed up** (`DRIFT_IGNORE` in the script): Prometheus TSDB, Loki, Alloy state, Alertmanager state and Carousel job artefacts -- all regenerable. **Wakapi** (SQLite with coding statistics) is a deliberately accepted loss (decided 2026-08-30).

Database passwords are not stored in the script. It reads them from the stacks' `.env` files at run time.

---

## Failure Scenarios

### Scenario A -- Single service failure
*"Vaultwarden database corrupt, everything else running"*

Stop the affected service, restore from the local backup, restart. Expected downtime: 5–15 minutes.

### Scenario B -- Data SSD failure
*"Data SSD dead, Pi and backup drive intact"*

Replace the SSD, mount it by UUID, restore all services from the local backup. Expected downtime: 2–3 hours.

### Scenario C -- Pi and local backup lost
*"Fire, theft, water -- everything in the room is gone"*

Only the offsite copy survives. This is the scenario the offsite layer exists for, not drive failure alone. Restore path: pull the newest backup day out of the restic repository onto a new drive mounted at `/mnt/backup`, then follow the normal restore procedure. Commands: `RUNBOOK.md` in the repository root, section "Restore from Offsite (restic)". Expected downtime: ~1 day plus download time.

Scenario C needs three things that must exist **outside** Mnemosyne: the repository address, the restic password and a way to authenticate to the Storage Box.

---

## Backup Layers

### Layer 1 -- Local backup (daily, 02:00)

`backup-services.timer` runs `backup-services.sh`, which archives all service data into a dated directory under `/mnt/backup`. Directories older than 7 days are deleted at the start of each run.

Safeguards built into the script:

- **Change detection** -- unchanged services are skipped and get a `.SKIPPED` marker that names the date of the last real archive. A skip chain never outlives the retention window: after 5 days a full archive is forced.
- **Archive assertion** -- an archive with fewer than 2 entries counts as a failure. An empty directory is not a successful backup.
- **Free-space preflight** -- the run aborts before starting if less than 150 GB are free (one run writes roughly 73 GB).
- **Root-only date directories** -- each `/mnt/backup/<YYYY-MM-DD>/` is created with mode `700`. The archives inside are `644` and contain every `.env`, the Caddy CA key and the Vaultwarden database; until 2026-10 any local user could read them.
- **Mount drift** -- the script compares its source paths with the bind mounts of the running containers. A new stack with persistent data must either be added to `SOURCE_PATHS` or deliberately listed in `DRIFT_IGNORE`.

`verify-backup.timer` runs `verify-backup.sh` at 04:00: archives exist and are readable, Vaultwarden SQLite integrity, Nextcloud dump header, disk space.

### Layer 2 -- Offsite backup (restic, daily)

On a clean local run, `backup-services.sh` starts `restic-offsite.service` with `systemctl start --no-block` and exits. The offsite run is a separate systemd unit with its own lock, metrics and exit code. A slow upload never blocks the local backup, and a failed upload never marks the local backup as failed. A local run with errors does not trigger offsite: a broken snapshot is not shipped. `restic-offsite.timer` fires at 06:00 as a fallback in case the trigger was missed.

restic backs up the **local tarball tree** (`/mnt/backup`), not the live data. The tarballs are already application-consistent (maintenance mode, database dumps), so the offsite copy inherits that consistency.

| | |
|---|---|
| Target | Hetzner Storage Box over SFTP (port 23) |
| Configuration | `/etc/restic/restic-offsite.env` (template: `mnemosyne/scripts/restic-offsite.env.example`) |
| Password | `/root/.config/restic/password` -- **without it the repository is worthless** |
| SSH | Explicit `sftp.command` with key path and host; root does not read the user's `~/.ssh/config` |
| Retention | `restic forget` after each backup: 7 daily, 4 weekly, 6 monthly snapshots |
| Maintenance | Weekly (Sunday 05:00): `restic prune`, then `restic check --read-data-subset=2%` |
| Concurrency | Backup and maintenance share one lock file, `/var/run/restic-repo.lock` |

**Why restic replaced rclone (July 2026).** The first offsite design ran `rclone sync` through a `crypt` remote as a step inside `backup-services.sh`. Three problems ended it:

- **Upload volume.** Every archive lands in a new dated directory, so to rclone each re-archived service is a new file and goes up in full: one changed file in Nextcloud means the whole `nextcloud-data.tar` again. restic deduplicates at chunk level, so the near-identical archives of consecutive days cost almost nothing. Nights in October 2026 uploaded 250–450 MB in about 10 minutes.
- **History.** `rclone sync` mirrored the local retention, so offsite never held more history than the backup drive itself. restic snapshots are independent of local retention; 6 monthly snapshots cost little extra because identical blocks are shared.
- **A silent failure.** The rclone step relied on an SSH config alias that root's context never reads. It failed for three nights without anyone noticing, because the step was buried in the local backup's output. The restic unit has its own exit code, its own metrics and its own Healthchecks.io check.

Encryption is client-side: the Storage Box only ever sees restic's encrypted packs. The flip side is that the restic password is a single point of failure. It must be stored in Vaultwarden **and** in an offline copy, together with a way to reach the Storage Box that does not depend on the Pi.

### Layer 3 -- System image (monthly, manual)

`rpi-clone` creates a full image of the system drive on a second drive. This covers the OS and all configuration outside the data directories.

---

## Backup Medium

The local backup target is a WD Red 4 TB HDD on USB, formatted ext4 and mounted at `/mnt/backup` by UUID. The `nofail` mount option is required -- without it, a missing drive blocks the boot process. `restic-offsite.service` declares `RequiresMountsFor=/mnt/backup` and does not start when the drive is missing.

```
/etc/fstab entry:
UUID=<uuid>  /mnt/backup  ext4  defaults,nofail,noatime  0  2
```

---

## Schedule

| What | Frequency | When | Medium |
|---|---|---|---|
| Local backup (`backup-services.timer`) | Daily | 02:00 | Backup HDD |
| Offsite backup (`restic-offsite.service`) | Daily | Right after a clean local backup; fallback timer 06:00 | Hetzner Storage Box |
| Backup verification (`verify-backup.timer`) | Daily | 04:00 | -- |
| restic prune + check (`restic-maintenance.timer`) | Weekly | Sunday 05:00 | Hetzner Storage Box |
| Disk space check (ntfy alert) | Daily | 08:00 | -- |
| System image (rpi-clone) | Monthly | Manual | Second drive |
| Restore test | Quarterly | Manual | -- |

---

## Monitoring

| Question | Answer comes from |
|---|---|
| Did the job run at all? | Healthchecks.io: every backup unit pings `/start` and its result through a `healthchecks.conf` drop-in. A missing ping alerts even if Mnemosyne is down. |
| What did it produce? | Textfile metrics `backup*.prom`, `restic_offsite.prom`, `restic_maintenance.prom` and the systemd-level `restic_*_unit.prom` → alert rules in `prometheus/backup-alerts.yml` |
| Are the archives readable? | `verify-backup.sh` (daily) and `restic check` with a 2 % data read (weekly) |

The `restic_*_unit.prom` files are written by `restic-unit-metrics.sh` from `ExecStopPost=`. They record what systemd saw, so they also catch failures that kill the script before it can write its own metrics (missing executable bit, timeout, OOM).

---

## Restore

A backup that has never been restored is not a backup. After any significant change to the backup script or data layout, perform a restore test by restoring a single non-critical service.

`restore-services.sh` restores individual services from a date directory under `/mnt/backup`. It requires `/mnt/backup` to be a mountpoint. For an offsite restore, the newest day is first pulled out of the restic repository onto a drive mounted there.

See `RUNBOOK.md` in the repository root for the commands and for the services the restore script does not cover.
