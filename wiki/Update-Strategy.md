# Update Strategy

How system packages and container images are updated. [[Update-Notifications]] covers how updates are *detected*; this page covers what happens next.

---

## Principles

- **No auto-updates, no timers.** Packages and images are updated in a deliberate monthly window, never by Watchtower, `unattended-upgrades`, cron or systemd. Alerts make sure a window is not forgotten.
- **The tag says how far an image may move on its own.** Anything beyond that is a commit, reviewed in the diff.
- **The version lives in `docker-compose.yml`, never in `.env`.** `.env` is not versioned, and the deploy webhook only reacts to compose changes, so a version bump there is neither reviewable nor deployed.
- **Verified, not assumed.** After every update: the version inside the container, health, Prometheus targets and probes.

---

## Tag classes

Every service carries its class as a label in `docker-compose.yml`:

```yaml
    container_name: nextcloud-db
    labels:
      homelab.update-class: "A"
```

`check_configs.py` (CI, blocks the deploy) rejects a service without the label, with an unknown value, or where `C` does not match exactly the services with `build:`. `check-container-updates` shows the class per container and adds a hint per class.

| Class | Services | Tag rule | Examples | Why |
|---|---|---|---|---|
| **A** - stateful, schema migrations | Nextcloud, Immich, Gitea, Ghost, Grafana, Vaultwarden, calibre-web, kosync, Wakapi, Open WebUI, all databases | Major tag; databases and 0.x software on minor | `nextcloud:35-apache`, `immich-server:v3`, `gitea/gitea:28`, `ghost:6`, `n1try/wakapi:2`, `mariadb:12.3`, `mysql:8.4`, `koreader-sync:0.2`, `open-webui:0.12` | Migrations are one-way. Nextcloud cannot skip majors. Floating `latest`/`lts`/`release` tags moved Immich from v2 to v3 and MariaDB to 12.3 without anyone deciding it. |
| **B** - stateless infra and exporters | Caddy, Prometheus stack, node-exporter, Loki, exporters, caches and queues (Redis, Valkey), act_runner, Diun | Major tag (or minor where no major tag exists) | `caddy:2`, `prom/prometheus:v3`, `prom/node-exporter:v1`, `grafana/loki:3.7` | Patches arrive with every pull; a new major never arrives unannounced. Worst case is minutes of breakage, no data loss. |
| **C** - local builds | All `build:` services | Base image pinned to minor, rebuilt with `--pull` | `python:3.12-slim`, `node:22-slim` | `docker compose up -d` never re-pulls a base image, and `check-container-updates` cannot see base image updates; it lists the rebuild command per stack instead. Without a rebuild they get no security fixes. |

### Exceptions

| Image | Tag | Reason |
|---|---|---|
| `grafana/alloy` | exact (`v1.21.2`) | Upstream publishes no floating tags. Bump deliberately. |
| `grafana/grafana` | minor (`13.2`) | No major-only tag exists. Minor bumps are commits. Class A: dashboards live only in Grafana's database. |
| `prom/alertmanager`, `prom/blackbox-exporter` | `v0` | No minor tags exist; `v0` floats across all 0.x releases. Accepted for stateless exporters. |
| `ghcr.io/google/cadvisor` | minor (`0.60`) | 0.x minors may change behaviour. Note: `gcr.io/cadvisor` stopped receiving releases after v0.55. |
| `vaultwarden/server` | `latest` | `latest` is the newest stable release; fast security fixes matter more than pinning. |
| Small single-maintainer exporters | `latest` | Accepted risk for read-only exporters. Whatever upstream pushes runs on the LAN. |
| `lscr.io/linuxserver/calibre-web` | exact app version (`0.6.27`) | No floating minor tag upstream. linuxserver rebuilds of the same app version (base image fixes) still arrive on pull. |

---

## Update window (monthly, manual)

**First Sunday of the month, in the morning** (2026-11-01, 2026-12-06, 2027-01-03, ...). The longest gap between two first Sundays is 35 days, so a skipped window shows up as `SystemUpdateOverdue` (45 days) around the next one. `UpdateWindowToday` sends one push that morning (07:00 CET / 08:00 CEST).

One window for everything, so all restarts happen together. **Not between 02:00 and 04:30**: `backup-services` runs at 02:00 and `verify-backup` at 04:00, and a reboot or Docker restart in between breaks the dumps.

