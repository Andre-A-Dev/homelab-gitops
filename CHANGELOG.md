# Changelog

Milestones of this homelab. Every push to `main` deploys, so a release
marks a finished theme, not a deployable version. Releases before
v1.2.0 predate the public mirror and have no tag there.

## v1.2.0 — Validated Deploys and Offsite Backups (2026-11-02)

A push to `main` now passes CI before it deploys, and the deploy applies
exactly the validated commit, recreates only what changed and verifies the
result. Backups leave the house encrypted, are checked by content instead of
exit codes, and report to an external dead man's switch. Around that, the
homelab gained a Kubernetes learning node, disk health monitoring on every
machine and a hardening pass over network exposure and SSH access.

### Deployment and CI

- Every push is validated before deploy: Compose files, Prometheus and Alertmanager config, alert rules, dashboards, shellcheck, executable bits and `.env.example` drift
- Mnemosyne deploys through a synchronous webhook that fast-forwards to the validated commit, applies a per-file plan (up, recreate, rebuild), verifies the touched containers and reports manual follow-ups
- Boreas, Hephaestus, Zephyros and Daidalos share one pull-based webhook handler
- Services are disabled only through a fixed set of Compose profiles, enforced by CI
- Every service carries an update class; images are pinned to major or minor tags, and Diun watches for newer tags of stateful applications
- A nightly drift check compares the repo with the running containers and Tailscale routes and names cause and fix per finding
- Docker log rotation is versioned for all Pis

### Backups

- Offsite backups to a Hetzner Storage Box with restic: client-side encryption, scheduled maintenance and integrity checks
- Nightly backup runs as a systemd timer; every backup job reports to Healthchecks.io
- Verification asserts sources and archive contents, and a service without a check fails the run
- Coverage extended to Immich and to the Pi configs of Boreas and Zephyros
- Database dumps are written every night, independent of the file change detection that skips unchanged services
- Restore works with bind mounts and covers every backed-up service
- Runbook procedures for an emergency SSH key and an offline key stick

### Monitoring

- Loki and Alloy collect logs, provisioned into Grafana
- SMART and NVMe health for every disk on servers and workstations, with alerts keyed by drive serial
- Dead man's switch via Healthchecks.io, plus alerts for down nodes and exporters
- New exporters: Prusa MK4S via PrusaLink, Midea air conditioner (seasonal), Shelly H&T sensors and Gen2+ plugs, FritzBox Lua metrics on Zephyros
- Container update metrics including the running image digest; alerts and a monthly reminder for the manual system update window
- Push notifications when the washing machine or dryer has finished

### Services and displays

- Immich photo library, with VectorChord and Valkey
- Aether: a weather console for Netatmo, Tado and Shelly readings with forecast and rain radar, read live from Prometheus
- Rack display: a touch panel showing alerts, backups, containers, network and printer state, with a browser mirror
- Carousel: an accessible generator that turns a Markdown post into Instagram slides with alt text
- Nextcloud push notifications via notify_push

### Security

- `.home` sites answer only to the LAN and Tailscale
- Prometheus, Alertmanager, the exporters, the deploy webhook and vcontrold listen on local interfaces only; Caddy blocks Prometheus lifecycle writes
- SSH password login disabled; backup upload keys are restricted to rsync
- A read-only user with one key per workstation for automated checks, including read-only access to the cluster
- Backup directories are readable by root only
- Dependency upgrades close open advisories in the self-built services

### Kubernetes

- Daidalos runs single-node k3s, reconciled by Flux from Gitea; a throwaway deployment proved the GitOps loop
- Daidalos is monitored like the other nodes and has its own host folder in the repo

### Hosts and workstations

- Zephyros runs its own monitoring stack and makes all Mnemosyne services reachable from its network
- Mnemosyne keeps the Docker and containerd roots on the NVMe boot SSD
- A host-aware shell prompt with git state on all Pis and Daidalos
- A guided system update runner with a Markdown report and kernel reboot detection; apticron config shared by all Pis
- AstraeusNX and Proteus are monitored, and a hardware health check suite covers CPU cooling, GPU, RAM and disks

### Repository

- Public GitHub mirror, exported through a sanitizer with a path blocklist
- All docs in English, with the wiki synced from the repo
- Claude Code skills for change review, post-deploy checks, container updates, mirror review, host lifecycle and releases

## v1.1.0 — Viessmann Heating Integration (2026-04-26)

Monitoring for the oil boiler through an Optolink adapter on a new dedicated
host. Temperatures, burner and pump state and the heating curve land in
Prometheus, and a Grafana dashboard could adjust the heating curve.

### Hosts

- Hephaestus (Raspberry Pi 3B, Debian Trixie) joins as the heating integration host, with Node Exporter and Tailscale
- vcontrold, built from source, talks to the Vitotronic controller over USB Optolink

### Monitoring

- Textfile exporter with 16 heating metrics: temperatures, burner, pumps, operating mode and heating curve parameters
- Supply temperature setpoint calculated for the current outdoor temperature, plus a lookup table from −20 °C to +20 °C
- Room setpoint read live from the controller instead of a hardcoded value
- 60 s scrape interval to fit the slow controller protocol

### Control

- Flask control API for heating curve, setpoints and operating mode, with serialized writes, retries and a persistent change log; disabled by default
- Grafana panels for heating curve, setpoints and change log, served through Caddy so the API token stays on the server

## v1.0.0 — Core Infrastructure (2026-04-07)

The first milestone: Mnemosyne runs the core services behind Caddy, Boreas
filters DNS for the home network, and every stack deploys from Git.

### Services

- Caddy reverse proxy with an internal CA
- Vaultwarden, Nextcloud with external access via deSEC DynDNS, Gitea, Calibre-Web with KOSync, Syncthing for the notes vault

### Monitoring

- Prometheus and Grafana with 11 dashboards
- Alertmanager with ntfy notifications
- Diun image update notifications
- Solar integration for the Huawei inverter

### DNS

- Pi-hole v6 with Unbound as recursive resolver on Boreas, with a Prometheus exporter

### Operations

- A Gitea webhook pulls and restarts stacks on push
- Daily backups with verification and a Grafana dashboard export script
- Secrets in `.env` files on the hosts, never committed
- README, ARCHITECTURE, SERVICES and RUNBOOK
