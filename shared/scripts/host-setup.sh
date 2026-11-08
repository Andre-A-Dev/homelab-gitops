#!/usr/bin/env bash
# host-setup.sh — checks that a server has what this repo expects of it: apt
# packages, links into the checkout, configs copied out of the repo, systemd
# units, cron jobs and host-local files that never enter the repo. Meant for
# an empty host after `git clone`, and as a drift check on a running one.
#
#   host-setup.sh            check only, no root needed; exit 1 if anything
#                            is missing or differs
#   host-setup.sh --apply    first install missing apt packages and create
#                            missing links (sudo), then check
#
# --apply never replaces an existing file or link, never copies configs,
# never enables units and never touches cron: those are reported together
# with the command to fix them. Run as the login user from the checkout.
# Hostname dispatch; a host without a branch below is not configured yet.
set -euo pipefail

if [ "$EUID" -eq 0 ]; then
  echo "Run as your user, not root (HOME and the crontab would be root's)." >&2
  exit 1
fi

APPLY=0
case "${1:-}" in
  "") ;;
  --apply) APPLY=1 ;;
  -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "Usage: $0 [--apply]" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Every list uses "|" as separator, since hints contain ":".
APT_PACKAGES=()     # package
LINKS=()            # repo path|target            (--apply creates missing ones)
COPIES=()           # repo path|target            (compared byte for byte)
EXECUTABLES=()      # path|how to install         (not from the default apt repos)
PY_MODULES=()       # module|pip package
UNITS=()            # unit|enabled or disabled
FILES=()            # path|hint                   (existence only, never read)
GROUPS_NEEDED=()    # group of the login user
BOOT_CONFIG=""
BOOT_LINES=()       # exact line in $BOOT_CONFIG
USER_CRON=()        # fixed string|crontab line to add
ROOT_CRON=()        # fixed string|crontab line to add

