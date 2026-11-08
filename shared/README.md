# shared/scripts

Generic maintenance scripts shared across all hosts. Each script is symlinked
to `/usr/local/bin/` on every host that uses it.

Files for the workstations only: [`workstations/README.md`](workstations/README.md).

---

## Overview

| Script | Purpose | Hosts |
|---|---|---|
| `system-update.sh` | Manual system update & cleanup | all |
| `check-container-updates.sh` | Check running containers for image updates | Mnemosyne |
| `gitea-webhook-handler.sh` | Pull repo changes and run host-specific actions | Boreas, Hephaestus, Zephyros, Daidalos |
| `gitea-pull.sh` | Pull latest repo changes unattended (cron) | Boreas, Hephaestus, Zephyros, Daidalos |
| `host-setup.sh` | Check what a server needs from the repo; `--apply` installs packages and links | Hephaestus, Boreas |

---

## system-update.sh

Manual update cycle for the host system. Detects the package manager
automatically (`apt` or `pacman`/`paru`).

**apt:** `update` → `dist-upgrade` → `autoremove` → `autoclean`  
**pacman/paru:** `paru -Syu` → orphan removal → `paccache -rk2`

After the package cycle: checks for failed systemd units, detects reboot
requirement, writes Prometheus textfile metrics, sends an ntfy notification.

```bash
system-update              # full run
system-update --dry-run    # show what would happen, no changes
```

**Config:** `/etc/system-update.conf` (not in Git, create on each host)

```bash
sudo install -m 644 -o "$(id -un)" /dev/null /etc/system-update.conf
nano /etc/system-update.conf
```

```bash
NTFY_TOPIC=your-topic-name
# NTFY_SERVER=https://ntfy.sh
# TEXTFILE_DIR=/var/lib/prometheus/node-exporter   # Debian prometheus-node-exporter package (Daidalos)
```

**Outputs:**
- Log: `/var/log/system-update.log`
- Metrics: `$TEXTFILE_DIR/system_update.prom`, default
  `/var/lib/node_exporter/textfile_collector/` (the node-exporter containers
  mount it). The script warns and skips the metrics if the directory is missing.

---

## check-container-updates.sh

Checks all running Docker containers for available image updates without
downloading image layers. Uses `skopeo` to fetch the remote manifest digest
and compares it against the locally stored digest.

**Dependency:** `skopeo` — `sudo apt install skopeo`

```bash
check-container-updates            # check all running containers
check-container-updates --quiet    # show only containers with updates
```

---

## gitea-webhook-handler.sh

Triggered by a Gitea webhook after every push. Pulls the latest changes and
executes host-specific actions based on which files changed.

Dispatches by hostname:

| Host | Action |
|---|---|
| Boreas | Pull only; every change under `boreas/` is manual |
| Hephaestus | Restarts `vcontrold` if its service file changed |
| Zephyros | `caddy/` changed: validates the Caddyfile, then recreates Caddy; `monitoring/` changed: `docker compose up -d` |
| Daidalos | Pull only (symlinked shared files); Kubernetes manifests go through Flux |

Called by `adnanh/webhook` on port 9000. Logs to `/var/log/webhook-handler.log`.

> Mnemosyne uses its own handler at `mnemosyne/scripts/gitea-webhook-handler.sh`.

---

## gitea-pull.sh

Pulls the latest `homelab-infra` changes from Gitea via `git pull --rebase`.
Runs unattended via cron on Boreas, Hephaestus, and Zephyros as a fallback
alongside the webhook handler, and on Daidalos for the same reason.

Logs to syslog (`logger`) — only on error or actual changes, silent on
"Already up to date".

---

## host-setup.sh

Lists per host what a server needs from this repo and checks it: apt
packages, links into the checkout, configs copied out of the repo, binaries
built from source, pip modules, systemd units (enabled or deliberately
disabled), host-local files that never enter the repo (existence only, never
read), group membership, boot config lines and cron jobs. Use it on an empty
host after `git clone`, and as a drift check on a running one.

```bash
host-setup.sh            # check only, no root; exit 1 on any MISSING/WRONG
host-setup.sh --apply    # install missing apt packages, create missing links, then check
```

`--apply` stops there on purpose: it never replaces an existing file or link
(it may be a local change worth keeping), never copies configs, enables units
or edits cron. Those lines come with the command to fix them. Not covered
at all: installing Docker itself, Pi-hole/Unbound, `tailscale up`,
starting stacks.

Runs as the login user from the checkout, not linked to `/usr/local/bin`.
The root crontab needs `sudo`; without a cached `sudo -v` it shows as `SKIP`.
Exit codes: `0` clean, `1` problems found or bad argument, `2` host not
configured yet.

A new host gets its own branch in the `case` with the lists filled from what
the live host really has (read-only `-ro` checks), not from memory.

---

## Installation

```bash
# Symlink to /usr/local/bin (run on each host) - never copy, a copy drifts
sudo ln -sf ~/homelab-infra/shared/scripts/system-update.sh /usr/local/bin/system-update
# Docker hosts only
sudo ln -sf ~/homelab-infra/shared/scripts/check-container-updates.sh /usr/local/bin/check-container-updates

# system-update: create log file with correct ownership (once per host)
sudo install -m 644 -o "$(id -un)" /dev/null /var/log/system-update.log
```
