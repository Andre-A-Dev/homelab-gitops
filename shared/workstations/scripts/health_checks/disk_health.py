#!/usr/bin/env python3
"""
disk_health.py - passive SMART health snapshot for all drives, with wear trends.

No load, no self-tests: reads SMART data of every drive smartctl can open
(takes seconds), judges each drive on its own counters and compares it with
its earlier snapshots. Drives are identified by model + serial number, never
by /dev path - those change between boots.

What it flags
  - SMART overall status failing, NVMe critical warnings, spare below threshold
  - counters that must never grow: reallocated / pending / uncorrectable
    sectors, NVMe media errors (any increase since the last snapshot = FAIL)
  - interface CRC errors growing (usually a cable or USB bridge, not the drive)
  - remaining rated life (NVMe percentage used, Samsung wear levelling count)
    and, once two snapshots are at least 30 days apart, the projected years left
  - drive temperature

Needs root (smartctl):
    sudo ./disk_health.py
    sudo ./disk_health.py --device /dev/sdd:sat     # extra drive, e.g. USB backup disk
Suitable for a weekly systemd timer; exits 2 when any drive is FAIL.
"""

from __future__ import annotations

import argparse
import html
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import health_common as hc
from health_common import CSS, FAIL, INFO, OK, SEVERITY, WARN, check, die, fmt

FILE_PREFIX = "disk"

LIFE_WARN, LIFE_FAIL = 20, 5          # % rated life left
YEARS_WARN = 1.0                      # projected years left at the current wear rate
MIN_TREND_DAYS = 30                   # shorter windows give meaningless wear rates
TEMP_WARN_SSD, TEMP_WARN_HDD = 70, 55

# ATA attributes (raw values) that must stay at zero / never grow
ATA_DEFECTS = {
    5: "reallocated sectors",
    187: "reported uncorrectable",
    197: "pending sectors",
    198: "offline uncorrectable",
}
# Counters that may be non-zero from the factory but must not keep growing
ATA_WATCH = {
    179: "used reserve blocks",
    181: "program fails",
    182: "erase fails",
    183: "runtime bad blocks",
    252: "added bad flash blocks",
}
ATA_CRC = 199
ATA_POWER_CYCLES = 12
ATA_POR_RECOVERY_SAMSUNG = 235        # Samsung SATA: unsafe shutdown count
ATA_WEAR_SAMSUNG = 177                # normalised value = % life left on Samsung SATA SSDs
ATA_LBAS_WRITTEN = 241


# --------------------------------------------------------------------------- #
# smartctl access
# --------------------------------------------------------------------------- #

