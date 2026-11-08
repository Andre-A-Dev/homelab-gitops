# Boreas

Raspberry Pi 3B at **192.168.1.11**, acting as the home network's DNS server
and monitoring satellite. The name is tracked in Prometheus as `boreas`.

## Role

| Service | How | Port |
|---|---|---|
| Pi-hole v6 | native install | 80 / 443 (admin) |
| Unbound | native install | 5335 (upstream for Pi-hole) |
| pihole6-exporter | systemd | 9666 |
| node-exporter | Docker | 9100 |
| Caddy | Docker (standby) | 443 |

Pi-hole and Unbound are installed natively — nothing in this directory manages
them. This repo only tracks the exporters and Caddy config.

## What is tracked here

### `monitoring/` — node-exporter

Standard node-exporter container with `textfile_collector` support, scraped by
Prometheus on Mnemosyne at `192.168.1.11:9100`.

```bash
cd monitoring && docker compose up -d
```

### `pihole6-exporter/` — Pi-hole v6 Prometheus exporter

A systemd-managed exporter that talks to Pi-hole v6's REST API and exposes
metrics on port `9666`. Pi-hole v6 replaced the old PHP `/admin/api.php`
endpoint with an authenticated REST API, so the older pihole-exporter images
don't work.

Create `/etc/pihole6-exporter.env` from the example:

```bash
cp pihole6-exporter/pihole6-exporter.env.example /etc/pihole6-exporter.env
# fill in PIHOLE_HOST, PIHOLE_PASSWORD, PIHOLE_PORT
```

The exporter itself is third-party code and not in this repo: a checkout of
[tarkilhk/pihole6_exporter_with_queries_time](https://github.com/tarkilhk/pihole6_exporter_with_queries_time)
in `/home/<user>/pihole6_exporter_with_queries_time`, running commit `66e8da2`
(2026-09-06). Its Python dependencies come from apt (`python3-prometheus-client`,
`python3-requests`, `python3-psutil`; `shared/scripts/host-setup.sh` checks them).

```bash
git clone https://github.com/tarkilhk/pihole6_exporter_with_queries_time.git ~/pihole6_exporter_with_queries_time
git -C ~/pihole6_exporter_with_queries_time checkout 66e8da2
```

The systemd unit `/etc/systemd/system/pihole6-exporter.service` runs
`metrics_exporter/pihole6_metrics_exporter.py` from that checkout and reads the
env file. It is host-local: `User=` and `WorkingDirectory=` name the login
user. Its drop-in is tracked in
`systemd/pihole6-exporter.service.d/override.conf`: it starts the exporter
only after `pihole-FTL` and holds the start until the Pi-hole API answers, so
the exporter does not log in too early and stay on 401.
`shared/scripts/host-setup.sh` compares the deployed copy with the repo.
`scripts/backup-boreas.sh` copies the unit, the drop-in and the env file to
Mnemosyne.

No webhook deploys or restarts any of it. After changing the drop-in:

```bash
sudo mkdir -p /etc/systemd/system/pihole6-exporter.service.d
sudo cp systemd/pihole6-exporter.service.d/override.conf /etc/systemd/system/pihole6-exporter.service.d/
sudo systemctl daemon-reload && sudo systemctl restart pihole6-exporter
```

After changing the checkout or the unit, only the last line.

Prometheus scrapes
`:9666` with a 30 s interval and 25 s timeout (set in Mnemosyne's
`prometheus.yml`).

Alerts defined in `mnemosyne/stacks/monitoring/prometheus/alerts.yml`:
- `PiholeDown` — exporter unreachable
- `PiholeHighBlockRate` — block rate > 50 % (possible DNS hijack or misconfigured list)

### `caddy/` — standby reverse proxy

**Not currently running.** This Caddyfile is a contingency config for the
scenario where Mnemosyne moves to a different network. When active, Boreas
acts as a Tailscale subnet router and Caddy proxies `.home` domains through to
Mnemosyne via its Tailscale IP (`100.x.x.x`).

Prerequisites before activating (documented in the Caddyfile header):
1. Disable Pi-hole's built-in port 443
2. Set Pi-hole DNS records: all `.home` → `192.168.1.11`
3. Enable Tailscale subnet router on Boreas (`192.168.1.0/24`)
4. Change the remote FritzBox subnet to `192.168.179.0/24` to avoid collision

### msmtp — email relay (host-local)

msmtp sends outgoing mail from Pi-hole (alerts, weekly digests) and apticron
via Gmail SMTP on port 587. `/etc/msmtprc` holds the credentials, so it is not
in the repo (`.gitignore` excludes every `msmtprc`). Template and setup steps:
`wiki/Update-Notifications.md`, section "msmtp -- SMTP relay".
`scripts/backup-boreas.sh` copies `/etc/msmtprc` to Mnemosyne.

### `docker/daemon.json` — container log rotation

Mirror of `/etc/docker/daemon.json` (deployed 2026-03). Rotates each
container's json log at 10 MB and keeps 3 files. Same reasoning as
`mnemosyne/docker/` — see `mnemosyne/README.md`.

```bash
# Deploy (restarts every container on this host):
sudo cp docker/daemon.json /etc/docker/daemon.json
sudo dockerd --validate --config-file /etc/docker/daemon.json
sudo systemctl restart docker
# Log options only apply to new containers: recreate them per stack
docker compose up -d --force-recreate
```

### `scripts/backup-boreas.sh` — config backup to Mnemosyne

Nightly from root's crontab, pushes Pi-hole, Unbound and misc configs to
`/mnt/codex/backups/boreas/<date>/` on Mnemosyne. Mnemosyne's
`backup-services.sh` prunes copies older than 14 days.

The key may only write into that directory. Its line in Mnemosyne's
`~/.ssh/authorized_keys` carries a forced command, so a compromised Boreas
gets neither a shell nor the other backups on Mnemosyne:

```
restrict,from="192.168.1.11",command="/usr/bin/rrsync -wo /mnt/codex/backups/boreas" ssh-ed25519 AAAA... boreas-backup
```

Check from Boreas: `sudo ssh -i ~/.ssh/backup_key <user>@192.168.1.10 id`
must be refused, then run the backup script once.

## Network

```
LAN 192.168.1.0/24
        │
   Boreas :78
        ├── Pi-hole v6 (DNS :53)
        │       └── Unbound (upstream :5335)
        ├── pihole6-exporter :9666  ←── Prometheus (Mnemosyne)
        └── node-exporter   :9100  ←── Prometheus (Mnemosyne)
```

DNS for all LAN clients is set to `192.168.1.11` in the FritzBox DHCP config.