HOST=$(uname -n | tr '[:upper:]' '[:lower:]')
case "$HOST" in
  hephaestus)
    APT_PACKAGES=(
      git curl
      docker.io docker-compose         # Debian packages, not docker-ce: Pi 3, 32-bit Raspbian
      skopeo                           # check-container-updates.sh
      webhook                          # Gitea repo webhook
      apticron msmtp msmtp-mta         # wiki/Update-Notifications.md
      bc                               # viessmann-exporter.sh
      fbset wireless-tools raspi-utils-core   # hephaestus-display.sh: con2fbmap, iwconfig, vcgencmd
      python3-flask python3-flask-cors # viessmann-api.py
      python3-pil python3-pip          # hephaestus-oled.py
      build-essential cmake libxml2-dev     # vcontrold build, wiki/Viessmann-Optolink.md
    )
    LINKS=(
      "shared/scripts/system-update.sh|/usr/local/bin/system-update"
      "shared/scripts/check-container-updates.sh|/usr/local/bin/check-container-updates.sh"
      "shared/scripts/gitea-pull.sh|$HOME/bin/gitea-pull.sh"
      "shared/apticron/apticron.conf|/etc/apticron/apticron.conf"
      "hephaestus/scripts/viessmann-exporter.sh|/usr/local/bin/viessmann-exporter.sh"
    )
    COPIES=(
      "hephaestus/scripts/hephaestus-display.sh|/usr/local/bin/hephaestus-display.sh"
      "hephaestus/scripts/hephaestus-oled.py|/usr/local/bin/hephaestus-oled.py"
      "hephaestus/vcontrold/vcontrold.xml|/etc/vcontrold/vcontrold.xml"
      "hephaestus/vcontrold/vito.xml|/etc/vcontrold/vito.xml"
      "hephaestus/docker/daemon.json|/etc/docker/daemon.json"
      "hephaestus/systemd/vcontrold.service|/etc/systemd/system/vcontrold.service"
      "hephaestus/systemd/viessmann-api.service|/etc/systemd/system/viessmann-api.service"
      "hephaestus/systemd/hephaestus-display.service|/etc/systemd/system/hephaestus-display.service"
      "hephaestus/systemd/hephaestus-oled.service|/etc/systemd/system/hephaestus-oled.service"
    )
    EXECUTABLES=(
      "/usr/local/sbin/vcontrold|build from source, wiki/Viessmann-Optolink.md"
      "/usr/local/bin/vclient|built together with vcontrold"
      "/usr/bin/tailscale|Tailscale apt repo (pkgs.tailscale.com), then tailscale up"
    )
    PY_MODULES=(
      "adafruit_ssd1306|adafruit-circuitpython-ssd1306"
      "board|Adafruit-Blinka"
    )
    UNITS=(
      "docker.service|enabled"
      "tailscaled.service|enabled"
      "webhook.service|enabled"
      "vcontrold.service|enabled"
      "hephaestus-display.service|enabled"
      "hephaestus-oled.service|enabled"
      "viessmann-api.service|disabled"   # write errors, see hephaestus/README.md
    )
    FILES=(
      "/etc/systemd/system/webhook.service|host-local unit, not in the repo"
      "$REPO_ROOT/shared/webhook/hooks.json|from shared/webhook/hooks.json.example, with the HMAC secret"
      "/etc/system-update.conf|shared/README.md, system-update.sh"
      "/var/log/system-update.log|shared/README.md, Installation"
      "/etc/apticron/apticron.local.conf|recipients, wiki/Update-Notifications.md"
      "/var/log/webhook-handler.log|written by gitea-webhook-handler.sh as the login user"
      "/var/lib/node_exporter/textfile_collector|sudo mkdir -p /var/lib/node_exporter/textfile_collector"
      "/boot/firmware/overlays/tft35a.dtbo|overlay of the 3.5\" TFT driver"
    )
    GROUPS_NEEDED=(docker)
    BOOT_CONFIG=/boot/firmware/config.txt
    BOOT_LINES=(
      "dtparam=i2c_arm=on"               # OLED
      "dtparam=spi=on"                   # TFT
      "dtoverlay=tft35a:rotate=270"      # TFT
    )
    USER_CRON=(
      "gitea-pull.sh|*/15 * * * * $HOME/bin/gitea-pull.sh"
      "hc-ping.com|Healthchecks ping, URL in Healthchecks.io"
    )
    ROOT_CRON=(
      "viessmann-exporter.sh|* * * * * /usr/local/bin/viessmann-exporter.sh"
    )
    ;;
  boreas)
    APT_PACKAGES=(
      git curl
      docker.io docker-compose         # Debian packages, not docker-ce: Pi 3, 32-bit Raspbian
      skopeo                           # check-container-updates.sh
      webhook                          # Gitea repo webhook
      apticron msmtp msmtp-mta         # wiki/Update-Notifications.md; msmtp also mails for Pi-hole
      unbound                          # wiki/Pi-hole-Unbound.md
      rsync openssh-client             # backup-boreas.sh
      python3-prometheus-client python3-requests python3-psutil # pihole6-exporter
    )
    LINKS=(
      "shared/scripts/system-update.sh|/usr/local/bin/system-update"
      "shared/scripts/check-container-updates.sh|/usr/local/bin/check-container-updates"
      "shared/scripts/check-container-updates.sh|/usr/local/bin/check-container-updates.sh"
      "shared/scripts/gitea-pull.sh|$HOME/bin/gitea-pull.sh"
      "shared/apticron/apticron.conf|/etc/apticron/apticron.conf"
      "boreas/scripts/backup-boreas.sh|/usr/local/bin/backup-boreas.sh"
    )
    COPIES=(
      "boreas/docker/daemon.json|/etc/docker/daemon.json"
      "boreas/systemd/pihole6-exporter.service.d/override.conf|/etc/systemd/system/pihole6-exporter.service.d/override.conf"
    )
    EXECUTABLES=(
      "/usr/local/bin/pihole|Pi-hole installer, then restore its config, wiki/Pi-hole-Unbound.md"
      "/usr/bin/tailscale|Tailscale apt repo (pkgs.tailscale.com), then tailscale up"
    )
    UNITS=(
      "docker.service|enabled"
      "tailscaled.service|enabled"
      "webhook.service|enabled"
      "pihole-FTL.service|enabled"
      "unbound.service|enabled"
      "pihole6-exporter.service|enabled"
    )
    FILES=(
      "/etc/systemd/system/webhook.service|host-local unit, not in the repo"
      "$REPO_ROOT/shared/webhook/hooks.json|from shared/webhook/hooks.json.example, with the HMAC secret"
      "/etc/unbound/unbound.conf.d/pi-hole.conf|wiki/Pi-hole-Unbound.md"
      "/etc/systemd/system/pihole6-exporter.service|host-local unit, not in the repo"
      "/etc/pihole6-exporter.env|from boreas/pihole6-exporter/pihole6-exporter.env.example"
      "$HOME/pihole6_exporter_with_queries_time|exporter checkout the unit runs from, boreas/README.md"
      "/etc/msmtprc|credentials, boreas/README.md"
      "$HOME/.ssh/backup_key|key for backup-boreas.sh, boreas/README.md"
      "/etc/system-update.conf|shared/README.md, system-update.sh"
      "/var/log/system-update.log|shared/README.md, Installation"
      "/etc/apticron/apticron.local.conf|recipients, wiki/Update-Notifications.md"
      "/var/log/webhook-handler.log|written by gitea-webhook-handler.sh as the login user"
      "/var/lib/node_exporter/textfile_collector|sudo mkdir -p /var/lib/node_exporter/textfile_collector"
    )
    GROUPS_NEEDED=(docker)
    USER_CRON=(
      "gitea-pull.sh|*/15 * * * * $HOME/bin/gitea-pull.sh"
      "hc-ping.com|Healthchecks ping, URL in Healthchecks.io"
    )
    ROOT_CRON=(
      "backup-boreas.sh|0 2 * * * /usr/local/bin/backup-boreas.sh"
    )
    ;;
  zephyros)
    APT_PACKAGES=(
      git curl
      skopeo                           # check-container-updates.sh
      webhook                          # Gitea repo webhook
      apticron msmtp msmtp-mta         # wiki/Update-Notifications.md; msmtp also mails for Pi-hole
      unbound                          # wiki/Pi-hole-Unbound.md
      rsync openssh-client             # backup-zephyros.sh
      python3-prometheus-client python3-requests python3-psutil # pihole6-exporter
    )
    LINKS=(
      "shared/scripts/system-update.sh|/usr/local/bin/system-update"
      "shared/scripts/check-container-updates.sh|/usr/local/bin/check-container-updates"
      "shared/scripts/check-container-updates.sh|/usr/local/bin/check-container-updates.sh"
      "shared/scripts/gitea-pull.sh|$HOME/bin/gitea-pull.sh"
      "shared/apticron/apticron.conf|/etc/apticron/apticron.conf"
      "zephyros/scripts/backup-zephyros.sh|/usr/local/bin/backup-zephyros.sh"
    )
    COPIES=(
      "zephyros/docker/daemon.json|/etc/docker/daemon.json"
      "zephyros/systemd/pihole6-exporter.service.d/override.conf|/etc/systemd/system/pihole6-exporter.service.d/override.conf"
    )
    EXECUTABLES=(
      "/usr/bin/docker|Docker apt repo (download.docker.com), docker-ce + docker-compose-plugin"
      "/usr/libexec/docker/cli-plugins/docker-compose|docker-compose-plugin from the Docker apt repo"
      "/usr/local/bin/pihole|Pi-hole installer, then restore its config, wiki/Pi-hole-Unbound.md"
      "/usr/bin/tailscale|Tailscale apt repo (pkgs.tailscale.com), then tailscale up"
    )
    UNITS=(
      "docker.service|enabled"
      "tailscaled.service|enabled"
      "webhook.service|enabled"
      "pihole-FTL.service|enabled"
      "unbound.service|enabled"
      "pihole6-exporter.service|enabled"
    )
    FILES=(
      "/etc/systemd/system/webhook.service|host-local unit, not in the repo"
      "$REPO_ROOT/shared/webhook/hooks.json|from shared/webhook/hooks.json.example, with the HMAC secret"
      "/etc/unbound/unbound.conf.d/pi-hole.conf|wiki/Pi-hole-Unbound.md"
      "/etc/systemd/system/pihole6-exporter.service|host-local unit, not in the repo"
      "/etc/pihole6-exporter.env|from boreas/pihole6-exporter/pihole6-exporter.env.example, PIHOLE_HOST on :8081"
      "$HOME/pihole6_exporter_with_queries_time|exporter checkout the unit runs from, zephyros/README.md"
      "/etc/msmtprc|credentials, zephyros/README.md"
      "$HOME/.ssh/backup_key|key for backup-zephyros.sh, zephyros/README.md"
      "$REPO_ROOT/zephyros/monitoring/.env|from zephyros/monitoring/.env.example (FRITZ_*)"
      "$REPO_ROOT/zephyros/caddy/data/caddy/pki|Caddy's internal CA: restore it from the Mnemosyne backup, a new CA means re-trusting it on every client"
      "/etc/ssh/sshd_config.d/10-no-password.conf|RUNBOOK \"Disable SSH Password Login\""
      "/etc/system-update.conf|shared/README.md, system-update.sh"
      "/var/log/system-update.log|shared/README.md, Installation"
      "/etc/apticron/apticron.local.conf|recipients, wiki/Update-Notifications.md"
      "/var/log/webhook-handler.log|written by gitea-webhook-handler.sh as the login user"
      "/var/lib/node_exporter/textfile_collector|sudo mkdir -p /var/lib/node_exporter/textfile_collector"
    )
    GROUPS_NEEDED=(docker)
    USER_CRON=(
      "gitea-pull.sh|*/15 * * * * $HOME/bin/gitea-pull.sh"
    )
    ROOT_CRON=(
      "backup-zephyros.sh|nightly /usr/local/bin/backup-zephyros.sh, zephyros/README.md"
    )
    ;;
  daidalos)
    # No Docker here; k3s, Flux and node-exporter details: daidalos/README.md.
    APT_PACKAGES=(
      git curl smartmontools
      webhook                          # Gitea repo webhook
      apticron msmtp msmtp-mta         # wiki/Update-Notifications.md
      prometheus-node-exporter prometheus-node-exporter-collectors # --no-install-recommends: no ipmitool/openipmi
      prometheus-smartctl-exporter
      open-iscsi nfs-common            # Longhorn prerequisites, bootstrap.sh
      intel-media-va-driver-non-free vainfo intel-gpu-tools # VAAPI, bootstrap.sh
    )
    LINKS=(
      "shared/scripts/system-update.sh|/usr/local/bin/system-update"
      "shared/scripts/gitea-pull.sh|$HOME/bin/gitea-pull.sh"
      "shared/apticron/apticron.conf|/etc/apticron/apticron.conf"
    )
    COPIES=(
      "daidalos/monitoring/prometheus-node-exporter|/etc/default/prometheus-node-exporter"
    )
    EXECUTABLES=(
      "/usr/local/bin/k3s|daidalos/scripts/bootstrap.sh"
      "/usr/local/bin/flux|Flux CLI binary, see daidalos/scripts/bootstrap.sh next steps"
      "/usr/bin/tailscale|Tailscale apt repo (pkgs.tailscale.com), then tailscale up"
    )
    UNITS=(
      "k3s.service|enabled"
      "tailscaled.service|enabled"
      "webhook.service|enabled"
      "prometheus-node-exporter.service|enabled"
      "prometheus-node-exporter-apt.timer|enabled"
      "smartctl_exporter.service|enabled"
    )
    FILES=(
      "/etc/systemd/system/webhook.service|host-local unit, not in the repo"
      "$REPO_ROOT/shared/webhook/hooks.json|only the gitea-pull hook, daidalos/README.md"
      "/etc/rancher/k3s/k3s.yaml|written by k3s, daidalos/scripts/bootstrap.sh"
      "/etc/msmtprc|credentials, wiki/Update-Notifications.md"
      "/etc/ssh/sshd_config.d/10-no-password.conf|RUNBOOK \"Disable SSH Password Login\""
      "/etc/system-update.conf|TEXTFILE_DIR=/var/lib/prometheus/node-exporter, daidalos/README.md"
      "/var/log/system-update.log|shared/README.md, Installation"
      "/etc/apticron/apticron.local.conf|recipients, wiki/Update-Notifications.md"
      "/var/log/webhook-handler.log|written by gitea-webhook-handler.sh as the login user"
      "/var/lib/prometheus/node-exporter|created by prometheus-node-exporter"
    )
    USER_CRON=(
      "gitea-pull.sh|*/15 * * * * $HOME/bin/gitea-pull.sh"
      "hc-ping.com|Healthchecks ping, URL in Healthchecks.io"
    )
    ;;
  *)
    echo "No host branch for '$HOST' in $0 yet (configured: hephaestus, boreas, zephyros, daidalos)." >&2
    exit 2
    ;;
