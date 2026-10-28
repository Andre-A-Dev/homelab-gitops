# Daidalos

Dell OptiPlex 7070 Micro (i5-9500T, 16 GB, x86_64) at **192.168.1.x**,
Debian 13. Single-node k3s cluster for learning Kubernetes; production stays
on Docker Compose on Mnemosyne. The name is tracked in Prometheus as
`daidalos`.

There is no Docker on this host. Everything outside the cluster is a native
install or a Debian package.

## Role

| Service | How | Port |
|---|---|---|
| k3s server (API) | `get.k3s.io` installer, traefik + servicelb disabled | 6443 |
| kubelet | part of k3s | 10250 |
| Flux | in the cluster, reconciles `clusters/daidalos/` | – |
| node-exporter | Debian `prometheus-node-exporter` | 9100 |
| smartctl_exporter | Debian `prometheus-smartctl-exporter` | 9633 |
| webhook | Debian `webhook`, systemd | 9000 |
| Tailscale | native install | – |

Prometheus on Mnemosyne scrapes `:9100` (job `node-exporter`) and `:9633`
(job `smartctl`).

## What is tracked here

### `monitoring/prometheus-node-exporter` — node-exporter flags

Mirror of `/etc/default/prometheus-node-exporter`. Enables the `cpu.info` and
`systemd` collectors and filters the per-pod mounts and veth/flannel devices
that k3s creates; without the filters the series count grows with every pod.

```bash
# Deploy (manual, nothing applies it):
sudo cp monitoring/prometheus-node-exporter /etc/default/prometheus-node-exporter
sudo systemctl restart prometheus-node-exporter
```

The Debian package reads textfile metrics from `/var/lib/prometheus/node-exporter`,
not from `/var/lib/node_exporter/textfile_collector` like the node-exporter
containers on the Pis. Scripts writing `.prom` files need that path (see
`system-update` below).

### `scripts/bootstrap.sh` — base provisioning

Idempotent setup of a fresh Debian 13 install: packages, swap off, Intel VAAPI
drivers, Longhorn prerequisites, k3s, kubeconfig. Flux bootstrap and SSH
hardening are deliberately left out; the script prints them as next steps.

```bash
sudo ./scripts/bootstrap.sh
```

## Tracked elsewhere

| Path | What | On the host |
|---|---|---|
| `clusters/daidalos/` | Kubernetes manifests | applied by Flux, not by the webhook |
| `shared/scripts/system-update.sh` | monthly update | symlink `/usr/local/bin/system-update` |
| `shared/scripts/gitea-pull.sh` | cron pull | symlink `~/bin/gitea-pull.sh` |
| `shared/scripts/gitea-webhook-handler.sh` | webhook pull | called by `webhook` |
| `shared/apticron/apticron.conf` | apticron settings | symlink `/etc/apticron/apticron.conf` |

## Set up by hand (not in Git)

After `bootstrap.sh`, in this order:

1. **node-exporter.** Install without recommends:

   ```bash
   sudo apt install --no-install-recommends prometheus-node-exporter prometheus-node-exporter-collectors
   ```

   The recommends pull in `ipmitool` and `openipmi`. This machine has no BMC,
   so `openipmi.service` fails on every boot (removed 2026-10-24). The
   `collectors` package provides the `apt` textfile timer. Then deploy
   `monitoring/prometheus-node-exporter` as above.
2. **smartctl_exporter:** `sudo apt install prometheus-smartctl-exporter`,
   default flags.
3. **apticron:** symlink plus `/etc/apticron/apticron.local.conf`, see
   `wiki/Update-Notifications.md`.
4. **system-update:** symlink per `shared/README.md`, then
   `/etc/system-update.conf` with `TEXTFILE_DIR=/var/lib/prometheus/node-exporter`
   and `NTFY_TOPIC`.
5. **Repo pulls:** clone to `~/homelab-infra`, crontab
   `*/15 * * * * /home/<user>/bin/gitea-pull.sh`. Webhook as on the Pis:
   `sudo apt install webhook`, unit `/etc/systemd/system/webhook.service`
   (`-hooks ~/homelab-infra/shared/webhook/hooks.json -port 9000`), a
   `hooks.json` with only the `gitea-pull` hook and its own secret (mode 600,
   gitignored), and `/var/log/webhook-handler.log` owned by the user. Gitea
   only delivers to addresses in `GITEA__security__ALLOWED_HOST_LIST`
   (`mnemosyne/stacks/gitea/docker-compose.yml`).
6. **Heartbeat:** crontab pings Healthchecks.io every 5 minutes, next to the
   Mnemosyne watchdog. The check UUID lives only in the crontab.
7. **Tailscale.**

## Storage

| Disk | Use |
|---|---|
| `sdb` Intel SSD 256 GB | system: `/boot/efi`, `/`, swap partition `sdb3` |
| `sda` Toshiba HDD 1 TB | unused, old partitions, not mounted; SMART-monitored |

No Longhorn yet, see `.claude/rules/kubernetes.md`.

## Known deviations (2026-10-24)

- **Swap is on.** `bootstrap.sh` comments the swap line out of fstab, but
  `sdb3` carries the GPT swap partition type, so `systemd-gpt-auto-generator`
  activates it on every boot anyway (unit `dev-disk-by\x2ddiskseq-2\x2dpart3.swap`
  from `/run/systemd/generator.late/`). k3s runs anyway.
- **SSH hardening not done.** `sshd_config` sets only `PermitRootLogin no`;
  `PasswordAuthentication` is unset, so the default (`yes`) applies.
  `bootstrap.sh` defers this until key auth has proven stable.
