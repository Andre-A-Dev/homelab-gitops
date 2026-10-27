#!/usr/bin/env bash
# zephyros-postmortem.sh — collect evidence about the last outage on Zephyros.
#
# Usage:   sudo ./zephyros-postmortem.sh
# Output:  /home/andre/postmortem-<timestamp>.txt
#
# Read-only: changes nothing on the system. Stderr is captured into the
# report on purpose — a failing check is evidence, not noise.

set -u

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

REPORT="/home/andre/postmortem-$(date +%F_%H%M).txt"
BOOTS_BACK=3   # crash boot + failed boot(s) after unplugging
KEYWORDS='oom|killed process|undervoltage|under-voltage|i/o error|ext4-fs|uas|usb disconnect|reset .*usb|xhci|dwc_otg|brcmf|watchdog|emergency|fsck'

section() {
  printf '\n===== %s =====\n' "$1"
}

run() {
  # Print the command, run it via bash so pipes work, keep stderr and exit code.
  printf '$ %s\n' "$1"
  bash -c "$1" 2>&1
  local rc=$?
  [[ $rc -ne 0 ]] && printf '[exit code %d]\n' "$rc"
  return 0
}

decode_throttled() {
  # Decode vcgencmd get_throttled bits (only covers the CURRENT boot).
  local raw val
  raw=$(vcgencmd get_throttled 2>&1) || { echo "$raw"; return; }
  echo "$raw"
  val=$(( ${raw#*=} ))
  (( val & 0x1 ))     && echo "  NOW: under-voltage"
  (( val & 0x2 ))     && echo "  NOW: ARM frequency capped"
  (( val & 0x4 ))     && echo "  NOW: throttled"
  (( val & 0x8 ))     && echo "  NOW: soft temperature limit"
  (( val & 0x10000 )) && echo "  SINCE BOOT: under-voltage occurred"
  (( val & 0x20000 )) && echo "  SINCE BOOT: frequency capping occurred"
  (( val & 0x40000 )) && echo "  SINCE BOOT: throttling occurred"
  (( val & 0x80000 )) && echo "  SINCE BOOT: soft temperature limit occurred"
  (( val == 0 ))      && echo "  OK: no power or thermal issues since boot"
  return 0
}

{
  echo "Zephyros post-mortem — generated $(date -Is)"

  section "1. Boot history"
  run "uptime -s"
  # Real boot time (uptime -s) vs. first journal entry: a gap means the clock
  # started from the last saved time (no RTC) — that saved time ≈ moment of death.
  run "timedatectl show -p NTPSynchronized -p TimeUSec"
  run "journalctl --list-boots --no-pager | tail -n 10"
  # Storage=volatile anywhere in here means earlier boots are lost.
  run "systemd-analyze cat-config systemd/journald.conf | grep -iE '^\\s*Storage|^# /'"

  for i in $(seq 1 "$BOOTS_BACK"); do
    section "2.$i Boot -$i: last 60 lines before it ended"
    run "journalctl -b -$i -n 60 --no-pager"

    section "3.$i Boot -$i: kernel hits (power, USB/SSD, filesystem, OOM, WLAN)"
    run "journalctl -k -b -$i --no-pager | grep -iE '$KEYWORDS' | tail -n 40"

    section "4.$i Boot -$i: errors and worse"
    run "journalctl -b -$i -p err --no-pager | tail -n 40"
  done

  section "5. Power and thermal (current boot only)"
  decode_throttled
  run "vcgencmd measure_temp"
  run "journalctl -k -b 0 --no-pager | grep -iE '$KEYWORDS' | tail -n 40"

  section "6. Storage (USB SSD)"
  run "findmnt /"
  run "lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,MODEL,TRAN"
  # Driver per port: 'uas' vs 'usb-storage' — some USB-SATA bridges misbehave with UAS.
  run "lsusb -t"
  run "lsusb"
  run "df -h /"
  run "tune2fs -l \"\$(findmnt -no SOURCE /)\" | grep -iE 'state|error|last checked|mount count'"
  if command -v smartctl >/dev/null; then
    run "smartctl -a -d sat /dev/\$(lsblk -no PKNAME \"\$(findmnt -no SOURCE /)\")"
  else
    echo "smartctl not installed (apt install smartmontools)"
  fi

  section "7. Memory"
  run "free -h"
  run "swapon --show"

  section "8. Network / WLAN"
  run "ip -br addr"
  run "iw dev wlan0 link"
  run "rfkill list"
  run "nmcli -t device status"
  run "nmcli -t connection show"
  run "journalctl -b -u NetworkManager -u wpa_supplicant --no-pager | grep -iE 'wlan0|wpa|supplicant' | tail -n 30"
  run "journalctl -b -1 -u NetworkManager --no-pager | tail -n 30"
  run "ping -c 3 -W 2 192.168.1.x"

  section "9. Tailscale"
  run "systemctl status tailscaled --no-pager | head -n 15"
  run "tailscale status"
  run "journalctl -b -1 -u tailscaled --no-pager | tail -n 30"

  section "10. Services"
  run "systemctl --failed --no-pager"
  run "systemctl is-active pihole-FTL unbound pihole6-exporter"
  run "docker ps -a --format 'table {{.Names}}\t{{.Status}}'"

  section "11. Recent changes"
  run "uname -r"
  run "grep -A3 -E '^Start-Date' /var/log/apt/history.log | tail -n 40"
} > "$REPORT" 2>&1

chown andre:andre "$REPORT"
echo "Report written to $REPORT"