esac

PROBLEMS=0

# say <STATUS> <text> [hint]
say() {
  printf '%-8s%s\n' "$1" "$2"
  if [ -n "${3:-}" ]; then
    printf '        -> %s\n' "$3"
  fi
  case "$1" in
    MISSING|WRONG) PROBLEMS=$((PROBLEMS + 1)) ;;
  esac
}

pkg_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed' # intentional: an unknown package is "not installed"
}

missing_packages() {
  local p
  for p in "${APT_PACKAGES[@]}"; do
    pkg_installed "$p" || echo "$p"
  done
}

apply_packages() {
  local missing
  mapfile -t missing < <(missing_packages)
  if [ "${#missing[@]}" -eq 0 ]; then
    echo "apt: nothing to install"
    return
  fi
  echo "apt: installing ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends "${missing[@]}"
}

# Creates absent links only. An existing file or a link elsewhere is left
# alone and reported by check_links: it may be a deliberate local change.
apply_links() {
  local entry src dst
  for entry in "${LINKS[@]}"; do
    src="$REPO_ROOT/${entry%%|*}"
    dst="${entry#*|}"
    if [ -e "$dst" ] || [ -L "$dst" ]; then
      continue
    fi
    echo "link: $dst -> $src"
    if [ "${dst#"$HOME"/}" != "$dst" ]; then
      mkdir -p "$(dirname "$dst")"
      ln -s "$src" "$dst"
    else
      sudo mkdir -p "$(dirname "$dst")"
      sudo ln -s "$src" "$dst"
    fi
  done
}

