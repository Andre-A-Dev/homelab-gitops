#!/usr/bin/env python3
"""
health_common.py - building blocks shared by the hardware health checks.

Used by aio_health.py, gpu_health.py, ram_health.py, disk_health.py and
health_all.py: verdict constants, helpers, root/sudo handling, results
directory, report styling and charts, CoolerControl pause and NZXT device
control. Not meant to be run on its own.
"""

from __future__ import annotations

import html
import json
import math
import os
import pwd
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

COOLERCONTROL_SERVICES = ("coolercontrold", "coolercontrol-liqctld")

# Shared results directory for all hardware health checks (relative to the sudo
# user's home). Every check prefixes its files: aio-*, gpu-*, ...
RESULTS_DIR = ".local/share/hardware-health"

OK, WARN, FAIL, INFO, INVALID = "OK", "WARN", "FAIL", "INFO", "INVALID"
SEVERITY = {INFO: 0, OK: 0, WARN: 1, FAIL: 2}


class SafetyAbort(RuntimeError):
    """Raised when a safety limit is hit during the test."""


class InvalidLoad(RuntimeError):
    """Raised when the load generator does not actually load the CPU."""


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def find_hwmon(name: str) -> Path | None:
    """Return the hwmon directory whose 'name' attribute matches."""
    for d in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (d / "name").read_text().strip() == name:
                return d
        except OSError:
            continue
    return None


def fmt(value, spec: str, none: str = "n/a") -> str:
    return none if value is None else format(value, spec)