1. Backups are fresh and verified: `sudo verify-backup.sh` on Mnemosyne.
2. System packages on all Pis, in a fixed order (see [System updates](#system-updates)).
3. Containers on Mnemosyne (below).
4. Verify, then prune old images.

```bash
# What has updates?
check-container-updates --quiet

# Per stack: pull and recreate
cd ~/stacks/<stack>
docker compose pull
docker compose up -d

# Local builds: refresh the base image
docker compose build --pull
docker compose up -d

# After verification (see below)
docker image prune
```

Keep the old images until verification has passed. They are the only quick way back to the previous version.

### Verification

- The version **inside** the container: `--version`, the OCI label `org.opencontainers.image.version`, or the app's own version file.
- `docker ps`: nothing `unhealthy` or `Restarting`.
- Prometheus: `up == 0` and `probe_success == 0` return only known exceptions.
- Logs since the restart: `docker logs --since 10m <container>`.

---

## System updates

`system-update` (`shared/scripts/system-update.sh`) runs `apt update` → `dist-upgrade` → `autoremove` → `autoclean`, reports failed systemd units and whether a reboot is needed, and writes `system_update.prom`. It never reboots on its own.

| Order | Host | Why at this position |
|---|---|---|
| 1 | Daidalos | Learning cluster, no production impact. Plain Debian 13 (x86_64): a broken Debian package shows up here first. |
| 2 | Hephaestus | Smallest impact of the Pis (heating telemetry), same OS as the DNS Pis: the Raspbian canary. |
| 3 | Boreas | Home DNS, but physically reachable if a reboot hangs. Canary for Pi-hole. |
| 4 | Zephyros | Only after Boreas is clean. Remote: a hung reboot cannot be fixed on site. |
| 5 | Mnemosyne | Last, directly followed by the container updates. |

`scripts/update-hosts.sh` walks this order from a workstation: dry-run, confirm, update, reboot only when required and confirmed, wait for the host to come back. A non-zero exit code, failed units or a failed reboot stop the run unless confirmed. It writes a Markdown report per run (`$UPDATE_REPORT_DIR`, see `scripts/README.md`). The host list in the script follows this table; change both together.

Per host, by hand:

```bash
system-update --dry-run   # what is coming: kernel? Docker engine?
system-update
sudo reboot               # only if it reports "Reboot required" - same window, cleanly
```

- **Reboot in the same window, cleanly.** Never cut the power instead: hard power loss on Mnemosyne left NUL bytes in container logs (2026-08/09).
- **A Docker engine upgrade restarts every container.** Mnemosyne and Zephyros use `docker-ce` from Docker's repo, Boreas and Hephaestus `docker.io` from Debian. No `live-restore`, so `dockerd` restarting means all containers restart.
- **Mnemosyne: system packages before containers.** A reboot after the container updates would restart everything a second time.
- **Security fixes do not wait for the window.** apticron mails each new pending package once (`DIFF_ONLY`). If its changelog shows a `CVE-` for something exposed (OpenSSH, OpenSSL, the kernel on Mnemosyne), apply it on that host right away. Boreas and Hephaestus (Raspbian) have no separate security suite, so the changelog is the only signal there.
- **No timer exists, on purpose.** `apt-daily.timer` and `apt-daily-upgrade.timer` are active but install nothing: `unattended-upgrades` is not installed and `APT::Periodic::*` is unset.
- **Daidalos: apt only.** k3s and Flux are installed outside apt (installer script, binary in `/usr/local/bin`) and `system-update` does not touch them. Its node-exporter is the Debian package, which reads `/var/lib/prometheus/node-exporter`: `/etc/system-update.conf` there sets `TEXTFILE_DIR` accordingly.
- **Not covered:** AstraeusNX and Proteus (workstations, updated on demand) report no `system_update_*` metrics.

### Alerts instead of a timer

Group `system-update` in `alerts.yml`, routed with a 24 h repeat instead of the default 4 h.

| Alert | Fires when | Note |
|---|---|---|
| `UpdateWindowToday` | first Sunday of the month, 06:00-06:59 UTC | Reminder. Own route and receiver: one push, no resolved message. PromQL time functions are UTC, so the local time shifts by an hour with DST. |
| `SystemUpdateOverdue` | last run older than 45 days | First-Sunday window (max. 35 days apart) plus slack. |
| `SystemUpdateFailed` | `system_update_exit_code != 0` | |
| `SystemUpdateFailedUnits` | failed units at the end of the last run | Clears only with the next run. |
| `RebootPending` | reboot required and not done for 3 days | Ignores hosts that booted after the run, because `system_update_reboot_required` stays `1` until the script runs again. |

`system_update.prom` is excluded from `TextfileMetricsStale` (8 days): a monthly file would otherwise fire for most of every month.

---

## Major and minor upgrades (class A, exact pins)

One commit per stack, never bundled into a "bump everything" commit.

1. **Read the release notes.** Breaking changes, removed config options, changed defaults (e.g. MySQL 8.4 disables `mysql_native_password` by default).
2. **Back up.** A fresh dump, and for database engine upgrades a cold copy of the data directory (`docker compose stop` → `cp -a` → `start`). MySQL, MariaDB and Postgres data directories cannot be downgraded. Without the copy, a rollback means restoring the dump into an empty data directory.
3. **Test what can be tested locally** with the new image: `alloy validate`, `loki -verify-config`, `promtool check config`, `dockerd --validate`, and that tools the backup scripts depend on still exist (e.g. `mysqldump`).
4. **Commit, push, read the deploy log** (`journalctl ... | grep webhook`).
5. **Verify the upgrade itself**: upgrade lines in the log (MySQL: `Server upgrade from '80046' to '80413' completed`), the app serves data, the version inside the container.
6. **Remove the rollback copy** only after the next nightly backup has passed `verify-backup.sh`.

---

## Deploy behaviour to keep in mind

| Situation | Effect |
|---|---|
| Image string changed in `docker-compose.yml` | The deploy handler runs `docker compose up -d` and recreates the container. If the tag is not local, Compose pulls its *current* state, so even a "pin to the running version" commit can bring a newer patch. |
| `gitea`, `gitea-runner` | Reported as `manual`, never applied: run `docker compose up -d` by hand. Recreating either kills the running deploy job. |
| Zephyros | Its webhook handler applies `caddy/` (validated first) and `monitoring/`. |
| Boreas, Hephaestus | Only `git pull` plus restarts of specific systemd services; compose changes need a manual `docker compose up -d`. |
| Config-only changes on Mnemosyne (`alerts.yml`, Caddyfile, ...) | The deploy handler force-recreates exactly the services that mount the changed file (inode swap). Check `applied=` / `manual=` in the deploy result. |
| `/etc/docker/daemon.json`, systemd units | Not deployed by anything. Copy by hand; for Docker restart the daemon and recreate containers. |

---

## Verification pitfalls

- **Compare versions, not image IDs.** Comparing a container's image ID with the local tag, or registry digests with each other, said "no change" for Gitea, Immich and Ghost. All three moved a minor or patch version on deploy.
- **A pulled image is not a running image.** `docker compose pull` without `up -d` leaves the old container running. The Immich Redis container ran a dangling image for days that way.

---

## New versions beyond the pinned tag

Diun alone reports digest changes of the tag **in use**: patches show up, a new major does not. Class A applications on Mnemosyne therefore watch their repository for newer tags:

```yaml
    labels:
      homelab.update-class: "A"
      # Diun: new tags from the pinned one upward; move the lower bound with the tag.
      diun.watch_repo: "true"
      diun.include_tags: '^(3[5-9]|[4-9][0-9])-apache$$'
```

- **Current tag and newer only.** With `watch_repo`, Diun also reports rebuilds of every matching tag, and Nextcloud, Grafana and others rebuild older versions regularly. The lower bound keeps those out. When you bump the tag, move the lower bound in the same commit.
- **`$$`, not `$`:** Compose interpolates labels; `docker compose config` prints `$$` again, the container label gets a single `$`.
- **Databases are not watched.** Which MariaDB or MySQL version comes next is decided by what Nextcloud and Ghost support, so the signal is in the application's release notes. Immich's Postgres image follows the upstream compose file.
- **Not watched either:** Vaultwarden (`latest` already reports every release), notify_push and immich-machine-learning (same version as their main service), Open WebUI (no Diun on AstraeusNX).
- **Class C** carries `diun.enable: "false"`: a local build is in no registry, and Diun logged a warning for each of them on every run.
- CI enforces `diun.enable: "false"` on class C, and for `diun.watch_repo` an `include_tags` filter that matches the pinned tag.
- Diun runs on Mnemosyne only. Containers on Boreas, Zephyros and Hephaestus are not watched.

---

## Not used: Renovate

Renovate would turn every tag bump into a pull request in Gitea, which makes exact pins with digests cheap and gives a version history per image. It is one more service plus a steady stream of PRs. Major tags, Diun and a monthly window cover the need today. Revisit if exact pins start to accumulate.

See [[Update-Notifications]] for Diun, ntfy and apticron, and [[Backup-Strategy]] for the backups every upgrade depends on.