check_packages() {
  local p
  for p in "${APT_PACKAGES[@]}"; do
    if pkg_installed "$p"; then
      say OK "apt $p"
    else
      say MISSING "apt $p" "host-setup.sh --apply"
    fi
  done
}

check_links() {
  local entry src dst sudo
  for entry in "${LINKS[@]}"; do
    src="$REPO_ROOT/${entry%%|*}"
    dst="${entry#*|}"
    # same rule as apply_links: below $HOME the link belongs to the user
    sudo="sudo "
    if [ "${dst#"$HOME"/}" != "$dst" ]; then
      sudo=""
    fi
    if [ ! -e "$src" ]; then
      say WRONG "link $dst" "source missing in the repo: $src"
    elif [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$src")" ]; then
      say OK "link $dst"
    elif [ -L "$dst" ]; then
      say WRONG "link $dst points to $(readlink "$dst")" "should point to $src"
    elif [ -e "$dst" ]; then
      say WRONG "link $dst is a regular file, not a link" "diff $dst $src, then: ${sudo}ln -sf $src $dst"
    else
      say MISSING "link $dst" "host-setup.sh --apply"
    fi
  done
}

check_copies() {
  local entry src dst
  for entry in "${COPIES[@]}"; do
    src="$REPO_ROOT/${entry%%|*}"
    dst="${entry#*|}"
    if [ ! -e "$dst" ]; then
      say MISSING "copy $dst" "sudo cp $src $dst"
    elif [ ! -r "$dst" ]; then
      say SKIP "copy $dst (not readable)"
    elif cmp -s "$src" "$dst"; then
      say OK "copy $dst"
    else
      say WRONG "copy $dst differs from the repo" "diff $src $dst"
    fi
  done
}

