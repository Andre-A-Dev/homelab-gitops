# Services Reference

All internal `.home` domains require the Caddy root certificate to be imported on the client. External access from remote the home network requires Tailscale, or — for services proxied via Zephyros — only the Zephyros CA certificate on the accessing device.

---

## Mnemosyne — `192.168.1.10` · Tailscale `100.x.x.x`

| Service | URL / Address | Port | TLS | External (Tailscale) |
|---|---|---|---|---|
| Vaultwarden | `https://vault.home` | `443` | Internal CA | ✅ |
| Nextcloud | `https://cloud.yourdomain.dedyn.io` | `443` | Let's Encrypt | ✅ public |
| Gitea (web) | `https://git.home` | `443` | Internal CA | ✅ |
| Gitea (SSH) | `ssh://git@192.168.1.10` | `2222` | — | ✅ |
| Immich | `https://immich.home` | `443` | Internal CA | ✅ (`http://100.x.x.x:2283`) |
| Grafana | `https://grafana.home` | `443` | Internal CA | ✅ |
| Prometheus | `https://prometheus.home` | `443` | Internal CA | ✅ |
| Calibre-Web | `https://calibre.home` | `443` | Internal CA | ✅ |
| KOSync | `https://kosync.home` | `443` | Internal CA | ✅ |
| Wakapi | `https://wakapi.home` | `443` | Internal CA | ✅ |
| Aether (Weather) | `https://weather.home` | `443` | Internal CA | ✅ |
| Jobiris | `https://jobiris.home` | `443` | Internal CA | ✅ |
| Carousel | `https://carousel.home` | `443` | Internal CA | ✅ |
| Syncthing | `https://syncthing.home` | `443` | Internal CA | ✅ |
| Alertmanager | `https://alertmanager.home` | `443` | Internal CA | ✅ |
| Alloy | `https://alloy.home` | `443` | Internal CA | ✅ |
| Rack display mirror | `https://rack.home` | `443` | Internal CA | ✅ |
| Galatea (in development) | `https://galatea.home` | `443` | Internal CA | ✅ |
| Pi-hole admin (Boreas / Zephyros) | `https://pihole-boreas.home`, `https://pihole-zephyros.home` | `443` | Internal CA | ✅ |
| FritzBox (remote) | `https://fritz-zephyros.home` | `443` | Internal CA | ✅ |
| Caddy (HTTP) | — | `80` | — | — |
| Webhook listener | — | `9000` | — | — |

> Disabled Caddy sites: `homepage.home` (stack under the `inactive` profile), `blog.yourdomain.dedyn.io`, `ghostwrite.home` and `ghostproxy.home` (Ghost, Ghostwrite and GhostProxy under the `inactive` profile; `ghost-db` keeps running for the nightly backup dump) and `viessmann.home` (see Hephaestus). Their Pi-hole records on Boreas still exist.

> Services without a URL: `diun` (image update notifications via ntfy), `gitea-runner` (Gitea Actions) and `discord-inactivity` (Discord bot, in development, under the `inactive` profile).

### Monitoring exporters (internal only)

| Exporter | Port | Notes |
|---|---|---|
| Node Exporter | `9100` | System metrics |
| Blackbox Exporter | `9115` | HTTP uptime checks |
| Alertmanager | `9093` | Alert routing → ntfy |
| Netatmo Exporter | `9210` | Weather station |
| Fritz Exporter (home) | `9787` | FritzBox home network |
| Tado Exporter | `9100` | Heating metrics |
| Nextcloud Exporter | `9205` | Nextcloud metrics |
| cAdvisor | `8080` | Container metrics |
| Shelly Exporter | `9117` | Shelly smart plugs (Gen1 + Gen2/3) |
| Midea Exporter | `9116` | Midea AC (LAN, cloud only on first run) — scrape job disabled |
| smartctl Exporter | `9633` | Disk health, native systemd service on the host |
| Prusa Exporter | `9118` | PrusaLink: temps, state, job progress (Pygmalion MK4S) |

> Prometheus (`9090`) and all exporters are internal only. Never expose these ports externally.

---

## Boreas — `192.168.1.11` (home network)

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| Pi-hole | `http://192.168.1.11/admin` | `80` | DNS filtering, v6 |
| Unbound | — | `5335` | Upstream for Pi-hole only |
| Node Exporter | — | `9100` | Docker, scraped by Prometheus on Mnemosyne |
| Pi-hole Exporter | — | `9666` | systemd service, scraped by Prometheus |
| Caddy | — | `443` | Docker, standby |

