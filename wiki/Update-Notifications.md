# Update Notifications

No auto-updates. The philosophy is: get notified, review the changelog, update deliberately. A silent breaking update to Vaultwarden or Caddy at 2 AM is not acceptable.

Three tools handle this:

- **Diun** -- monitors Docker images, sends a push notification when a new image is available
- **apticron** -- monitors apt packages, sends an email (and, via ntfy's e-mail publishing, a push) when new system updates are available
- **check-container-updates.sh** -- supplementary script that writes container update state as Prometheus metrics via the textfile collector, making update availability visible in Grafana alongside service health

Alertmanager handles a separate class of notifications: infrastructure alerts (disk space, service down, heating pump faults) routed from Prometheus to ntfy topics. These are operational alerts, not update notifications.

---

## Architecture

```
Mnemosyne:
  new Docker image   ->  Diun  ->  ntfy (push)  ->  Android
  container state    ->  check-container-updates.sh  ->  textfile collector  ->  Grafana
  apt packages       ->  apticron  ->  email + ntfy (e-mail publishing)
  firing alerts      ->  Prometheus  ->  Alertmanager  ->  ntfy (push)  ->  Android

Boreas / Zephyros / Hephaestus:
  apt packages       ->  apticron  ->  email
  (their containers are not watched by Diun)
```

---

## Diun -- Docker image update notifications

Diun watches all running containers and checks their registries for newer images. When a new image is found, it sends a notification via ntfy.

The watch schedule is set to weekly (Monday 08:00) -- daily would generate too much noise.

`WATCHBYDEFAULT=true` covers all containers automatically without needing to label each one individually.

### docker-compose.yml

```yaml
services:
  diun:
    image: crazymax/diun:latest
    container_name: diun
    volumes:
      - diun-data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TZ=Europe/Berlin
      - DIUN_WATCH_WORKERS=20
      - DIUN_WATCH_SCHEDULE=0 8 * * 1        # Mondays at 08:00
      - DIUN_PROVIDERS_DOCKER=true
      - DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true
      - DIUN_NOTIF_NTFY_ENDPOINT=https://ntfy.sh
      - DIUN_NOTIF_NTFY_TOPIC=${NTFY_DIUN_TOPIC}   # set in .env
    restart: unless-stopped

volumes:
  diun-data:
```

### Useful commands

```bash
# Check current image state
docker exec diun diun image list

# Send a test notification immediately
docker exec diun diun notif test

# Trigger an immediate check of all images
docker exec diun diun image update

# Logs
docker logs diun -f
```

---

## ntfy -- push notifications

ntfy is a minimal pub/sub push service. Sending a notification is a single `curl` call. The Android app receives it like a normal system push notification.

The public `ntfy.sh` instance works without any setup. The topic name acts as a password -- treat it like one. Use a random string, not something guessable like `mnemosyne-updates`.

```bash
# Send a test notification
curl -d "Test from Mnemosyne" https://ntfy.sh/your-topic-name

# With title and priority
curl \
  -H "Title: Mnemosyne Update" \
  -H "Priority: default" \
  -d "New Docker images available" \
  https://ntfy.sh/your-topic-name
```

**Android:** install the ntfy app (Play Store or F-Droid) and subscribe to your topic. Disable battery optimization for the app to ensure reliable delivery.

**Self-hosting:** ntfy can run as a Docker container on Mnemosyne for a fully private setup. Add it to the Diun stack and point `DIUN_NOTIF_NTFY_ENDPOINT` to the internal URL. Requires Tailscale or a reverse proxy for external delivery.

---

## apticron -- system package update notifications

apticron runs once a day (an hourly cron job with `--cron`, which skips if the last run is less than a day old), checks for available apt upgrades, and sends an email with the package list and changelogs. No mail means nothing new -- not an error.

Updates are installed in the monthly window ([[Update-Strategy]]); between windows only security fixes for exposed components are applied. apticron's job is therefore "tell me once what is new", not a daily reminder of the same list.

### Installation

```bash
sudo apt install apticron msmtp msmtp-mta -y
```

### apticron configuration

The settings live in the repo as `shared/apticron/apticron.conf` and are symlinked to `/etc/apticron/apticron.conf` on every Pi and on Daidalos, so a `git pull` applies them with the next daily run. The symlink needs a checkout that is kept current: the Pis pull via webhook and a 15-minute cron job; a host without that ends up with a dangling link, and apticron then silently falls back to its defaults (`EMAIL="root"`, no `DIFF_ONLY`). The recipients stay on the host in `/etc/apticron/apticron.local.conf`, which the shared file sources last:

```ini
# /etc/apticron/apticron.local.conf  (root, mode 600, not in Git)
EMAIL="your@email.com ntfy-<topic>@ntfy.sh"
```

`EMAIL` takes several space-separated recipients. ntfy.sh turns a mail to `ntfy-<topic>@ntfy.sh` into a push notification with the mail subject as its title; the `ntfy-` prefix is part of the address, the bare topic does not work. The topic acts as a password, which is why it stays in the host-local file. Test it by loading the configuration exactly the way apticron does - through the symlink, so a dangling link (an outdated checkout) shows up as a mail to `root` instead of a false success:

```bash
sudo bash -e -c '. /usr/lib/apticron/apticron.conf; [ -e /etc/apticron/apticron.conf ] && . /etc/apticron/apticron.conf; echo "apticron config test from $(hostname)" | mailx -s "[apticron] $(hostname): config test" $EMAIL'
```

Tested on Mnemosyne 2026-10-24: mail and push both arrived. All four Pis and Daidalos use one shared topic; the host is in the title. ntfy cuts the mail body to 4096 bytes (`server/smtp_server.go`, no attachment for e-mail publishing), so the push carries the subject and the start of the package list; the full report with changelogs is in the mail. `IPADDRESSES="omitted"` in the shared config keeps the host's addresses (public IPv6, Tailscale) out of the report, which now passes through the public ntfy.sh server.

Set up once per host:

```bash
sudo cp -a /etc/apticron/apticron.conf /etc/apticron/apticron.conf.bak
sudo sh -c 'grep "^EMAIL=" /etc/apticron/apticron.conf.bak > /etc/apticron/apticron.local.conf'
sudo chmod 600 /etc/apticron/apticron.local.conf
sudo ln -sf ~/homelab-infra/shared/apticron/apticron.conf /etc/apticron/apticron.conf
```

The shared file sets `NOTIFY_NO_UPDATES="0"`, `DIFF_ONLY="1"` (each pending package is reported once, not every day until the monthly window) and a subject that shows the total number of pending packages. The mail lists only packages not reported before, plus a line "N other packages previously reported to you pending an upgrade". `/etc/apticron/apticron.conf` is not a dpkg conffile, so package upgrades leave the symlink alone. CI runs shellcheck on the shared file: apticron sources it under `bash -e`, so an error would stop the mails on all four hosts.

### Reading the mail: security fix or monthly window?

apticron cannot filter for security updates, and on Boreas and Hephaestus there is nothing to filter by: Raspbian (armhf) has no separate security suite, fixes arrive in `trixie/main`. Mnemosyne and Zephyros (arm64, Debian repos) do have `trixie-security`. The changelogs in the mail work everywhere: an entry with `CVE-` for an exposed component (OpenSSH, OpenSSL, the kernel on Mnemosyne, Caddy-facing libraries) means install now on that host; everything else waits for the window.

### msmtp -- SMTP relay

apticron uses the system MTA to send mail. `msmtp` is the lightest option, forwarding via an external SMTP account.

Create `~/.msmtprc`:

```ini
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        smtp
host           smtp.example.com
port           587
from           your@email.com
user           your@email.com
password       your-app-password

account default : smtp
```

```bash
chmod 600 ~/.msmtprc                        # required -- msmtp refuses to run otherwise

sudo touch /var/log/msmtp.log
sudo chown $(whoami):$(whoami) /var/log/msmtp.log

# apticron runs as root -- it needs its own copy
sudo cp ~/.msmtprc /etc/msmtprc
sudo chmod 600 /etc/msmtprc

# Test
echo "Test from Mnemosyne" | msmtp your@email.com
sudo apticron    # manual run -- sends mail only if updates are available
```

If using Gmail: a regular password is rejected. Enable 2FA and generate an App Password at `myaccount.google.com/apppasswords`. Use that 16-character code in `password`.

---

## Applying updates

When apticron sends a mail or Diun sends a push notification, apply updates manually:

**System packages:**

```bash
sudo apt update
apt list --upgradable 2>/dev/null   # review what changes
sudo apt dist-upgrade -y            # dist-upgrade resolves new dependencies; upgrade skips them
sudo reboot                         # required after kernel updates
```

**Docker images:** follow the update window in [[Update-Strategy]] -- which tag may move on its own, how to verify, and when it is safe to prune old images.

See `RUNBOOK.md` in the repository root for the full update procedure per stack.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Diun sends no notification | Wrong topic name | Check `DIUN_NOTIF_NTFY_TOPIC` in `.env` |
| Diun finds no updates despite new image | `latest` tag cached | `docker exec diun diun image list` shows current state |
| apticron sends no mail | No updates available | `apt list --upgradable` -- empty list is correct |
| apticron sends no mail although updates are pending | `DIFF_ONLY`: already reported in an earlier mail | Expected; `system-update --dry-run` shows the full list |
| apticron sends no mail | msmtp not configured | Send a test mail manually: `echo test \| msmtp your@email.com` |
| msmtp: `authentication failed` | Wrong password or not an app password | Generate an app password, not the account password |
| msmtp: `contains secrets and therefore must have...` | File permissions too open | `chmod 600 ~/.msmtprc` |
| ntfy app receives nothing | Battery optimization active | Disable battery optimization for the ntfy app |