check_executables() {
  local entry path
  for entry in "${EXECUTABLES[@]}"; do
    path="${entry%%|*}"
    if [ -x "$path" ]; then
      say OK "binary $path"
    else
      say MISSING "binary $path" "${entry#*|}"
    fi
  done
}

check_py_modules() {
  local entry mod
  for entry in "${PY_MODULES[@]}"; do
    mod="${entry%%|*}"
    # intentional: the ImportError traceback is the expected "missing" case
    if python3 -c "import $mod" > /dev/null 2>&1; then
      say OK "python $mod"
    else
      say MISSING "python $mod" "pip package ${entry#*|}"
    fi
  done
}

check_units() {
  local entry unit want state
  for entry in "${UNITS[@]}"; do
    unit="${entry%%|*}"
    want="${entry#*|}"
    # is-enabled exits non-zero for "disabled" too; the printed state counts
    state=$(systemctl is-enabled "$unit" 2>&1 || true)
    if [ "$state" = "$want" ]; then
      say OK "unit $unit $state"
    elif [ "$want" = enabled ]; then
      say WRONG "unit $unit $state, expected enabled" "sudo systemctl enable --now $unit"
    else
      say WRONG "unit $unit $state, expected disabled" "sudo systemctl disable --now $unit"
    fi
  done
}