def fmt_mmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def median_of(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def slope_per_min(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of (seconds, value) points in units per minute."""
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 5:
        return None
    mt = statistics.fmean(t for t, _ in pts)
    mv = statistics.fmean(v for _, v in pts)
    den = sum((t - mt) ** 2 for t, _ in pts)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in pts) / den * 60.0


def tail(samples: list, phase: str, seconds: float) -> list:
    """Samples of one phase within its last `seconds` (any object with .t and .phase)."""
    phase_samples = [s for s in samples if s.phase == phase]
    if not phase_samples:
        return []
    end = phase_samples[-1].t
    return [s for s in phase_samples if s.t >= end - seconds]


def check(name: str, measured: str, expected: str, status: str, note: str = "") -> dict:
    return {"name": name, "measured": measured, "expected": expected,
            "status": status, "note": note}


def grade(value: float, warn_at: float, fail_at: float) -> str:
    return FAIL if value > fail_at else WARN if value > warn_at else OK


# --------------------------------------------------------------------------- #
# Root, results directory, history
# --------------------------------------------------------------------------- #

def ensure_root() -> None:
    if os.geteuid() != 0:
        print("Root privileges needed - re-running with sudo ...")
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        raise SystemExit(128 + signum)          # unwinds through all finally blocks
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


def target_user() -> tuple[str, int, int, Path]:
    name = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    entry = pwd.getpwnam(name)
    return name, entry.pw_uid, entry.pw_gid, Path(entry.pw_dir)


def prepare_results_dir(out_dir: Path | None) -> tuple[Path, int, int]:
    """Create the results directory; files belong to its owner (also under a timer)."""
    path = out_dir or target_user()[3] / RESULTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    st = path.stat()
    if st.st_uid == 0:                         # freshly created by root via sudo
        _, uid, gid, _ = target_user()
        chown(path, uid, gid)
        return path, uid, gid
    return path, st.st_uid, st.st_gid


def save_results(out_dir: Path, prefix: str, stamp: str, run: dict, report_html: str,
                 uid: int, gid: int, extra: tuple[Path, ...] = ()) -> Path:
    """Write <prefix>-run-<stamp>.json and <prefix>-report-<stamp>.html; return the report."""
    json_path = out_dir / f"{prefix}-run-{stamp}.json"
    html_path = out_dir / f"{prefix}-report-{stamp}.html"
    json_path.write_text(json.dumps(run, indent=1))
    html_path.write_text(report_html)
    for path in (json_path, html_path, *extra):
        if path.exists():
            chown(path, uid, gid)
    return html_path


def open_report(path: Path) -> None:
    """Open a report in the desktop session of the user who started the check.

    Runs as root via sudo, so the browser has to be started as SUDO_USER with
    that user's session variables - never as root. Skipped without a terminal
    (systemd timer) or without a graphical session.
    """
    print(f" Report: {path}")
    opener = shutil.which("xdg-open")
    if not sys.stdout.isatty() or not opener:
        return
    cmd = [opener, str(path)]
    if os.geteuid() == 0:
        user = os.environ.get("SUDO_USER")
        if not user:
            return
        entry = pwd.getpwnam(user)
        runtime = Path(f"/run/user/{entry.pw_uid}")
        sockets = sorted((p for p in runtime.glob("wayland-*") if not p.name.endswith(".lock")),
                         key=lambda p: p.stat().st_mtime)
        wayland = os.environ.get("WAYLAND_DISPLAY") or (sockets[-1].name if sockets else None)
        display = os.environ.get("DISPLAY") or (":0" if Path("/tmp/.X11-unix/X0").exists()
                                                else None)
        if not (wayland or display):
            print(" (no graphical session found - open the report manually)")
            return
        env = {"HOME": entry.pw_dir, "USER": user, "PATH": "/usr/local/bin:/usr/bin:/bin",
               "XDG_RUNTIME_DIR": str(runtime),
               "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
               "WAYLAND_DISPLAY": wayland, "DISPLAY": display}
        cmd = ["runuser", "-u", user, "--", "env",
               *[f"{k}={v}" for k, v in env.items() if v], *cmd]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def chown(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def load_history(out_dir: Path, prefix: str) -> list[dict]:
    """All earlier runs of one check (files <prefix>-run-*.json), oldest first."""
    runs = []
    for f in sorted(out_dir.glob(f"{prefix}-run-*.json")):
        try:
            runs.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            warn(f"skipping unreadable history file {f.name}: {exc}")
    return runs


# --------------------------------------------------------------------------- #
# CoolerControl and NZXT devices
# --------------------------------------------------------------------------- #

def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def is_active(service: str) -> bool:
    return systemctl("is-active", "--quiet", service).returncode == 0


class CoolerControlPause:
    """Stop CoolerControl for the duration of the test, restart it afterwards."""

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.restart_ok: dict[str, bool] = {}

    def __enter__(self) -> "CoolerControlPause":
        for svc in COOLERCONTROL_SERVICES:
            if is_active(svc):
                res = systemctl("stop", svc)
                if res.returncode != 0 or is_active(svc):
                    self.__exit__(None, None, None)
                    die(f"could not stop {svc}: {res.stderr.strip()}")
                self.stopped.append(svc)
                print(f"  stopped {svc}")
        if not self.stopped:
            warn("CoolerControl is not running - nothing will restore your "
                 "profiles afterwards; the pump is left at 100 %")
        time.sleep(2)                       # give the daemon time to release HID handles
        return self

    def __exit__(self, *_exc) -> None:
        for svc in reversed(self.stopped):
            systemctl("start", svc)
            time.sleep(1)
            self.restart_ok[svc] = is_active(svc)
            state = "restarted" if self.restart_ok[svc] else "FAILED TO RESTART"
            print(f"  {svc}: {state}")
            if not self.restart_ok[svc]:
                warn(f"start it manually: sudo systemctl start {svc}")


class NzxtCooling:
    """Kraken X pump control plus all NZXT fan controllers."""

    def __init__(self) -> None:
        try:
            import liquidctl
            from liquidctl import find_liquidctl_devices
            from liquidctl.driver.kraken3 import KrakenX3
        except ImportError:
            die("liquidctl Python module not found - install it with: pacman -S liquidctl")

        self.liquidctl_version = getattr(liquidctl, "__version__", "unknown")
        self.kraken = None
        self.fan_devices: list[tuple[str, object]] = []
        seen: dict[str, int] = {}

        for dev in find_liquidctl_devices(vendor=0x1E71):
            try:
                dev.connect()
            except Exception as exc:  # noqa: BLE001 - report and keep going
                warn(f"cannot open {dev.description}: {exc}")
                continue
            if isinstance(dev, KrakenX3) and self.kraken is None:
                self.kraken = dev
                continue
            fans = [k for k, _, _ in self._safe_status(dev)
                    if k.startswith("Fan ") and k.endswith(" speed")]
            if fans:
                seen[dev.description] = seen.get(dev.description, 0) + 1
                self.fan_devices.append((f"{dev.description} #{seen[dev.description]}", dev))
            else:
                dev.disconnect()

        if self.kraken is None:
            self.close()
            die("no NZXT Kraken X found - check 'liquidctl list'")
        self.kraken_name = self.kraken.description
        self.kraken_hwmon = getattr(getattr(self.kraken, "_hwmon", None), "driver", None)

    @staticmethod
    def _safe_status(dev) -> list:
        try:
            return dev.get_status()
        except Exception as exc:  # noqa: BLE001
            warn(f"status read failed for {dev.description}: {exc}")
            return []

    def status(self) -> tuple[float | None, float | None, float | None]:
        values = {k: v for k, v, _ in self.kraken.get_status()}
        return (values.get("Liquid temperature"),
                values.get("Pump speed"),
                values.get("Pump duty"))

    def set_pump(self, duty: int) -> None:
        self.kraken.set_fixed_speed("pump", duty)

    def _fan_status(self, dev) -> dict[int, dict]:
        """{channel: {'speed': rpm, 'duty': %, 'mode': str}} from one fan controller."""
        out: dict[int, dict] = {}
        for key, value, _ in self._safe_status(dev):
            parts = key.split()
            if len(parts) >= 3 and parts[0] == "Fan" and parts[1].isdigit():
                field = {"speed": "speed", "duty": "duty", "control mode": "mode"}.get(
                    " ".join(parts[2:]))
                if field:
                    out.setdefault(int(parts[1]), {})[field] = value
        return out

    def set_fans(self, duty: int) -> list[str]:
        """Pin every fan channel to `duty`, one channel at a time, and verify it.

        Writing all channels via 'sync' in one burst has been seen to leave a
        channel at its previous duty, so each channel is written separately and
        read back. Returns a list of problems (empty when everything took).
        """
        problems = []
        for name, dev in self.fan_devices:
            channels = sorted(self._fan_status(dev))
            for attempt in range(2):
                for ch in channels:
                    try:
                        dev.set_fixed_speed(f"fan{ch}", duty)
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"{name} fan{ch}: write failed ({exc})")
                    time.sleep(0.3)
                time.sleep(3)
                off = [ch for ch, st in self._fan_status(dev).items()
                       if st.get("mode") not in (None, "N/A")
                       and st.get("duty") is not None and abs(st["duty"] - duty) > 3]
                if not off:
                    break
                if attempt == 0:
                    warn(f"{name}: fan{off} did not take {duty} % - retrying")
            else:
                problems += [f"{name} fan{ch}: duty stuck at "
                             f"{self._fan_status(dev)[ch].get('duty')} %" for ch in off]
        return problems

    def fan_rpms(self) -> dict[str, list[int]]:
        out = {}
        for name, dev in self.fan_devices:
            out[name] = [int(v) for k, v, _ in self._safe_status(dev)
                         if k.startswith("Fan ") and k.endswith(" speed")]
        return out

    def fan_details(self) -> dict[str, dict]:
        """Per-channel control mode, voltage and current where the driver reports them."""
        out = {}
        for name, dev in self.fan_devices:
            for key, value, unit in self._safe_status(dev):
                parts = key.split()             # e.g. "Fan 3 control mode"
                if len(parts) >= 3 and parts[0] == "Fan" and parts[1].isdigit():
                    field = " ".join(parts[2:])
                    if field in ("control mode", "duty", "voltage", "current"):
                        entry = out.setdefault(f"{name} fan{parts[1]}", {})
                        entry[field] = f"{value} {unit}".strip()
        return out

    def close(self) -> None:
        for dev in [self.kraken] + [d for _, d in self.fan_devices]:
            if dev is not None:
                try:
                    dev.disconnect()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------- #
# Console and HTML output
# --------------------------------------------------------------------------- #

def color(text: str, status: str) -> str:
    if not sys.stdout.isatty():
        return text
    code = {OK: "32", WARN: "33", FAIL: "31", INFO: "36", INVALID: "35"}.get(status, "0")
    return f"\033[{code}m{text}\033[0m"


def print_checks(checks: list[dict]) -> None:
    """Console table of checks; explanatory notes only where something is off."""
    width = max((len(c["name"]) for c in checks), default=20)
    for c in checks:
        print(f" {color(c['status'].ljust(4), c['status'])}  {c['name'].ljust(width)}  "
              f"{c['measured']}")
        if c["note"] and c["status"] in (WARN, FAIL):
            print(f"       {' ' * width}  -> {c['note']}")


def checks_table(checks: list[dict], label=lambda c: c["name"]) -> str:
    """HTML table of checks."""
    esc = html.escape
    rows = "".join(
        f'<tr><td class="status s-{c["status"]}">{c["status"]}</td><td>{esc(label(c))}'
        + (f'<span class="note">{esc(c["note"])}</span>' if c["note"] else "")
        + f'</td><td>{esc(c["measured"])}</td><td>{esc(c["expected"])}</td></tr>'
        for c in checks)
    return ('<div class="scroll"><table><thead><tr><th>Result</th><th>Check</th>'
            f'<th>Measured</th><th>Expected</th></tr></thead><tbody>{rows}</tbody></table></div>')


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        ticks.append(round(v, 6))
        v += step
    return ticks


def svg_chart(series: list[dict], *, y_unit: str, bands=(), x_labels=None,
              x_unit: str = "min", ref_lines=(), height: int = 230) -> str:
    """Minimal line chart. series: [{'name', 'cls', 'points': [(x, y), ...]}]."""
    width, ml, mr, mt, mb = 760, 52, 16, 24, 30
    xs = [x for s in series for x, y in s["points"] if y is not None]
    ys = [y for s in series for x, y in s["points"] if y is not None]
    ys += [v * 1.06 for v, _ in ref_lines]            # headroom for the label
    if not xs:
        return '<p class="empty">No data recorded for this chart.</p>'
    x0, x1 = min(xs), max(xs)
    if x1 == x0:
        x1 = x0 + 1
    yt = nice_ticks(min(ys), max(ys))
    y0, y1 = yt[0], yt[-1]

    def sx(x): return ml + (x - x0) / (x1 - x0) * (width - ml - mr)
    def sy(y): return mt + (1 - (y - y0) / (y1 - y0)) * (height - mt - mb)

    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for i, (b0, b1, label) in enumerate(bands):
        x_a, x_b = sx(b0), sx(b1)
        if label == "load":                      # shade the phase that matters most
            out.append(f'<rect class="band" fill="none" x="{x_a:.1f}" y="{mt}" '
                       f'width="{max(x_b - x_a, 0):.1f}" height="{height - mt - mb}"/>')
        if i > 0:
            out.append(f'<line class="divider" x1="{x_a:.1f}" x2="{x_a:.1f}" y1="{mt}" '
                       f'y2="{height - mb}"/>')
        if x_b - x_a >= 44:                      # skip labels that would collide
            out.append(f'<text class="band-label" x="{x_a + 4:.1f}" y="{mt - 8}">'
                       f'{html.escape(label)}</text>')
    for t in yt:
        out.append(f'<line class="grid" x1="{ml}" x2="{width - mr}" y1="{sy(t):.1f}" '
                   f'y2="{sy(t):.1f}"/>')
        out.append(f'<text class="tick" x="{ml - 6}" y="{sy(t) + 4:.1f}" '
                   f'text-anchor="end">{t:g}</text>')
    out.append(f'<text class="tick" x="{ml - 6}" y="12" text-anchor="end">'
               f'{html.escape(y_unit)}</text>')
    for v, label in ref_lines:
        out.append(f'<line class="ref" x1="{ml}" x2="{width - mr}" y1="{sy(v):.1f}" '
                   f'y2="{sy(v):.1f}"/>')
        out.append(f'<text class="ref-label" x="{width - mr - 4}" y="{sy(v) - 5:.1f}" '
                   f'text-anchor="end">{html.escape(label)}</text>')
    if x_labels:
        for i, (x, label) in enumerate(x_labels):
            anchor = ("start" if i == 0 else "end" if i == len(x_labels) - 1
                      else "middle")
            out.append(f'<text class="tick" x="{sx(x):.1f}" y="{height - 10}" '
                       f'text-anchor="{anchor}">{html.escape(label)}</text>')
    else:
        for t in nice_ticks(x0, x1, 8):
            if x0 <= t <= x1:
                out.append(f'<text class="tick" x="{sx(t):.1f}" y="{height - 10}" '
                           f'text-anchor="middle">{t:g}</text>')
        out.append(f'<text class="tick" x="{width - mr}" y="{height - 10}" '
                   f'text-anchor="end">{x_unit}</text>')
    for s in series:
        d, pen = [], "M"
        for x, y in s["points"]:
            if y is None:
                pen = "M"
                continue
            d.append(f"{pen}{sx(x):.1f},{sy(y):.1f}")
            pen = "L"
        out.append(f'<path class="line {s["cls"]}" fill="none" d="{" ".join(d)}"/>')
        if s.get("dots"):
            for x, y in s["points"]:
                if y is not None:
                    out.append(f'<circle class="dot {s["cls"]}" cx="{sx(x):.1f}" '
                               f'cy="{sy(y):.1f}" r="4"/>')
    out.append("</svg>")
    return "".join(out)


def badge(run: dict) -> str:
    """Round inspection-sticker style badge: month ring, year, verdict colour."""
    started = datetime.fromisoformat(run["started"])
    cx = cy = 80
    ring = []
    for i in range(12):
        a0 = math.radians(i * 30 - 90 - 15)
        a1 = math.radians(i * 30 - 90 + 15)
        r_out, r_in = 76, 58
        pts = [(cx + r_out * math.cos(a0), cy + r_out * math.sin(a0)),
               (cx + r_out * math.cos(a1), cy + r_out * math.sin(a1)),
               (cx + r_in * math.cos(a1), cy + r_in * math.sin(a1)),
               (cx + r_in * math.cos(a0), cy + r_in * math.sin(a0))]
        month = (started.month - 1 + i) % 12 + 1     # test month sits at 12 o'clock
        cls = "seg now" if i == 0 else "seg"
        tx = cx + 67 * math.cos(math.radians(i * 30 - 90))
        ty = cy + 67 * math.sin(math.radians(i * 30 - 90)) + 4
        ring.append(f'<path class="{cls}" d="M{pts[0][0]:.1f},{pts[0][1]:.1f} '
                    f'A{r_out},{r_out} 0 0 1 {pts[1][0]:.1f},{pts[1][1]:.1f} '
                    f'L{pts[2][0]:.1f},{pts[2][1]:.1f} '
                    f'A{r_in},{r_in} 0 0 0 {pts[3][0]:.1f},{pts[3][1]:.1f} Z"/>'
                    f'<text class="seg-num" x="{tx:.1f}" y="{ty:.1f}" '
                    f'text-anchor="middle">{month}</text>')
    return (f'<svg class="badge v-{run["verdict"].lower()}" viewBox="0 0 160 160" '
            f'role="img" aria-label="Verdict {run["verdict"]}">'
            f'<circle class="disc" cx="{cx}" cy="{cy}" r="78"/>{"".join(ring)}'
            f'<text class="year" x="{cx}" y="{cy + 14}" text-anchor="middle">'
            f'{started:%y}</text></svg>')


CSS = """
:root{--paper:#f3f6f7;--ink:#17262e;--muted:#5d6f78;--line:#d3dde1;--panel:#ffffff;
--coolant:#1c7c86;--heat:#c9711f;--power:#6a5acd;--pump:#2f6db5;--ok:#2e7d4f;
--warn:#b7861a;--fail:#b33a3a;--band:rgba(28,124,134,.07)}
@media (prefers-color-scheme:dark){:root{--paper:#121a1e;--ink:#dce6ea;--muted:#8fa2ab;
--line:#2a3940;--panel:#182328;--coolant:#4fb8c2;--heat:#e8994f;--power:#a297f0;
--pump:#73a6e8;--ok:#5cbf83;--warn:#e0b44a;--fail:#e06a6a;--band:rgba(79,184,194,.08)}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.5 "Noto Sans","Cantarell",system-ui,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:60rem;margin:0 auto;padding:2rem 1.25rem 4rem}
header{display:grid;grid-template-columns:auto 1fr;gap:1.75rem;align-items:center;
padding-bottom:1.5rem;border-bottom:2px solid var(--ink)}
h1{font-size:2rem;line-height:1.15;margin:0 0 .35rem;font-weight:700;letter-spacing:-.01em}
h2{font-size:1.15rem;margin:2.5rem 0 .75rem}
.sub{color:var(--muted);margin:0}
.verdict{font-size:1.35rem;font-weight:600;margin:.6rem 0 0}
.v-ok-text{color:var(--ok)}.v-warn-text{color:var(--warn)}.v-fail-text{color:var(--fail)}
.v-invalid-text{color:var(--muted)}
.badge{width:150px;height:150px}
.badge .disc{fill:var(--panel);stroke:var(--line);stroke-width:2}
.badge .seg{fill:var(--paper);stroke:var(--panel);stroke-width:1.5}
.badge .seg-num{font-size:9px;fill:var(--muted);font-weight:600}
.badge .year{font-size:44px;font-weight:700;fill:var(--ink)}
.badge.v-ok .seg.now{fill:var(--ok)}.badge.v-warn .seg.now{fill:var(--warn)}
.badge.v-fail .seg.now{fill:var(--fail)}.badge.v-invalid .seg.now{fill:var(--muted)}
.badge .seg.now + .seg-num{fill:#fff}
.alert{border-left:4px solid var(--fail);padding:.6rem 1rem;background:var(--panel);margin-top:1.5rem}
table{width:100%;border-collapse:collapse;background:var(--panel)}
th,td{text-align:left;padding:.55rem .75rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.85rem;color:var(--muted);font-weight:600}
td.status{font-weight:700;width:4.5rem}
.s-OK{color:var(--ok)}.s-WARN{color:var(--warn)}.s-FAIL{color:var(--fail)}.s-INFO{color:var(--muted)}
.note{display:block;color:var(--muted);font-size:.875rem}
.scroll{overflow-x:auto}
.chart{width:100%;height:auto;display:block;background:var(--panel)}
.grid{stroke:var(--line);stroke-width:1}
.tick{font-size:11px;fill:var(--muted)}
.band{fill:var(--band)}.band-label{font-size:11px;fill:var(--muted)}
.divider{stroke:var(--line);stroke-dasharray:2 3}
.ref{stroke:var(--fail);stroke-dasharray:5 4}.ref-label{font-size:11px;fill:var(--fail)}
.line{fill:none;stroke-width:2}
.coolant{stroke:var(--coolant);fill:var(--coolant)}.heat{stroke:var(--heat);fill:var(--heat)}
.power{stroke:var(--power);fill:var(--power)}.pump{stroke:var(--pump);fill:var(--pump)}
path.line{fill:none}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.3rem 1.25rem;margin:0}
dt{color:var(--muted)}dd{margin:0}
.empty{color:var(--muted);font-style:italic}
@media (max-width:640px){header{grid-template-columns:1fr}.badge{width:120px;height:120px}}
"""

