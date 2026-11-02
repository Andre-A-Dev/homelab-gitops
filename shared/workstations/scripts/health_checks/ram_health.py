#!/usr/bin/env python3
"""
ram_health.py - memory health check with stressapptest, from the running system.

RAM itself hardly wears out, but a tuned EXPO setup can drift into instability
(BIOS updates, ageing memory controller, warmer summers), and BIOS updates
like to silently reset EXPO to slow JEDEC defaults. This check catches both.

Test sequence
  1. Record the configured memory speed per DIMM (dmidecode) and the DIMM
     temperature sensors (spd5118 hwmon) if present.
  2. Run stressapptest over most of the free memory for a fixed time while
     sampling DIMM temperatures.
  3. Scan the kernel log of the test window for machine-check / hardware errors.
  4. Judge, compare with earlier runs, write console summary, HTML and JSON.

Fans and CoolerControl are left alone: the verdict is about errors, not heat.
memtest86+ from the boot menu remains the more thorough test after BIOS/EXPO
changes; this one is for the regular check-up.

Needs root (dmidecode, kernel log):
    sudo ./ram_health.py              # 30 min
    sudo ./ram_health.py --minutes 10
Dependencies: stressapptest (AUR), optional: dmidecode.
"""

from __future__ import annotations

import argparse
import html
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import health_common as hc
from health_common import CSS, FAIL, INFO, OK, SEVERITY, WARN, check, die, fmt, fmt_mmss, warn

FILE_PREFIX = "ram"
RESERVE_MB = 4096                 # memory left for the system while testing
TEST_SHARE = 0.75                 # fixed share of installed RAM -> same size every run
SAMPLE_INTERVAL_S = 5.0
DIMM_TEMP_WARN = 85               # DDR5 starts throttling refresh around here
BANDWIDTH_TREND_WARN = -0.10      # -10 % copy bandwidth vs. baseline
KERNEL_ERROR_PATTERN = re.compile(r"mce:|Hardware Error|EDAC|Machine check", re.I)


# --------------------------------------------------------------------------- #
# System information
# --------------------------------------------------------------------------- #

def meminfo_mb(field: str) -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1]) // 1024
    die(f"cannot read {field} from /proc/meminfo")


def dimm_info() -> list[dict]:
    """Per-DIMM size and speeds from dmidecode (empty list if unavailable)."""
    if not shutil.which("dmidecode"):
        warn("dmidecode not found - EXPO check skipped (pacman -S dmidecode)")
        return []
    out = subprocess.run(["dmidecode", "-t", "memory"], capture_output=True, text=True).stdout
    dimms = []
    for block in out.split("\n\n"):
        if "Memory Device" not in block:
            continue
        fields = dict(re.findall(r"^\s*([^:\n]+):\s*(.+)$", block, re.M))
        if fields.get("Size", "No Module").startswith("No Module"):
            continue
        # some BIOSes repeat the same Locator for every slot; Bank Locator tells them apart
        locator = " / ".join(f for f in (fields.get("Bank Locator"), fields.get("Locator")) if f)
        dimms.append({"locator": locator, "size": fields.get("Size"),
                      "part": (fields.get("Part Number") or "").strip(),
                      "rated": fields.get("Speed"),
                      "configured": fields.get("Configured Memory Speed")})
    return dimms


class DimmTemps:
    """DDR5 SPD hub temperature sensors (kernel driver spd5118)."""

    def __init__(self) -> None:
        self.inputs: dict[str, Path] = {}
        for d in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                if (d / "name").read_text().strip() != "spd5118":
                    continue
            except OSError:
                continue
            sensor = d / "temp1_input"
            if sensor.exists():
                # the i2c address identifies the slot (0x50.. = DIMM 1..)
                label = os.path.basename(os.path.realpath(d / "device"))
                self.inputs[label] = sensor
        if not self.inputs:
            warn("no spd5118 DIMM temperature sensors found - temperatures skipped")

    def read(self) -> dict[str, float]:
        out = {}
        for label, path in self.inputs.items():
            try:
                out[label] = int(path.read_text()) / 1000
            except (OSError, ValueError):
                pass
        return out


def kernel_errors(since: datetime) -> list[str]:
    res = subprocess.run(["journalctl", "-k", "--no-pager", "-o", "short-iso",
                          "--since", since.strftime("%Y-%m-%d %H:%M:%S")],
                         capture_output=True, text=True)
    return [line for line in res.stdout.splitlines() if KERNEL_ERROR_PATTERN.search(line)]


# --------------------------------------------------------------------------- #
# stressapptest
# --------------------------------------------------------------------------- #

def parse_sat(text: str) -> dict:
    status = re.search(r"Status:\s*(PASS|FAIL)[^\n]*", text)
    incidents = re.search(r"Stats: Found (\d+) hardware incidents", text)
    completed = re.search(r"Stats: Completed: ([\d.]+)M in ([\d.]+)s ([\d.]+)MB/s", text)
    return {"status": status.group(1) if status else None,
            "status_line": status.group(0).strip() if status else None,
            "incidents": int(incidents.group(1)) if incidents else None,
            "bandwidth_mbs": float(completed.group(3)) if completed else None}