check_files() {
  local entry path
  for entry in "${FILES[@]}"; do
    path="${entry%%|*}"
    if [ -e "$path" ]; then
      say OK "file $path"
    # A path below a root-only directory (Caddy's data/) is invisible to the
    # login user; ask root if sudo is already cached. stderr dropped: "a
    # password is required" is the expected no-cache case.
    elif sudo -n true 2> /dev/null; then
      if sudo -n test -e "$path"; then
        say OK "file $path (root only)"
      else
        say MISSING "file $path" "${entry#*|}"
      fi
    else
      say MISSING "file $path" "${entry#*|} (if it sits in a root-only directory: run sudo -v first)"
    fi
  done
}

check_groups() {
  local g
  for g in "${GROUPS_NEEDED[@]}"; do
    if id -nG | tr ' ' '\n' | grep -qx "$g"; then
      say OK "group $g"
    else
      say MISSING "group $g" "sudo usermod -aG $g $USER, then log in again"
    fi
  done
}

check_boot() {
  local line
  [ -n "$BOOT_CONFIG" ] || return 0
  if [ ! -e "$BOOT_CONFIG" ]; then
    say MISSING "boot $BOOT_CONFIG"
    return 0
  fi
  if [ ! -r "$BOOT_CONFIG" ]; then
    say SKIP "boot $BOOT_CONFIG (not readable)"
    return 0
  fi
  for line in "${BOOT_LINES[@]}"; do
    if grep -qxF "$line" "$BOOT_CONFIG"; then
      say OK "boot $line"
    else
      say MISSING "boot $line" "add to $BOOT_CONFIG, then reboot"
    fi
  done
}

# check_cron <label> <crontab text> <entry>...
check_cron() {
  local label="$1" tab="$2" entry
  shift 2
  for entry in "$@"; do
    if printf '%s\n' "$tab" | grep -v '^[[:space:]]*#' | grep -qF "${entry%%|*}"; then
      say OK "cron ($label) ${entry%%|*}"
    else
      say MISSING "cron ($label) ${entry%%|*}" "${entry#*|}"
    fi
  done
}

check_crontabs() {
  local tab
  if [ "${#USER_CRON[@]}" -gt 0 ]; then
    # "no crontab for <user>" goes to stderr and means: nothing scheduled
    tab=$(crontab -l 2>&1 || true)
    check_cron "$USER" "$tab" "${USER_CRON[@]}"
  fi
  if [ "${#ROOT_CRON[@]}" -gt 0 ]; then
    if tab=$(sudo -n crontab -l -u root 2>&1); then
      check_cron root "$tab" "${ROOT_CRON[@]}"
    else
      say SKIP "cron (root): needs sudo" "sudo crontab -l -u root, or run sudo -v first"
    fi
  fi
}

echo "host-setup: $HOST, repo $REPO_ROOT"
if [ "$APPLY" -eq 1 ]; then
  apply_packages
  apply_links
fi
echo

check_packages
check_links
check_copies
check_executables
check_py_modules
check_units
check_files
check_groups
check_boot
check_crontabs

echo
if [ "$PROBLEMS" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$PROBLEMS problem(s) found."
  exit 1
fi
