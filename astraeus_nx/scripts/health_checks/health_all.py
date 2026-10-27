#!/usr/bin/env python3
"""
health_all.py - run every hardware health check in one go (~80 min).

Runs disk, AIO/CPU, GPU and RAM checks one after another, each as its own
process so every check restores the system (CoolerControl, fans) by itself.
The individual reports are not opened in between; at the end - or after an
interruption, as soon as at least one result exists - a summary page with
all verdicts and links to the detailed reports is written and opened.

    sudo ./health_all.py
    sudo ./health_all.py --skip gpu --skip ram
Ctrl+C stops the running check cleanly (it saves its partial result) and
skips the remaining ones.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import health_common as hc
from health_common import CSS, FAIL, INVALID, OK, SEVERITY, WARN, die, fmt

HERE = Path(__file__).resolve().parent
FILE_PREFIX = "all"

# name, script, result prefix, rough minutes, external tools it needs
CHECKS = [
    ("disk", "disk_health.py", "disk", 0.2, ["smartctl"]),
    ("aio", "aio_health.py", "aio", 30, ["liquidctl"]),
    ("gpu", "gpu_health.py", "gpu", 17, ["gpu_burn|gpu-burn", "nvidia-smi", "liquidctl"]),
    ("ram", "ram_health.py", "ram", 31, ["stressapptest"]),
]
TITLES = {"disk": "Drives", "aio": "AIO / CPU cooling", "gpu": "GPU", "ram": "Memory"}


# --------------------------------------------------------------------------- #
# Running the checks
# --------------------------------------------------------------------------- #

def preflight(selected: list[tuple]) -> None:
    """Fail before an 80-minute session, not halfway through it."""
    missing = sorted({tool for *_, tools in selected for tool in tools
                      if not any(shutil.which(t) for t in tool.split("|"))})
    if missing:
        die(f"missing tools: {', '.join(missing)} - install them or use --skip")


def run_check(script: str, out_dir: Path) -> tuple[int | None, bool]:
    """Run one check in the foreground; return (exit code, interrupted)."""
    proc = subprocess.Popen([sys.executable, str(HERE / script), "--no-open",
                             "--out-dir", str(out_dir)], cwd=HERE)
    interrupted = False
    while True:
        try:
            return proc.wait(), interrupted
        except KeyboardInterrupt:
            # The check received the same Ctrl+C and is restoring the system -
            # wait for it instead of leaving CoolerControl stopped.
            interrupted = True
            print("\n> Interrupted - waiting for the running check to clean up ...")


def newest_result(out_dir: Path, prefix: str, since: float) -> Path | None:
    files = [f for f in out_dir.glob(f"{prefix}-run-*.json") if f.stat().st_mtime >= since]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def verdict_text(name: str, run: dict) -> str:
    """Reuse each check's own wording for its verdict."""
    module = importlib.import_module(f"{name}_health")
    return module.verdict_text(run["verdict"] if name == "disk" else run)


def key_figures(name: str, run: dict) -> str:
    m = run.get("metrics", {})
    if name == "aio":
        return (f"coolant {fmt(m.get('r_coolant'), '.3f')} K/W, "
                f"pump {fmt(m.get('pump_rpm_100'), '.0f')} rpm")
    if name == "gpu":
        rpm = "/".join(str(r) for r in m.get("gpu_fan_rpm") or []) or "n/a"
        return (f"{fmt(m.get('r_gpu'), '.3f')} K/W, internal "
                f"{fmt(m.get('internal_offset'), '+.0f')} K, fans {rpm} rpm")
    if name == "ram":
        peaks = m.get("dimm_peak_c") or {}
        return (f"{fmt(m.get('bandwidth_mbs'), '.0f')} MB/s, "
                f"DIMMs max {fmt(max(peaks.values()) if peaks else None, '.1f')} C, "
                f"{m.get('configured_speed') or 'n/a'}")
    if name == "disk":
        lives = [d["life_left"] for d in run.get("drives", []) if d.get("life_left") is not None]
        return (f"{len(run.get('drives', []))} drives, lowest life left "
                f"{fmt(min(lives) if lives else None, '.0f')} %")
    return ""


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

def overall(results: list[dict]) -> str:
    verdicts = [r["verdict"] for r in results if r.get("verdict")]
    if not verdicts:
        return INVALID
    worst = max(SEVERITY.get(v, 1) for v in verdicts)     # INVALID counts as WARN
    return {0: OK, 1: WARN, 2: FAIL}[worst]


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 78)
    print(f" Hardware health  ({summary['started'][:16].replace('T', ' ')})"
          + ("  - INTERRUPTED" if summary["interrupted"] else ""))
    print("=" * 78)
    for r in summary["results"]:
        status = r.get("verdict") or "-"
        line = r.get("text") or r.get("note", "")
        print(f" {hc.color(status.ljust(7), status)} {TITLES[r['name']]:<18} {line}")
        if r.get("figures"):
            print(f" {'':7} {'':18} {r['figures']}")
    print("-" * 78)
    print(" Overall: " + hc.color(summary["verdict"], summary["verdict"]))
    print("=" * 78)