def run_test(args, log_path: Path, temps: DimmTemps, run: dict) -> None:
    size = run["config"]["test_mb"]
    seconds = int(args.minutes * 60)
    print(f"\n> stressapptest: {size} MB for {args.minutes} min")
    with open(log_path, "w") as log:
        proc = subprocess.Popen(["stressapptest", "-M", str(size), "-s", str(seconds), "-W"],
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                elapsed = time.monotonic() - start
                t = temps.read()
                run["samples"].append({"t": round(elapsed, 1), "dimm_c": t})
                if sys.stdout.isatty():
                    shown = " / ".join(f"{v:.1f}" for v in t.values()) or "n/a"
                    print(f"\r[{fmt_mmss(elapsed)}/{fmt_mmss(seconds)}]  DIMMs {shown} C"
                          .ljust(70), end="", flush=True)
                if elapsed > seconds + 300:
                    raise RuntimeError("stressapptest did not finish in time")
                time.sleep(SAMPLE_INTERVAL_S)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=30)
        print()
    run["sat"] = parse_sat(log_path.read_text(errors="replace"))
    run["sat"]["exit_code"] = proc.returncode


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def analyze(run: dict, history: list[dict]) -> None:
    checks, m = [], {}
    sat = run.get("sat") or {}

    if run["status"] != "completed":
        checks.append(check("Test completed", run["error"] or run["status"], "completed", WARN,
                            "No verdict on the memory"))
    if sat:
        bad = sat.get("status") != "PASS" or (sat.get("incidents") or 0) > 0
        checks.append(check("Memory errors (stressapptest)",
                            f"{fmt(sat.get('incidents'), '.0f')} incidents, "
                            f"{sat.get('status') or 'no result'}",
                            "0 incidents, PASS", FAIL if bad else OK,
                            "Run memtest86+ and check EXPO/voltage settings" if bad else ""))
    kerr = run.get("kernel_errors", [])
    checks.append(check("Kernel hardware errors during test", f"{len(kerr)} messages", "0",
                        FAIL if kerr else OK,
                        "Machine-check or EDAC messages in the kernel log" if kerr else ""))

    dimms = run.get("dimms", [])
    if dimms:
        configured = sorted({d["configured"] for d in dimms if d.get("configured")})
        m["configured_speed"] = configured[0] if len(configured) == 1 else ", ".join(configured)
        prev = next((h for h in reversed(history) if h.get("metrics", {}).get("configured_speed")),
                    None)
        before = prev["metrics"]["configured_speed"] if prev else None
        changed = before is not None and before != m["configured_speed"]
        checks.append(check("Configured memory speed", m["configured_speed"],
                            f"same as last run ({before})" if before else "first run - baseline",
                            WARN if changed else OK if before else INFO,
                            "Speed changed - BIOS update reset EXPO?" if changed else ""))

    temps = [s["dimm_c"] for s in run.get("samples", []) if s["dimm_c"]]
    if temps:
        peaks = {label: max(t.get(label, 0) for t in temps) for label in temps[0]}
        m["dimm_peak_c"] = peaks
        hottest = max(peaks.values())
        checks.append(check("Peak DIMM temperature",
                            " / ".join(f"{v:.1f} C" for v in peaks.values()),
                            f"< {DIMM_TEMP_WARN} C", WARN if hottest >= DIMM_TEMP_WARN else OK,
                            "Hot DIMMs get less stable - check airflow over the RAM"
                            if hottest >= DIMM_TEMP_WARN else ""))

    if sat.get("bandwidth_mbs"):
        m["bandwidth_mbs"] = sat["bandwidth_mbs"]
        same = [h for h in history if h.get("status") == "completed"
                and h.get("fingerprint") == run["fingerprint"]
                and h.get("metrics", {}).get("bandwidth_mbs")]
        if same:
            base = same[0]["metrics"]["bandwidth_mbs"]
            change = sat["bandwidth_mbs"] / base - 1
            checks.append(check("Trend: copy bandwidth", f"{change * 100:+.0f} %  "
                                f"({base:.0f} -> {sat['bandwidth_mbs']:.0f} MB/s)",
                                f"> {BANDWIDTH_TREND_WARN * 100:.0f} %",
                                WARN if change < BANDWIDTH_TREND_WARN else OK,
                                f"Baseline from {same[0]['started'][:10]}"))
        else:
            checks.append(check("Copy bandwidth", f"{sat['bandwidth_mbs']:.0f} MB/s",
                                "baseline for later runs", INFO))

    worst = max((SEVERITY[c["status"]] for c in checks), default=0)
    run["metrics"], run["checks"] = m, checks
    run["verdict"] = {0: OK, 1: WARN, 2: FAIL}[worst]