---

## Zephyros — `192.168.1.11`* (remote network) · Tailscale `100.y.y.y`

> \* Zephyros shares the `192.168.1.0/24` range with the home network, but is on a physically separate network at the remote's location. No routing conflict — Tailscale addresses it exclusively via `100.y.y.y` from remote.

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| Pi-hole | `http://192.168.1.11/admin` | `80` | DNS filtering, v6 |
| Unbound | — | `5335` | Upstream for Pi-hole only |
| Caddy | — | `443` | Reverse proxy → Mnemosyne via Tailscale |
| Node Exporter | — | `9100` | Scraped by Prometheus on Mnemosyne via Tailscale |
| Pi-hole Exporter | — | `9666` | systemd service, scraped via Tailscale |
| Fritz Exporter | — | `9787` | FritzBox at remote's location |
| Fritz Exporter Lua | — | `9042` | FritzBox DECT + system metrics via Lua API |

### Proxied domains (via Zephyros Caddy → Mnemosyne)

Devices on the remote's network without Tailscale can access all `.home` services of Mnemosyne after importing the Zephyros CA certificate. Pi-hole on Zephyros resolves them to Zephyros (`192.168.1.11`); Caddy proxies them to Mnemosyne via Tailscale.

| Domain | Notes |
|---|---|
| `pihole.home`, `pihole-zephyros.home` | Local Pi-hole admin, not proxied |
| `immich.home` | Proxied directly to the Tailscale IP (the DNS chain would loop back) |
| all other Mnemosyne `.home` sites | Proxied to Mnemosyne Caddy with the same hostname |

Details: `zephyros/README.md`.

---

## Hephaestus — `192.168.1.13`

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| vcontrold | `127.0.0.1:3002` | `3002` | Viessmann KW2 daemon, localhost only |
| Viessmann Control API | — | `8081` | **Disabled** — see below |
| Node Exporter | — | `9100` | Scraped by Prometheus on Mnemosyne (60s interval) |

> The Flask control API backed a Grafana panel for setting heating values (slope, level, setpoints). It is disabled (`viessmann-api.service` stopped and disabled) because writing values to the boiler produced errors. The `viessmann.home` Caddy sites on Mnemosyne and Zephyros are commented out so that an accidental `systemctl start` does not reopen the route. Reading is unaffected: vcontrold and the exporter keep running.

---

## Daidalos — `192.168.1.x`

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| k3s API | — | `6443` | Single node, traefik + servicelb disabled |
| Flux | — | — | Reconciles `clusters/daidalos/` from Gitea |
| Node Exporter | — | `9100` | Debian package, scraped by Prometheus on Mnemosyne |
| smartctl Exporter | — | `9633` | Debian package |
| Webhook listener | — | `9000` | Gitea webhook → `gitea-webhook-handler.sh` |

---

## DNS Records (Pi-hole, Boreas)

| Domain | Resolves to | Notes |
|---|---|---|
| `mnemosyne.local` | `192.168.1.10` | |
| `boreas.local` | `192.168.1.11` | |
| `hephaestus.local` | `192.168.1.13` | |
| `vault.home` | `192.168.1.10` | |
| `git.home` | `192.168.1.10` | |
| `grafana.home` | `192.168.1.10` | |
| `prometheus.home` | `192.168.1.10` | |
| `calibre.home` | `192.168.1.10` | |
| `kosync.home` | `192.168.1.10` | |
| `syncthing.home` | `192.168.1.10` | |
| `immich.home` | `192.168.1.10` | |
| `ghostwrite.home` | `192.168.1.10` | Stale: Caddy site disabled |
| `ghostproxy.home` | `192.168.1.10` | Stale: Caddy site disabled |
| `weather.home` | `192.168.1.10` | Aether weather console |
| `carousel.home` | `192.168.1.10` | Instagram carousel generator |
| `alertmanager.home` | `192.168.1.10` | |
| `alloy.home` | `192.168.1.10` | |
| `jobiris.home` | `192.168.1.10` | |
| `wakapi.home` | `192.168.1.10` | |
| `rack.home` | `192.168.1.10` | Rack display mirror |
| `pihole.home`, `pihole-boreas.home`, `pihole-zephyros.home` | `192.168.1.10` | Caddy proxy → Pi-hole admin |
| `fritz-zephyros.home` | `192.168.1.10` | Caddy proxy → remote FritzBox via Zephyros |
| `homepage.home` | `192.168.1.10` | Stale: Caddy site disabled |
| `viessmann.home` | `192.168.1.10` | Stale: Caddy site disabled, API off |
| `cloud.yourdomain.dedyn.io` | `192.168.1.10` | Also public via deSEC DynDNS |
| `blog.yourdomain.dedyn.io` | `192.168.1.10` | Public via deSEC DynDNS. Stale: Caddy site disabled |
| `yourdomain.dedyn.io` | `192.168.1.10` | deSEC DynDNS |