def render_html(summary: dict) -> str:
    esc = html.escape
    cards = []
    for r in summary["results"]:
        verdict = r.get("verdict")
        cls = f"s-{verdict}" if verdict else "s-INFO"
        link = (f'<a href="{esc(r["report"])}">detailed report</a>' if r.get("report")
                else "")
        issues = "".join(f"<li><b class='s-{c['status']}'>{c['status']}</b> "
                         f"{esc(c['name'])}: {esc(c['measured'])}</li>"
                         for c in r.get("issues", []))
        cards.append(f"""
<tr><td class="status {cls}">{esc(verdict or '-')}</td>
<td><b>{esc(TITLES[r['name']])}</b><span class="note">{esc(r.get('figures', ''))}</span>
{f'<ul>{issues}</ul>' if issues else ''}</td>
<td>{esc(r.get('text') or r.get('note', ''))}</td><td>{link}</td></tr>""")
    alert = ('<div class="alert"><strong>Session interrupted.</strong> '
             'Checks after the interruption were not run.</div>'
             if summary["interrupted"] else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hardware health {esc(summary['started'][:10])}</title><style>{CSS}
ul{{margin:.4rem 0 0;padding-left:1.1rem;font-size:.9rem}} a{{color:var(--pump)}}</style></head>
<body><main>
<header>{hc.badge(summary)}<div><h1>Hardware health</h1>
<p class="sub">{esc(summary['host'])}, {esc(summary['started'][:16].replace('T', ' '))},
{summary['minutes']:.0f} min</p>
<p class="verdict v-{summary['verdict'].lower()}-text">Overall: {esc(summary['verdict'])}</p>
</div></header>
{alert}
<h2>Checks</h2>
<div class="scroll"><table><thead><tr><th>Result</th><th>Component</th><th>Verdict</th>
<th></th></tr></thead><tbody>{''.join(cards)}</tbody></table></div>
</main></body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description="Run all hardware health checks (needs root).")
    p.add_argument("--skip", action="append", default=[], choices=[c[0] for c in CHECKS],
                   help="leave out a check (repeatable)")
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--no-open", action="store_true", help="do not open the summary report")
    args = p.parse_args()
    selected = [c for c in CHECKS if c[0] not in args.skip]
    if not selected:
        die("nothing to run")
    preflight(selected)
    hc.ensure_root()

    out_dir, uid, gid = hc.prepare_results_dir(args.out_dir)
    started = datetime.now().astimezone()
    t0 = time.monotonic()
    print(f"> Running {', '.join(c[0] for c in selected)} - about "
          f"{sum(c[3] for c in selected):.0f} min. Leave the PC alone meanwhile.")

    results, interrupted = [], False
    for name, script, prefix, _, _ in selected:
        if interrupted:
            results.append({"name": name, "note": "not run (session interrupted)"})
            continue
        print(f"\n{'#' * 78}\n# {TITLES[name]}  ({script})\n{'#' * 78}")
        step_start = time.time()
        try:
            code, interrupted = run_check(script, out_dir)
        except KeyboardInterrupt:                  # Ctrl+C between two checks
            code, interrupted = None, True
        result_file = newest_result(out_dir, prefix, step_start)
        if result_file is None:
            results.append({"name": name, "exit_code": code,
                            "note": "no result written" + (" (interrupted)" if interrupted
                                                           else f" (exit code {code})")})
            continue
        run = json.loads(result_file.read_text())
        results.append({
            "name": name, "exit_code": code, "verdict": run.get("verdict"),
            "status": run.get("status"),
            "text": verdict_text(name, run) + ("" if run.get("status", "completed") == "completed"
                                               else f" (run {run['status']}, partial data)"),
            "figures": key_figures(name, run),
            "issues": [c for c in run.get("checks", []) if c["status"] in (WARN, FAIL)],
            "result": result_file.name,
            "report": result_file.name.replace("-run-", "-report-").replace(".json", ".html"),
        })

    summary = {"started": started.isoformat(timespec="seconds"), "host": platform.node(),
               "minutes": (time.monotonic() - t0) / 60, "interrupted": interrupted,
               "results": results}
    summary["verdict"] = overall(results)
    print_summary(summary)

    if not any(r.get("verdict") for r in results):
        print(" No results to summarise.")
        sys.exit(1)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    report = hc.save_results(out_dir, FILE_PREFIX, stamp, summary, render_html(summary), uid, gid)
    if not args.no_open:
        hc.open_report(report)
    else:
        print(f" Report: {report}")
    sys.exit(130 if interrupted else 2 if summary["verdict"] == FAIL else 0)


if __name__ == "__main__":
    main()