def verdict_text(run: dict) -> str:
    if run["verdict"] == FAIL:
        return "Memory errors - check before trusting this system with important work"
    if run["verdict"] == WARN:
        return "Watch it - some values are off"
    return "Healthy - no memory errors"


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def print_report(run: dict) -> None:
    print("\n" + "=" * 78)
    print(f" RAM health check  ({run['started'][:16].replace('T', ' ')})")
    print("=" * 78)
    hc.print_checks(run["checks"])
    print("-" * 78)
    print(" Verdict: " + hc.color(verdict_text(run), run["verdict"]))
    print("=" * 78)


def render_html(run: dict) -> str:
    esc = html.escape
    samples = run.get("samples", [])
    labels = list(samples[0]["dimm_c"]) if samples and samples[0]["dimm_c"] else []
    classes = ("coolant", "heat", "power", "pump")
    series = [{"name": lab, "cls": classes[i % 4],
               "points": [(s["t"] / 60, s["dimm_c"].get(lab)) for s in samples]}
              for i, lab in enumerate(labels)]
    chart = (hc.svg_chart(series, y_unit="°C") if series
             else '<p class="empty">No DIMM temperature sensors available.</p>')
    dimms = "".join(f"<li>{esc(str(d['locator']))}: {esc(str(d['size']))}, "
                    f"{esc(d['part'])}, rated {esc(str(d['rated']))}, "
                    f"configured {esc(str(d['configured']))}</li>" for d in run.get("dimms", []))
    cfg = run["config"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAM health check {esc(run['started'][:10])}</title><style>{CSS}</style></head>
<body><main>
<header>{hc.badge(run)}<div><h1>RAM health check</h1>
<p class="sub">{esc(run['host'])}, {esc(run['started'][:16].replace('T', ' '))}</p>
<p class="verdict v-{run['verdict'].lower()}-text">{esc(verdict_text(run))}</p></div></header>
<h2>Checks</h2>
{hc.checks_table(run["checks"])}
<h2>DIMM temperatures</h2>
<p class="sub">One line per module, labelled by its SPD bus address.</p>
<div class="scroll">{chart}</div>
<h2>Test conditions</h2>
<dl><dt>Tested memory</dt><dd>{cfg['test_mb']} MB for {cfg['minutes']} min</dd>
<dt>Modules</dt><dd><ul>{dimms or '<li>dmidecode not available</li>'}</ul></dd>
<dt>Result line</dt><dd>{esc(str((run.get('sat') or {}).get('status_line')))}</dd></dl>
</main></body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description="RAM health check with stressapptest (needs root).")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    args = p.parse_args()
    if not shutil.which("stressapptest"):
        die("stressapptest not found - install it with: paru -S stressapptest")
    hc.ensure_root()
    hc.install_signal_handlers()

    out_dir, uid, gid = hc.prepare_results_dir(args.out_dir)
    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d-%H%M%S")

    # A fixed test size keeps runs comparable; free memory varies with the session.
    total, available = meminfo_mb("MemTotal"), meminfo_mb("MemAvailable")
    target_mb = int(total * TEST_SHARE) // 4096 * 4096             # whole 4 GB steps
    test_mb = min(target_mb, (available - RESERVE_MB) // 1024 * 1024)
    if test_mb < 2048:
        die(f"only {available} MB free - not enough for a meaningful test")
    if test_mb < target_mb:
        warn(f"only {available} MB free - testing {test_mb} MB instead of {target_mb} MB; "
             "close applications for a run comparable with earlier ones")
    temps = DimmTemps()
    run = {"started": started.isoformat(timespec="seconds"), "host": platform.node(),
           "status": "completed", "error": None, "dimms": dimm_info(), "samples": [],
           "config": {"test_mb": test_mb, "minutes": args.minutes}}
    # bandwidth is only comparable for the same amount of memory, time and modules
    run["fingerprint"] = {"test_gb": test_mb // 1024, "minutes": args.minutes,
                          "parts": sorted(d["part"] for d in run["dimms"])}
    print(f"> Preflight\n  {available} MB available, testing {test_mb} MB  |  "
          f"DIMMs: {len(run['dimms']) or 'n/a'}  |  temp sensors: {len(temps.inputs)}")
    for d in run["dimms"]:
        print(f"  {d['locator']}: {d['size']} {d['part']} - configured {d['configured']}")

    log_path = out_dir / f"{FILE_PREFIX}-stress-{stamp}.log"
    try:
        run_test(args, log_path, temps, run)
    except KeyboardInterrupt:
        run["status"], run["error"] = "interrupted", "Interrupted by user"
    except RuntimeError as exc:
        run["status"], run["error"] = "failed", str(exc)
    run["kernel_errors"] = kernel_errors(started)

    analyze(run, hc.load_history(out_dir, FILE_PREFIX))
    print_report(run)

    report = hc.save_results(out_dir, FILE_PREFIX, stamp, run, render_html(run), uid, gid,
                             extra=(log_path,))
    if not args.no_open:
        hc.open_report(report)
    else:
        print(f" Report: {report}")

if __name__ == "__main__":
    main()