def smartctl(*args: str) -> tuple[dict, int]:
    res = subprocess.run(["smartctl", "-j", *args], capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        data = {}
    return data, data.get("smartctl", {}).get("exit_status", res.returncode)


def discover(extra: list[str]) -> list[tuple[str, str]]:
    """(device, type) pairs: what smartctl can open plus devices given by hand."""
    data, _ = smartctl("--scan-open")
    found = [(d["name"], d.get("type", "auto")) for d in data.get("devices", [])]
    for spec in extra:
        name, _, dtype = spec.partition(":")
        found = [f for f in found if f[0] != name] + [(name, dtype or "auto")]
    if not found:
        die("smartctl found no drives")
    return found


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def ata_raw(data: dict) -> dict[int, dict]:
    table = data.get("ata_smart_attributes", {}).get("table", [])
    return {a["id"]: a for a in table if "id" in a}


def parse(device: str, dtype: str, data: dict, status: int) -> dict:
    """Reduce smartctl JSON to the values we judge and trend."""
    d = {
        "device": device, "type": dtype,
        "model": data.get("model_name") or data.get("model_family") or "unknown",
        "serial": data.get("serial_number") or "unknown",
        "firmware": data.get("firmware_version"),
        "protocol": data.get("device", {}).get("protocol"),
        "rotational": (data.get("rotation_rate") or 0) > 0,
        "smart_passed": data.get("smart_status", {}).get("passed"),
        "exit_status": status,
        "temperature": data.get("temperature", {}).get("current"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "life_left": None, "tb_written": None, "defects": {}, "watch": {}, "crc_errors": None,
        "power_cycles": None,
        "critical_warning": None, "spare": None, "spare_threshold": None,
        "unsafe_shutdowns": None, "error_log_entries": None, "last_self_test": None,
    }
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        d["life_left"] = max(0, 100 - nvme.get("percentage_used", 0))
        units = nvme.get("data_units_written")
        d["tb_written"] = units * 512_000 / 1e12 if units is not None else None
        d["defects"] = {"media errors": nvme.get("media_errors")}
        d["critical_warning"] = nvme.get("critical_warning")
        d["spare"] = nvme.get("available_spare")
        d["spare_threshold"] = nvme.get("available_spare_threshold")
        d["unsafe_shutdowns"] = nvme.get("unsafe_shutdowns")
        d["power_cycles"] = nvme.get("power_cycles")
        d["error_log_entries"] = nvme.get("num_err_log_entries")
        tests = data.get("nvme_self_test_log", {}).get("table") or []
        if tests:
            d["last_self_test"] = tests[0].get("self_test_result", {}).get("string")
    attrs = ata_raw(data)
    if attrs:
        d["defects"] = {name: attrs[i]["raw"]["value"]
                        for i, name in ATA_DEFECTS.items() if i in attrs}
        d["watch"] = {name: attrs[i]["raw"]["value"] for i, name in ATA_WATCH.items() if i in attrs}
        if ATA_POWER_CYCLES in attrs:
            d["power_cycles"] = attrs[ATA_POWER_CYCLES]["raw"]["value"]
        if ATA_POR_RECOVERY_SAMSUNG in attrs and "samsung" in d["model"].lower():
            d["unsafe_shutdowns"] = attrs[ATA_POR_RECOVERY_SAMSUNG]["raw"]["value"]
        if ATA_CRC in attrs:
            d["crc_errors"] = attrs[ATA_CRC]["raw"]["value"]
        if ATA_WEAR_SAMSUNG in attrs and "samsung" in d["model"].lower():
            d["life_left"] = attrs[ATA_WEAR_SAMSUNG].get("value")
        if ATA_LBAS_WRITTEN in attrs:
            block = data.get("logical_block_size", 512)
            d["tb_written"] = attrs[ATA_LBAS_WRITTEN]["raw"]["value"] * block / 1e12
        tests = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table") or []
        if tests:
            d["last_self_test"] = tests[0].get("status", {}).get("string")
    return d


def key(d: dict) -> str:
    return f"{d['model']}#{d['serial']}"


# --------------------------------------------------------------------------- #
# Judgement
# --------------------------------------------------------------------------- #

def judge(d: dict, history: list[tuple[str, dict]], now: datetime) -> list[dict]:
    """Checks for one drive. history: (timestamp, drive dict) of earlier snapshots."""
    checks = []
    label = f"{d['model']} ({d['serial'][-6:]})"

    def add(name, measured, expected, status, note=""):
        checks.append({**check(f"{label}: {name}", measured, expected, status, note),
                       "drive": key(d)})

    if d["exit_status"] & 0b11:
        add("readable", f"smartctl exit status {d['exit_status']}", "readable", WARN,
            "Could not open the drive - try --device <path>:sat for USB enclosures")
        return checks
    if d["smart_passed"] is False:
        add("SMART overall status", "FAILING", "PASSED", FAIL, "Back up this drive now")
    if d["critical_warning"]:
        add("NVMe critical warning", f"0x{d['critical_warning']:02x}", "0x00", FAIL,
            "The controller reports a critical condition - back up now")
    if d["spare"] is not None and d["spare_threshold"] is not None:
        low = d["spare"] < d["spare_threshold"]
        add("spare blocks", f"{d['spare']} % (threshold {d['spare_threshold']} %)",
            "above threshold", FAIL if low else OK)

    prev = history[-1][1] if history else None
    for name, value in d["defects"].items():
        if value is None:
            continue
        before = (prev or {}).get("defects", {}).get(name)
        grew = before is not None and value > before
        if grew:
            add(name, f"{value} (was {before})", "never grows", FAIL,
                "Counter grew since the last snapshot - the drive is degrading, back up")
        elif value > 0:
            serious = name != "reallocated sectors"
            add(name, str(value), "0", FAIL if serious else WARN,
                "Watch whether this grows" if not serious else "Data integrity at risk")
        else:
            add(name, "0", "0", OK)

    for name, value in d.get("watch", {}).items():
        before = (prev or {}).get("watch", {}).get(name)
        if before is not None and value > before:
            add(name, f"{value} (was {before})", "not growing", WARN,
                "Flash is retiring blocks - watch whether this keeps climbing")
        elif value > 0:
            add(name, f"{value} (baseline)", "not growing", INFO,
                "Non-zero from earlier wear or the factory - only growth matters")

    if d["crc_errors"] is not None:
        before = (prev or {}).get("crc_errors")
        grew = before is not None and d["crc_errors"] > before
        add("interface CRC errors",
            f"{d['crc_errors']}" + (f" (was {before})" if before is not None else ""),
            "not growing", WARN if grew else OK,
            "Usually a loose SATA cable or a flaky USB bridge, not the drive" if grew else "")

    if d["life_left"] is not None:
        life = d["life_left"]
        add("rated life left", f"{life} %", f">= {LIFE_WARN} %",
            FAIL if life < LIFE_FAIL else WARN if life < LIFE_WARN else OK)
        oldest = next(((ts, h) for ts, h in history if h.get("life_left") is not None), None)
        if oldest:
            days = (now - datetime.fromisoformat(oldest[0])).total_seconds() / 86400
            used = oldest[1]["life_left"] - life
            if days >= MIN_TREND_DAYS and used > 0:
                years = life / (used / days) / 365
                d["years_left"] = years
                add("projected life", f"~{years:.1f} years at the current wear rate",
                    f">= {YEARS_WARN:.0f} year", WARN if years < YEARS_WARN else INFO)

    if d["temperature"] is not None:
        limit = TEMP_WARN_HDD if d["rotational"] else TEMP_WARN_SSD
        add("temperature", f"{d['temperature']} C", f"< {limit} C",
            WARN if d["temperature"] >= limit else OK)

    if d["unsafe_shutdowns"] is not None:
        share = (f" of {d['power_cycles']} power cycles "
                 f"({d['unsafe_shutdowns'] / d['power_cycles'] * 100:.0f} %)"
                 if d.get("power_cycles") else "")
        new = (d["unsafe_shutdowns"] - prev["unsafe_shutdowns"]
               if prev and prev.get("unsafe_shutdowns") is not None else None)
        add("unsafe shutdowns",
            f"{d['unsafe_shutdowns']}{share}" + (f", +{new} since last snapshot" if new else ""),
            "rare", INFO, "Power-offs without the drive being told to flush first")
    if d["last_self_test"] and "without error" not in d["last_self_test"].lower():
        add("last self-test", d["last_self_test"], "completed without error", WARN)
    return checks


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def verdict_text(verdict: str) -> str:
    return {OK: "All drives healthy",
            WARN: "Watch it - some drives show early signs",
            FAIL: "Back up now - a drive is degrading"}[verdict]


def print_report(run: dict) -> None:
    print("\n" + "=" * 78)
    print(f" Disk health snapshot  ({run['started'][:16].replace('T', ' ')})")
    print("=" * 78)
    for d in run["drives"]:
        print(f" {d['model']}  [{d['serial']}]  {d['device']}")
        print(f"   life left {fmt(d['life_left'], '.0f')} %   written "
              f"{fmt(d['tb_written'], '.1f')} TB   {fmt(d['temperature'], '.0f')} C   "
              f"{fmt(d['power_on_hours'], '.0f')} h")
        for c in (c for c in run["checks"] if c["drive"] == key(d)):
            if c["status"] != OK:
                name = c["name"].split(": ", 1)[1]
                print(f"   {hc.color(c['status'].ljust(4), c['status'])}  {name}: "
                      f"{c['measured']}"
                      + (f"  -> {c['note']}" if c["note"] and c["status"] in (WARN, FAIL) else ""))
    for missing in run["missing"]:
        print(f" (not seen this time: {missing})")
    print("-" * 78)
    print(" Verdict: " + hc.color(verdict_text(run["verdict"]), run["verdict"]))
    print("=" * 78)


def render_html(run: dict, history_by_drive: dict) -> str:
    esc = html.escape
    sections = []
    for d in run["drives"]:
        table = hc.checks_table([c for c in run["checks"] if c["drive"] == key(d)],
                                label=lambda c: c["name"].split(": ", 1)[1])
        points = [(i, h.get("life_left")) for i, (_, h) in
                  enumerate(history_by_drive.get(key(d), []) + [(run["started"], d)])]
        labels = [(i, ts[2:10]) for i, (ts, _) in
                  enumerate(history_by_drive.get(key(d), []) + [(run["started"], d)])]
        trend = ""
        if sum(1 for _, v in points if v is not None) >= 2:
            trend = ('<p class="sub">Rated life left (%) across snapshots.</p>'
                     '<div class="scroll">' + hc.svg_chart(
                         [{"name": "life", "cls": "coolant", "points": points, "dots": True}],
                         y_unit="%", x_labels=labels) + "</div>")
        sections.append(f"""
<h2>{esc(d['model'])}</h2>
<p class="sub">Serial {esc(d['serial'])}, {esc(d['device'])}, firmware {esc(str(d['firmware']))},
{fmt(d['power_on_hours'], '.0f')} h powered on, {fmt(d['tb_written'], '.1f')} TB written</p>
{table}{trend}""")
    missing = "".join(f"<li>{esc(m)}</li>" for m in run["missing"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Disk health {esc(run['started'][:10])}</title><style>{CSS}</style></head>
<body><main>
<header>{hc.badge(run)}<div><h1>Disk health snapshot</h1>
<p class="sub">{len(run['drives'])} drives on {esc(run['host'])},
{esc(run['started'][:16].replace('T', ' '))}</p>
<p class="verdict v-{run['verdict'].lower()}-text">{esc(verdict_text(run['verdict']))}</p>
</div></header>
{''.join(sections)}
{f'<h2>Not seen this time</h2><ul>{missing}</ul>' if missing else ''}
</main></body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description="Passive SMART health snapshot (needs root).")
    p.add_argument("--device", action="append", default=[],
                   help="extra drive as PATH[:TYPE], e.g. /dev/sdd:sat (repeatable)")
    p.add_argument("--out-dir", type=Path,
                   help=f"results directory (default ~/{hc.RESULTS_DIR} of the sudo user)")
    p.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    args = p.parse_args()
    if not shutil.which("smartctl"):
        die("smartctl not found - install it with: pacman -S smartmontools")
    hc.ensure_root()

    out_dir, uid, gid = hc.prepare_results_dir(args.out_dir)
    now = datetime.now().astimezone()

    history = hc.load_history(out_dir, FILE_PREFIX)
    by_drive: dict[str, list[tuple[str, dict]]] = {}
    for h in history:
        for d in h.get("drives", []):
            by_drive.setdefault(key(d), []).append((h["started"], d))

    run = {"started": now.isoformat(timespec="seconds"), "host": platform.node(),
           "drives": [], "checks": [], "missing": [], "raw": {}}
    for device, dtype in discover(args.device):
        data, status = smartctl("-x", "-d", dtype, device)
        d = parse(device, dtype, data, status)
        run["drives"].append(d)
        run["raw"][key(d)] = data             # kept for later parser fixes
        run["checks"] += judge(d, by_drive.get(key(d), []), now)
    seen = {key(d) for d in run["drives"]}
    run["missing"] = sorted(k for k in by_drive if k not in seen)

    worst = max((SEVERITY[c["status"]] for c in run["checks"]), default=0)
    run["verdict"] = {0: OK, 1: WARN, 2: FAIL}[worst]
    print_report(run)

    stamp = now.strftime("%Y%m%d-%H%M%S")
    report = hc.save_results(out_dir, FILE_PREFIX, stamp, run, render_html(run, by_drive),
                             uid, gid)
    if not args.no_open:
        hc.open_report(report)                 # no-op under the systemd timer (no terminal)
    else:
        print(f" Report: {report}")
    sys.exit(2 if run["verdict"] == FAIL else 0)


if __name__ == "__main__":
    main()