### DNS Records (Pi-hole, Zephyros — remote network)

| Domain | Resolves to | Notes |
|---|---|---|
| `*.home` (all proxied sites) | `192.168.1.11` | → Zephyros Caddy → Mnemosyne via Tailscale |

---

## Storage Paths

| Data | Path | Host |
|---|---|---|
| Vaultwarden | `/mnt/vault/vaultwarden/data/` | Mnemosyne |
| Caddy TLS | `/mnt/vault/caddy/` | Mnemosyne |
| Nextcloud files | `/mnt/codex/nextcloud/data/` | Mnemosyne |
| Nextcloud DB | `/mnt/codex/nextcloud/db/` | Mnemosyne |
| Gitea | `/mnt/codex/gitea/data/` | Mnemosyne |
| Immich uploads | `/mnt/codex/immich/upload/` | Mnemosyne |
| Immich DB | `/mnt/codex/immich/db/` | Mnemosyne |
| Ghost content | `/mnt/codex/ghost/content/` | Mnemosyne |
| Calibre library | `/mnt/codex/calibre-library/Library/` | Mnemosyne |
| Syncthing vault | `/mnt/codex/syncthing/obsidian/` | Mnemosyne |
| Wakapi data | `/mnt/codex/wakapi/` | Mnemosyne |
| Alertmanager data | `/mnt/codex/alertmanager/` | Mnemosyne |
| Netatmo token | `/mnt/codex/netatmo-exporter/` | Mnemosyne |
| Tado token | `/mnt/codex/tado-exporter/` | Mnemosyne |
| Midea device credentials | `/mnt/codex/midea-exporter/` | Mnemosyne |
| Carousel job output | `/mnt/codex/carousel/jobs/` (last 30 renders, older pruned automatically) | Mnemosyne |
| Docker data root | `/mnt/codex/docker/` | Mnemosyne |
| containerd root | `/mnt/codex/containerd/` | Mnemosyne |
| Backup target (local) | `/mnt/backup/` | Mnemosyne (UUID `XXXX-XXXX`) |
| Backup target (offsite) | Hetzner Storage Box, restic repository over SFTP (client-side encryption) | External — Germany/Finland |
| restic config | `/etc/restic/restic-offsite.env` (template: `mnemosyne/scripts/restic-offsite.env.example`) | Mnemosyne |
| restic password | `/root/.config/restic/password` — must also exist in Vaultwarden and offline; without it the repository is unrecoverable | Mnemosyne |
| restic cache | `/var/cache/restic/` | Mnemosyne |
| Offsite SSH key | `~/.ssh/hetzner_storagebox` (no passphrase — used by the unattended restic units) | Mnemosyne |
| vcontrold config | `/etc/vcontrold/` | Hephaestus |
| Viessmann metrics | `/var/lib/node_exporter/textfile_collector/viessmann.prom` | Hephaestus |
| Viessmann change log | `/var/log/viessmann-control.log.json` | Hephaestus |
| Fan metrics | `/var/lib/node_exporter/textfile_collector/fan.prom` | Mnemosyne |

---

## Symlinks

| Symlink | Target |
|---|---|
| `~/stacks/` | `~/homelab-infra/mnemosyne/stacks/` |
| `/usr/local/bin/backup-services.sh` | `~/homelab-infra/mnemosyne/scripts/backup-services.sh` |
| `/usr/local/bin/verify-backup.sh` | `~/homelab-infra/mnemosyne/scripts/verify-backup.sh` |
| `/usr/local/bin/restore-services.sh` | `~/homelab-infra/mnemosyne/scripts/restore-services.sh` |
| `/usr/local/bin/tailscale-metrics.sh` | `~/homelab-infra/mnemosyne/scripts/tailscale-metrics.sh` |
| `/usr/local/bin/fan-metrics.sh` | `~/homelab-infra/mnemosyne/scripts/fan-metrics.sh` |
