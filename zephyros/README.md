# Zephyros

Raspberry Pi 3B+ at a **remote location**, reachable from the home network only
via Tailscale (`100.y.y.y`). It provides local DNS and acts as a
Caddy reverse proxy so that `.home` services on Mnemosyne remain accessible
from that site.

## Role

| Service | How | Port |
|---|---|---|
| Pi-hole v6 | native install | 53 / 8081 (admin) |
| Unbound | native install | 5335 |
| pihole6-exporter | systemd | 9666 |
| Caddy | Docker | 443 |
| node-exporter | Docker | 9100 |
| fritz-exporter | Docker | 9787 |

## What is tracked here

### `caddy/` — active reverse proxy

Unlike Boreas, Caddy is **always running** here. It is the primary way to
access `.home` services from this location — browsers resolve `.home` names via
Pi-hole, which returns Zephyros's LAN IP, and Caddy proxies the request over
Tailscale to Mnemosyne (`100.x.x.x`).

```
Browser (remote LAN)
  → DNS: Pi-hole resolves *.home → Zephyros LAN IP
  → Caddy :443 (TLS, internal CA)
    ├── pihole.home, pihole-zephyros.home → host.docker.internal:8081 (Pi-hole admin, local)
    ├── pihole-boreas.home → Mnemosyne Caddy → Boreas Pi-hole
    ├── immich.home  → 100.x.x.x  (Tailscale direct — bypasses DNS chain)
    └── *.home       → https://<same hostname>.home  (resolved by Pi-hole → Mnemosyne Caddy)
```

`pihole.home` routes to `172.19.0.1` (Docker bridge gateway) instead of
`localhost` because Caddy runs in Docker and Pi-hole runs natively on the host.

`immich.home` is proxied directly to the Tailscale IP rather than via the
hostname because the DNS resolution chain would loop back through Caddy.

All upstream transports use `versions 1.1` — TLS 1.3 negotiation is unreliable
on the armv7 build of Caddy's Go runtime on this hardware.

```bash
cd caddy && docker compose up -d
```

### `monitoring/` — node-exporter + fritz-exporter

```bash
cp monitoring/.env.example monitoring/.env
# fill in FRITZ_HOSTNAME, FRITZ_USERNAME, FRITZ_PASSWORD
cd monitoring && docker compose up -d
```

`fritz-exporter` polls the local FritzBox router via TR-064 and exposes metrics
on `:9787`. Both exporters are scraped by Prometheus on Mnemosyne over Tailscale.

node-exporter here does **not** mount a `textfile_collector` volume — there are
no custom textfile metrics on this node.

### `pihole/pihole.toml`

A snapshot of `/etc/pihole/pihole.toml` checked in for reference. Notable
non-default settings:

| Setting | Value | Reason |
|---|---|---|
| `dns.upstreams` | `1.1.1.1`, `1.0.0.1` | Cloudflare upstream (Unbound used for local resolution only) |
| `dns.interface` | `eth0` | Explicit bind to wired interface |
| `webserver.port` | `80o` only | HTTPS handled by Caddy; Pi-hole admin runs plain HTTP |
| `dns.specialDomains.mozillaCanary` | `true` | Blocks Firefox DoH bypass |
| `dns.specialDomains.iCloudPrivateRelay` | `true` | Blocks iCloud Private Relay bypass |
| `dns.specialDomains.designatedResolver` | `true` | Blocks DDR discovery (RFC 9462) |

To update: copy the live file from the host and commit it as a reference snapshot.

### pihole6-exporter (host-local, not tracked)

Same setup as on Boreas (`boreas/README.md`, section `pihole6-exporter/`):
the third-party checkout `tarkilhk/pihole6_exporter_with_queries_time` at
commit `66e8da2` in `/home/<user>/pihole6_exporter_with_queries_time`, Python
dependencies from apt (`python3-prometheus-client`, `python3-requests`,
`python3-psutil`), a host-local unit with drop-in, and `/etc/pihole6-exporter.env`
from `boreas/pihole6-exporter/pihole6-exporter.env.example`.

One difference: the Pi-hole API answers on `http://localhost:8081` here, since
Caddy holds port 80. The env file's `PIHOLE_HOST` and the drop-in's
`ExecStartPre` check both point there.

### `scripts/backup-zephyros.sh`

Backs up Pi-hole, Unbound, Caddy, and misc configs to Mnemosyne via
Tailscale rsync. Run from cron on Zephyros.

```
Destination: <user>@100.x.x.x:/mnt/codex/backups/zephyros/<date>/
Retention:   30 days, pruned by Mnemosyne's backup-services.sh
SSH key:     /home/<user>/.ssh/backup_key
```

The key may only write into that directory. Its line in Mnemosyne's
`~/.ssh/authorized_keys` carries a forced command, so a compromised Zephyros
gets neither a shell nor the other backups on Mnemosyne:

```
restrict,from="100.y.y.y",command="/usr/bin/rrsync -wo /mnt/codex/backups/zephyros" ssh-ed25519 AAAA... <comment>
```

Find the line with `ssh-keygen -lf ~/.ssh/authorized_keys` (fingerprint of
`backup_key.pub`). Check from Zephyros: `sudo ssh -i ~/.ssh/backup_key
<user>@100.x.x.x id` must be refused, then run the backup script once.

What gets backed up: `/etc/pihole/pihole.toml`,
`/etc/unbound/unbound.conf.d/pi-hole.conf`, `/etc/msmtprc`,
`/etc/apticron/apticron.local.conf`, `/etc/pihole6-exporter.env`,
`/etc/systemd/system/pihole6-exporter.service` and its drop-in directory
`pihole6-exporter.service.d/`, and Caddy's internal CA (`caddy/data/caddy/pki`).

### `docker/daemon.json` — container log rotation

Deploy to `/etc/docker/daemon.json`. Rotates each container's json log at
10 MB and keeps 3 files. Same reasoning as `mnemosyne/docker/` — see
`mnemosyne/README.md`.

```bash
# Deploy (restarts every container on this host):
sudo cp docker/daemon.json /etc/docker/daemon.json
sudo dockerd --validate --config-file /etc/docker/daemon.json
sudo systemctl restart docker
# Log options only apply to new containers: recreate them per stack
docker compose up -d --force-recreate
```

## Network

```
Remote LAN (e.g. 192.168.x.0/24)
        │
   Zephyros :84 (Tailscale)
        ├── Pi-hole v6 (DNS :53)
        │       └── Unbound (local resolution :5335)
        ├── Caddy :443  ──── Tailscale ────▶ Mnemosyne :443
        ├── pihole6-exporter :9666  ◀── Prometheus (Mnemosyne, via Tailscale)
        ├── node-exporter   :9100  ◀── Prometheus (Mnemosyne, via Tailscale)
        └── fritz-exporter  :9787  ◀── Prometheus (Mnemosyne, via Tailscale)
```

DNS for remote LAN clients is set to Zephyros's LAN IP in the local FritzBox
DHCP config.
