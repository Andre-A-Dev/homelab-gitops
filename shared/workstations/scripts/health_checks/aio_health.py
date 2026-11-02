#!/usr/bin/env python3
"""
aio_health.py - automated health check for an NZXT Kraken X AIO on Linux.

Test sequence
  1. Stop CoolerControl so nothing else fights over pump/fan control.
  2. Pin every fan channel on all NZXT fan controllers to one fixed duty,
     so airflow is identical between runs.
  3. Pump sweep: step the pump through several duties, check the RPM response.
  4. Idle baseline: pump at 100 %, wait until the coolant temperature settles.
  5. Load: full CPU load for a fixed time; record coolant/CPU temps and power.
  6. Cooldown: record how quickly the coolant recovers.
  7. Restart CoolerControl, print a console summary, write an HTML report and
     store the raw run as JSON so that later runs show a trend.

Needs root (service control, hwmon PWM writes, RAPL energy counter):
    sudo ./aio_health.py            # full run, ~30 min
    sudo ./aio_health.py --quick    # short trial run, ~12 min

Dependencies: Python 3.10+, liquidctl (pacman), optional: stress-ng.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import multiprocessing
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from health_common import (CSS, FAIL, INFO, INVALID, OK, RESULTS_DIR, SEVERITY, WARN,
                           CoolerControlPause, InvalidLoad, NzxtCooling, SafetyAbort, badge,
                           check, checks_table, color, ensure_root, find_hwmon, fmt, fmt_mmss,
                           grade, install_signal_handlers, load_history, mean, median_of,
                           open_report, prepare_results_dir, print_checks, read_text,
                           save_results, slope_per_min, svg_chart, tail, warn)

# --------------------------------------------------------------------------- #
# Constants and heuristics
# --------------------------------------------------------------------------- #


FILE_PREFIX = "aio"

PUMP_SPEC_RPM = 2800        # NZXT spec sheet, Kraken X53/X63/X73 at 100 % duty
PUMP_SPEC_TOL = 300         # +/- tolerance from the same spec sheet
SWEEP_DUTIES = (40, 60, 80, 100)

SAMPLE_INTERVAL_S = 2.0
STEADY_WINDOW_S = 180       # tail of the load phase used as "steady state"
IDLE_WINDOW_S = 60          # tail of the idle phase used as baseline
STABLE_SLOPE_K_PER_MIN = 0.2

# Safety limits - the test aborts the load immediately when hit.
LIQUID_ABORT_C = 55.0       # Kraken X firmware treats ~59 C as critical
PUMP_DEAD_RPM = 300         # pump commanded to 100 % but basically not spinning

# Health heuristics. Absolute limits are rough guesses for a 360 mm AIO;
# the trend against your own first run is the more meaningful signal.
# First valid baseline on Astraeus (2026-10-12): 0.074 K/W with all fans at 60 %.
R_WARN, R_FAIL = 0.09, 0.12             # K/W: coolant rise per extra CPU watt
DT_WARN, DT_FAIL = 12.0, 15.0           # K: coolant rise if power is unknown
PUMP_CV_WARN, PUMP_CV_FAIL = 0.02, 0.05  # pump RPM coefficient of variation
TREND_WARN, TREND_FAIL = 0.15, 0.30     # relative increase vs. baseline run
PEAK_LIQUID_WARN = 50.0
LOAD_CHECK_S = 20           # verify the load generator after this many seconds
LOAD_MIN_UTIL = 90.0        # % CPU utilisation expected under full load
LOAD_MIN_EXTRA_W = 100.0    # below this the coolant rise is too small to measure well
PUMP_DIP_RATIO = 0.85       # a reading below 85 % of the median counts as a dip
FAN_OUTLIER_RATIO = 0.70    # a fan channel below 70 % of the median rpm is suspicious


# --------------------------------------------------------------------------- #
# Sensors
# --------------------------------------------------------------------------- #


class CpuTemps:
    """All labelled k10temp sensors (Tctl, Tccd1, Tccd2, ...)."""

    def __init__(self) -> None:
        self.inputs: dict[str, Path] = {}
        base = find_hwmon("k10temp")
        if base is None:
            warn("k10temp hwmon not found - CPU temperatures will be missing")
            return
        for label_file in sorted(base.glob("temp*_label")):
            input_file = label_file.with_name(label_file.name.replace("_label", "_input"))
            if input_file.exists():
                self.inputs[label_file.read_text().strip()] = input_file

    def read(self) -> dict[str, float]:
        out = {}
        for label, path in self.inputs.items():
            try:
                out[label] = int(path.read_text()) / 1000.0
            except (OSError, ValueError):
                pass
        return out


class CpuPower:
    """CPU package power from the powercap RAPL energy counter (AMD Zen too)."""

    def __init__(self) -> None:
        self.energy_file: Path | None = None
        self.max_range = 0
        self._last: tuple[float, int] | None = None
        for zone in sorted(Path("/sys/class/powercap").glob("*:*")):
            if zone.name.count(":") != 1:          # skip sub-zones like x:0:0
                continue
            try:
                if not (zone / "name").read_text().strip().startswith("package"):
                    continue
                int((zone / "energy_uj").read_text())   # probe readability
                self.energy_file = zone / "energy_uj"
                self.max_range = int((zone / "max_energy_range_uj").read_text())
                break
            except (OSError, ValueError):
                continue
        if self.energy_file is None:
            warn("no readable RAPL package counter - CPU power will be missing; "
                 "the coolant check falls back to plain temperature rise")

    @property
    def available(self) -> bool:
        return self.energy_file is not None

    def read(self) -> float | None:
        """Average package power in W since the previous call."""
        if self.energy_file is None:
            return None
        now = time.monotonic()
        energy = int(self.energy_file.read_text())
        last, self._last = self._last, (now, energy)
        if last is None:
            return None
        dt = now - last[0]
        de = energy - last[1]
        if de < 0:                                   # counter wrapped
            de += self.max_range
        return de / 1e6 / dt if dt > 0 else None


class CpuActivity:
    """System-wide CPU utilisation (/proc/stat) and mean core clock (cpufreq)."""

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None
        self.freq_files = sorted(
            Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))

    def read(self) -> tuple[float | None, float | None]:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields[:8])   # idle+iowait, w/o guest
        last, self._last = self._last, (idle, total)
        util = None
        if last is not None and total > last[1]:
            util = 100.0 * (1 - (idle - last[0]) / (total - last[1]))
        mhz = []
        for path in self.freq_files:
            try:
                mhz.append(int(path.read_text()) / 1000.0)
            except (OSError, ValueError):
                pass
        return util, (statistics.fmean(mhz) if mhz else None)


def system_snapshot() -> dict:
    """CPU settings that change how much heat the load test produces."""
    cpuinfo = read_text("/proc/cpuinfo") or ""
    model = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines()
                  if line.startswith("model name")), None)
    profile = None
    if shutil.which("powerprofilesctl"):
        res = subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            profile = res.stdout.strip()
    base = "/sys/devices/system/cpu"
    return {
        "cpu_model": model,
        "online_cpus": os.cpu_count(),
        "usable_cpus": len(os.sched_getaffinity(0)),
        "amd_pstate": read_text(f"{base}/amd_pstate/status"),
        "governor": read_text(f"{base}/cpu0/cpufreq/scaling_governor"),
        "epp": read_text(f"{base}/cpu0/cpufreq/energy_performance_preference"),
        "boost": read_text(f"{base}/cpufreq/boost"),
        "power_profile": profile,
    }


def warn_about_snapshot(snap: dict) -> None:
    if snap["usable_cpus"] != snap["online_cpus"]:
        warn(f"only {snap['usable_cpus']} of {snap['online_cpus']} CPUs usable (affinity)")
    if snap["power_profile"] == "power-saver" or snap["epp"] == "power":
        warn("power-saving CPU profile active - the load test will produce little heat")
    if snap["boost"] == "0":
        warn("CPU boost is disabled - the load test will produce less heat")


# --------------------------------------------------------------------------- #
# CPU load
# --------------------------------------------------------------------------- #

def _burn() -> None:
    """Busy loop for the Python fallback load generator."""
    x = 0
    while True:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF


class CpuLoad:
    def __init__(self, max_seconds: int, log_path: Path) -> None:
        self.method = "stress-ng" if shutil.which("stress-ng") else "python"
        self.max_seconds = max_seconds
        self.log_path = log_path
        self._log = None
        self._proc: subprocess.Popen | None = None
        self._workers: list[multiprocessing.Process] = []

    def start(self) -> None:
        if self.method == "stress-ng":
            self._log = open(self.log_path, "w")
            # --timeout is a dead-man switch in case this script gets SIGKILLed
            self._proc = subprocess.Popen(
                ["stress-ng", "--cpu", "0", "--cpu-method", "matrixprod",
                 "--timeout", f"{self.max_seconds}s", "--metrics-brief"],
                stdout=self._log, stderr=subprocess.STDOUT,
                start_new_session=True)
        else:
            for _ in range(os.cpu_count() or 1):
                worker = multiprocessing.Process(target=_burn, daemon=True)
                worker.start()
                self._workers.append(worker)

    def exit_code(self) -> int | None:
        """Return code if the load generator already died, else None."""
        if self._proc is not None:
            return self._proc.poll()
        dead = [w.exitcode for w in self._workers if not w.is_alive()]
        return dead[0] if dead else None

    def log_tail(self, lines: int = 5) -> str:
        text = read_text(str(self.log_path)) or ""
        return " | ".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)
        for worker in self._workers:
            worker.terminate()
        for worker in self._workers:
            worker.join(timeout=5)
        if self._log:
            self._log.close()
        self._proc, self._workers, self._log = None, [], None


# --------------------------------------------------------------------------- #
# Ambient temperature
# --------------------------------------------------------------------------- #

def query_prometheus(url: str, query: str) -> float | None:
    full = url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(full, timeout=5) as resp:
            data = json.load(resp)
        result = data["data"]["result"]
        if len(result) != 1:
            warn(f"Prometheus query returned {len(result)} series, expected exactly 1 "
                 "- narrow the label selector")
            return None
        return float(result[0]["value"][1])
    except Exception as exc:  # noqa: BLE001
        warn(f"Prometheus query failed: {exc}")
        return None


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #

@dataclass
class Sample:
    t: float                    # seconds since test start
    phase: str
    pump_target: int
    liquid_c: float | None
    pump_rpm: float | None
    pump_duty: float | None
    power_w: float | None
    cpu_util: float | None = None   # % across all CPUs
    cpu_mhz: float | None = None    # mean core clock
    cpu_c: dict[str, float] = field(default_factory=dict)


class Recorder:
    def __init__(self, cooling: NzxtCooling, temps: CpuTemps, power: CpuPower,
                 activity: CpuActivity) -> None:
        self.cooling, self.temps, self.power, self.activity = cooling, temps, power, activity
        self.samples: list[Sample] = []
        self.t0 = time.monotonic()
        self.pump_target = 100
        self._dead_pump_count = 0
        self._tty = sys.stdout.isatty()
        self._last_print = 0.0

    def now(self) -> float:
        return time.monotonic() - self.t0

    def sample(self, phase: str) -> Sample:
        liquid, rpm, duty = self.cooling.status()
        util, mhz = self.activity.read()
        s = Sample(t=round(self.now(), 1), phase=phase, pump_target=self.pump_target,
                   liquid_c=liquid, pump_rpm=rpm, pump_duty=duty,
                   power_w=self.power.read(), cpu_util=util, cpu_mhz=mhz,
                   cpu_c=self.temps.read())
        self.samples.append(s)
        self._check_safety(s)
        return s

    def _check_safety(self, s: Sample) -> None:
        if s.liquid_c is not None and s.liquid_c >= LIQUID_ABORT_C:
            raise SafetyAbort(f"coolant reached {s.liquid_c:.1f} C (limit {LIQUID_ABORT_C} C)")
        if s.pump_target == 100 and s.pump_rpm is not None and s.pump_rpm < PUMP_DEAD_RPM:
            self._dead_pump_count += 1
            if self._dead_pump_count >= 3:
                raise SafetyAbort(f"pump reports {s.pump_rpm:.0f} rpm at 100 % duty - "
                                  "pump failure suspected")
        else:
            self._dead_pump_count = 0

    def _live(self, s: Sample, phase_label: str, elapsed: float, total: float) -> None:
        tctl = s.cpu_c.get("Tctl")
        parts = [
            f"[{phase_label:<9} {fmt_mmss(elapsed)}/{fmt_mmss(total)}]",
            f"coolant {fmt(s.liquid_c, '.1f')} C",
            f"pump {fmt(s.pump_rpm, '.0f')} rpm @ {fmt(s.pump_duty, '.0f')} %",
            f"Tctl {fmt(tctl, '.1f')} C",
            f"CPU {fmt(s.power_w, '.0f')} W {fmt(s.cpu_util, '.0f')} % "
            f"{fmt(s.cpu_mhz, '.0f')} MHz",
        ]
        line = "  ".join(parts)
        if self._tty:
            print("\r" + line.ljust(125), end="", flush=True)
        elif time.monotonic() - self._last_print >= 30:
            print(line, flush=True)
            self._last_print = time.monotonic()

    def run_for(self, phase: str, seconds: float, label: str | None = None,
                stop_when=None, shown_offset: float = 0, shown_total: float | None = None,
                newline: bool = True) -> None:
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < seconds:
            tick = time.monotonic() + SAMPLE_INTERVAL_S
            s = self.sample(phase)
            self._live(s, label or phase, shown_offset + elapsed, shown_total or seconds)
            if stop_when is not None and stop_when(self.samples, start):
                break
            time.sleep(max(0.0, tick - time.monotonic()))
        if self._tty and newline:
            print()


def idle_is_stable(min_seconds: float):
    def predicate(samples: list[Sample], start: float) -> bool:
        if time.monotonic() - start < min_seconds:
            return False
        window = tail(samples, "idle", IDLE_WINDOW_S)
        slope = slope_per_min([(s.t, s.liquid_c) for s in window])
        return slope is not None and abs(slope) < STABLE_SLOPE_K_PER_MIN
    return predicate


# --------------------------------------------------------------------------- #
# Test sequence
# --------------------------------------------------------------------------- #

def run_test(rec: Recorder, cooling: NzxtCooling, load: CpuLoad, args, run: dict) -> None:
    print(f"\n> Pinning all NZXT fan channels to {args.fan_duty} %")
    run["fan_pin_problems"] = cooling.set_fans(args.fan_duty)
    for problem in run["fan_pin_problems"]:
        warn(problem)
    time.sleep(5)
    run["fan_rpm_at_test_duty"] = cooling.fan_rpms()
    run["fan_details"] = cooling.fan_details()
    for name, rpms in run["fan_rpm_at_test_duty"].items():
        print(f"  {name}: {rpms} rpm")
    for channel, info in run["fan_details"].items():
        print(f"    {channel}: " + ", ".join(f"{k} {v}" for k, v in info.items()))

    print("\n> Pump sweep")
    for duty in SWEEP_DUTIES:
        rec.pump_target = duty
        cooling.set_pump(duty)
        step_start = rec.now()
        rec.run_for("sweep", args.sweep_settle + args.sweep_measure, label=f"pump {duty}%")
        run["sweep_steps"].append({"duty": duty, "measure_from": step_start + args.sweep_settle})

    print("\n> Idle baseline (pump 100 %, waiting for stable coolant temperature)")
    rec.pump_target = 100
    cooling.set_pump(100)
    if args.prom_url and args.prom_query:
        run["ambient_samples"].append(query_prometheus(args.prom_url, args.prom_query))
    rec.run_for("idle", args.idle_max_minutes * 60,
                stop_when=idle_is_stable(args.idle_min_minutes * 60))

    print(f"\n> Load ({load.method}, {args.load_minutes} min)")
    total = args.load_minutes * 60
    warmup = min(LOAD_CHECK_S, total)
    load.start()
    try:
        rec.run_for("load", warmup, shown_total=total, newline=False)
        verify_load(rec, load)
        rec.run_for("load", total - warmup, shown_offset=warmup, shown_total=total)
    finally:
        load.stop()
    if args.prom_url and args.prom_query:
        run["ambient_samples"].append(query_prometheus(args.prom_url, args.prom_query))

    print(f"\n> Cooldown ({args.cooldown_minutes} min)")
    rec.run_for("cooldown", args.cooldown_minutes * 60)


def verify_load(rec: Recorder, load: CpuLoad) -> None:
    """Fail fast instead of measuring a load that is not there."""
    code = load.exit_code()
    if code is not None:
        raise InvalidLoad(f"{load.method} exited early with code {code}: {load.log_tail()}")
    utils = [s.cpu_util for s in tail(rec.samples, "load", 10) if s.cpu_util is not None]
    if utils and statistics.fmean(utils) < LOAD_MIN_UTIL:
        raise InvalidLoad(f"CPU utilisation only {statistics.fmean(utils):.0f} % under load "
                          f"(expected >= {LOAD_MIN_UTIL:.0f} %) - see {load.log_path}")


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def analyze(run: dict, samples: list[Sample]) -> dict:
    m: dict = {}
    checks: list[dict] = []

    # --- pump sweep -------------------------------------------------------- #
    sweep = []
    sweep_samples = [s for s in samples if s.phase == "sweep"]
    for step in run["sweep_steps"]:
        window = [s for s in sweep_samples
                  if s.pump_target == step["duty"] and s.t >= step["measure_from"]]
        sweep.append({"duty": step["duty"],
                      "rpm": median_of(s.pump_rpm for s in window),
                      "duty_readback": mean(s.pump_duty for s in window)})
    m["sweep"] = sweep

    measured_sweep = [p for p in sweep if p["rpm"] is not None]
    if len(measured_sweep) >= 2:
        monotonic = all(b["rpm"] > a["rpm"] * 1.03
                        for a, b in zip(measured_sweep, measured_sweep[1:], strict=False))
        checks.append(check(
            "Pump responds to duty changes",
            " / ".join(f"{p['duty']}%: {p['rpm']:.0f}" for p in measured_sweep),
            "RPM rises with every step",
            OK if monotonic else WARN,
            "" if monotonic else "Pump does not follow its duty cleanly"))

    readback_off = [p for p in sweep
                    if p["duty_readback"] is not None and abs(p["duty_readback"] - p["duty"]) > 5]
    if any(p["duty_readback"] is not None for p in sweep):
        checks.append(check(
            "Pump duty control",
            "matches target" if not readback_off else
            ", ".join(f"set {p['duty']}% read {p['duty_readback']:.0f}%" for p in readback_off),
            "readback within 5 % of target",
            OK if not readback_off else WARN,
            "" if not readback_off else "Something else may still be controlling the pump"))

    # --- pump at 100 % during idle + load ------------------------------------ #
    pump_100 = [s.pump_rpm for s in samples
                if s.phase in ("idle", "load") and s.pump_target == 100 and s.pump_rpm]
    if len(pump_100) >= 10:
        median = statistics.median(pump_100)
        dips = [v for v in pump_100 if v < median * PUMP_DIP_RATIO]
        steady = [v for v in pump_100 if v >= median * PUMP_DIP_RATIO]
        rpm = statistics.fmean(steady)
        cv = statistics.pstdev(steady) / rpm if rpm else 0.0
        share = len(dips) / len(pump_100)
        m["pump_rpm_100"], m["pump_rpm_cv"] = rpm, cv
        m["pump_dips"], m["pump_dip_share"] = len(dips), share
        spec_min = PUMP_SPEC_RPM - PUMP_SPEC_TOL
        checks.append(check(
            "Pump speed at 100 %", f"{rpm:.0f} rpm",
            f">= {spec_min} rpm (spec {PUMP_SPEC_RPM} +/- {PUMP_SPEC_TOL})",
            OK if rpm >= spec_min else WARN if rpm >= spec_min * 0.9 else FAIL,
            "" if rpm >= spec_min else "Below spec - bearing wear or electrical issue"))
        checks.append(check(
            "Pump speed stability", f"{cv * 100:.1f} % variation",
            f"< {PUMP_CV_WARN * 100:.0f} %",
            grade(cv, PUMP_CV_WARN, PUMP_CV_FAIL),
            "" if cv <= PUMP_CV_WARN else "Fluctuating RPM - air in the loop or bearing wear"))
        if dips:
            depth = statistics.fmean(dips) / median
            status = INFO if share <= 0.02 else WARN if share <= 0.10 else FAIL
            checks.append(check(
                "Short pump speed dips",
                f"{len(dips)} of {len(pump_100)} readings, down to ~{depth * 100:.0f} %",
                "none or isolated", status,
                "Isolated dips are often a reporting glitch - watch whether they become "
                "more frequent" if status == INFO else
                "Frequent drops - listen for rattling or gurgling"))
        else:
            checks.append(check("Short pump speed dips", "none", "none or isolated", OK))

    # --- thermal performance ---------------------------------------------- #
    idle = tail(samples, "idle", IDLE_WINDOW_S)
    load_ss = tail(samples, "load", STEADY_WINDOW_S)
    load_all = [s for s in samples if s.phase == "load"]
    if len(idle) >= 5 and len(load_ss) >= 10:
        liq_idle = mean(s.liquid_c for s in idle)
        liq_load = mean(s.liquid_c for s in load_ss)
        p_idle = mean(s.power_w for s in idle)
        p_load = mean(s.power_w for s in load_ss)
        d_t = liq_load - liq_idle
        m.update(liquid_idle=liq_idle, liquid_load=liq_load, delta_t=d_t,
                 power_idle=p_idle, power_load=p_load)

        util_load = mean(s.cpu_util for s in load_ss)
        mhz_load = mean(s.cpu_mhz for s in load_ss)
        m["util_load"], m["mhz_load"] = util_load, mhz_load
        if p_idle is not None and p_load is not None:
            extra = p_load - p_idle
            checks.append(check(
                "Heat produced by the load", f"{extra:.0f} W extra  ("
                f"{fmt(util_load, '.0f')} % CPU at {fmt(mhz_load, '.0f')} MHz)",
                f">= {LOAD_MIN_EXTRA_W:.0f} W extra",
                OK if extra >= LOAD_MIN_EXTRA_W else WARN,
                "" if extra >= LOAD_MIN_EXTRA_W else
                "Too little heat for a precise result - check power limits, "
                "Eco mode and the power profile"))

        if p_idle is not None and p_load is not None and p_load - p_idle > 20:
            r = d_t / (p_load - p_idle)
            m["r_coolant"] = r
            checks.append(check(
                "Coolant rise per watt", f"{r:.3f} K/W  ({d_t:.1f} K for "
                f"{p_load - p_idle:.0f} W)", f"< {R_WARN} K/W (heuristic)",
                grade(r, R_WARN, R_FAIL),
                "" if r <= R_WARN else "Radiator/pump moves less heat than expected"))
        else:
            checks.append(check(
                "Coolant rise under load", f"{d_t:.1f} K", f"< {DT_WARN:.0f} K (heuristic)",
                grade(d_t, DT_WARN, DT_FAIL), "CPU power unavailable - less precise"))

        tctl_load = mean(s.cpu_c.get("Tctl") for s in load_ss)
        if tctl_load is not None and p_load:
            r_plate = (tctl_load - liq_load) / p_load
            m["tctl_load"], m["r_coldplate"] = tctl_load, r_plate
            checks.append(check(
                "CPU-to-coolant transfer", f"{r_plate:.3f} K/W  (Tctl {tctl_load:.1f} C)",
                "watch the trend", INFO,
                "Rising over time points to paste or mounting, not the AIO itself"))

        slope = slope_per_min([(s.t, s.liquid_c) for s in load_ss])
        m["load_end_slope"] = slope
        if slope is not None:
            steady = slope < 0.15
            checks.append(check(
                "Coolant reached steady state", f"{slope:+.2f} K/min at the end",
                "< 0.15 K/min", OK if steady else WARN,
                "" if steady else "Not settled - use a longer --load-minutes for valid numbers"))

        ambient = [a for a in run["ambient_samples"] if a is not None]
        if run.get("ambient_manual") is not None:
            ambient = [run["ambient_manual"]]
        if ambient:
            amb = statistics.fmean(ambient)
            m["ambient"] = amb
            m["liquid_over_ambient"] = liq_load - amb
            checks.append(check(
                "Coolant over room temperature", f"{liq_load - amb:.1f} K (room {amb:.1f} C)",
                "watch the trend", INFO))

    peak = max((s.liquid_c for s in load_all if s.liquid_c is not None), default=None)
    if peak is not None:
        m["liquid_peak"] = peak
        checks.append(check("Peak coolant temperature", f"{peak:.1f} C",
                            f"< {PEAK_LIQUID_WARN:.0f} C",
                            OK if peak < PEAK_LIQUID_WARN else
                            WARN if peak < LIQUID_ABORT_C else FAIL))

    if run["status"] == "aborted":
        checks.insert(0, check("Safety limits", run["error"], "no limit reached", FAIL,
                               "Test stopped early - check the AIO before heavy use"))

    # --- fan channels at the fixed test duty ------------------------------------ #
    if run.get("fan_pin_problems"):
        checks.insert(0, check(
            "Fans pinned to test duty", "; ".join(run["fan_pin_problems"]),
            "every channel at the test duty", WARN,
            "Airflow was not as intended - thermal numbers are not comparable"))

    spinning = {f"{dev} fan{i + 1}": rpm
                for dev, rpms in run.get("fan_rpm_at_test_duty", {}).items()
                for i, rpm in enumerate(rpms) if rpm > 0}
    if len(spinning) >= 3:
        median = statistics.median(spinning.values())
        slow = {ch: rpm for ch, rpm in spinning.items() if rpm < median * FAN_OUTLIER_RATIO}
        checks.append(check(
            "Fan channels at the same duty",
            "all similar" if not slow else
            "; ".join(f"{ch}: {rpm} rpm" for ch, rpm in slow.items())
            + f" (others ~{median:.0f} rpm)",
            f">= {FAN_OUTLIER_RATIO * 100:.0f} % of the median rpm",
            OK if not slow else WARN,
            "" if not slow else "Check DC/PWM control mode, splitter and fan health "
            "- if this is the radiator, the thermal numbers are not representative"))

    # --- cooldown time constant ---------------------------------------------- #
    cool = [s for s in samples if s.phase == "cooldown" and s.liquid_c is not None]
    if cool and "liquid_idle" in m:
        start_t, start_v = cool[0].t, cool[0].liquid_c
        target = start_v - (start_v - m["liquid_idle"]) * (1 - math.exp(-1))
        hit = next((s for s in cool if s.liquid_c <= target), None)
        m["cooldown_tau_s"] = (hit.t - start_t) if hit else None

    if run["status"] == "invalid":
        checks.insert(0, check("Test validity", run["error"], "CPU fully loaded", FAIL,
                               "No verdict on the AIO - fix the load and run again"))
    worst = max((SEVERITY[c["status"]] for c in checks), default=0)
    run["metrics"], run["checks"] = m, checks
    run["verdict"] = INVALID if run["status"] == "invalid" else {0: OK, 1: WARN, 2: FAIL}[worst]
    return run


def fingerprint(run: dict) -> dict:
    """Runs are only comparable when these settings match."""
    cfg = run["config"]
    return {"device": run["device"]["kraken"], "fan_duty": cfg["fan_duty"],
            "load_method": cfg["load_method"], "load_minutes": cfg["load_minutes"],
            # BIOS modes like X3D Turbo change the heat output - never mix them
            "cpu_threads": run.get("system", {}).get("online_cpus"),
            # the power profile shifts idle power and therefore the measured rise
            "power_profile": run.get("system", {}).get("power_profile")}


def add_trend(run: dict, history: list[dict]) -> None:
    comparable = [h for h in history if fingerprint(h) == fingerprint(run)
                  and h.get("status") == "completed"]
    run["trend"] = {"comparable_runs": len(comparable)}
    if not comparable or run["status"] != "completed":
        return
    base = comparable[0]
    key = "r_coolant" if "r_coolant" in run["metrics"] else "delta_t"
    now, then = run["metrics"].get(key), base["metrics"].get(key)
    if now is None or not then:
        return
    change = now / then - 1
    run["trend"].update(metric=key, baseline=then, baseline_time=base["started"], change=change)
    run["checks"].append(check(
        "Trend vs. first run", f"{change * 100:+.0f} %  ({then:.3f} -> {now:.3f})",
        f"< +{TREND_WARN * 100:.0f} %", grade(change, TREND_WARN, TREND_FAIL),
        f"Baseline from {base['started'][:10]}"))
    if run["verdict"] != INVALID:
        worst = max(SEVERITY[c["status"]] for c in run["checks"])
        run["verdict"] = {0: OK, 1: WARN, 2: FAIL}[worst]


# --------------------------------------------------------------------------- #
# Console report
# --------------------------------------------------------------------------- #


PUMP_CHECKS = ("Pump", "Short pump", "Safety")


def verdict_text(run: dict) -> str:
    """Name the likely cause instead of jumping straight to 'replace it'."""
    verdict = run["verdict"]
    if verdict == INVALID:
        return "Test invalid - no verdict on the AIO"
    if verdict == OK:
        return "Healthy - no signs of wear"
    bad = [c for c in run["checks"] if c["status"] == verdict]
    if verdict == FAIL and any(c["name"].startswith(PUMP_CHECKS) for c in bad):
        return "Pump or safety problem - plan a replacement"
    if verdict == FAIL:
        return "Heat rejection weak - check radiator fans and dust before replacing"
    return "Watch it - some values are off"


def print_report(run: dict) -> None:
    m = run["metrics"]
    print("\n" + "=" * 78)
    print(f" AIO health check  {run['device']['kraken']}  "
          f"({run['started'][:16].replace('T', ' ')})")
    print("=" * 78)
    if run["status"] != "completed":
        print(color(f" Run {run['status']}: {run.get('error') or 'partial data'}", FAIL))
    print_checks(run["checks"])
    print("-" * 78)
    print(f" Coolant idle/load : {fmt(m.get('liquid_idle'), '.1f')} / "
          f"{fmt(m.get('liquid_load'), '.1f')} C    "
          f"CPU power idle/load: {fmt(m.get('power_idle'), '.0f')} / "
          f"{fmt(m.get('power_load'), '.0f')} W")
    tau = m.get("cooldown_tau_s")
    print(f" Cooldown time constant: {fmt(tau, '.0f')} s    "
          f"Comparable earlier runs: {run['trend']['comparable_runs']}")
    print("-" * 78)
    print(" Verdict: " + color(verdict_text(run), run["verdict"]))
    print("=" * 78)


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #


def render_html(run: dict, history: list[dict]) -> str:
    esc = html.escape
    samples = run["samples"]
    m = run["metrics"]

    def series(getter):
        return [(s["t"] / 60, getter(s)) for s in samples]

    bands, current, start = [], None, 0.0
    for s in samples:
        if s["phase"] != current:
            if current is not None:
                bands.append((start, s["t"] / 60, current))
            current, start = s["phase"], s["t"] / 60
    if current is not None:
        bands.append((start, samples[-1]["t"] / 60, current))

    coolant_series = [{"name": "Coolant", "cls": "coolant",
                       "points": series(lambda s: s["liquid_c"])}]
    cpu_series = [{"name": "CPU Tctl", "cls": "heat",
                   "points": series(lambda s: s["cpu_c"].get("Tctl"))}]
    pump_series = [{"name": "Pump speed", "cls": "pump",
                    "points": series(lambda s: s["pump_rpm"])}]
    power_series = [{"name": "CPU package power", "cls": "power",
                     "points": series(lambda s: s["power_w"])}]

    sweep_points = [(p["duty"], p["rpm"]) for p in m.get("sweep", [])]
    sweep_series = [{"name": "Measured pump speed", "cls": "pump", "points": sweep_points,
                     "dots": True}]

    comparable = [h for h in history if fingerprint(h) == fingerprint(run)
                  and h.get("status") == "completed"] + [run]
    trend_key = "r_coolant" if "r_coolant" in m else "delta_t"
    trend_points = [(i, h["metrics"].get(trend_key)) for i, h in enumerate(comparable)]
    trend_labels = [(i, h["started"][2:10]) for i, h in enumerate(comparable)]
    if len(comparable) > 1:
        trend_html = ('<p class="sub">' + ("Coolant rise per watt of CPU power (K/W), "
                      if trend_key == "r_coolant" else "Coolant rise under load (K), ")
                      + 'lower is better.</p><div class="scroll">'
                      + svg_chart([{"name": "trend", "cls": "coolant", "points": trend_points,
                                    "dots": True}], y_unit="K/W" if trend_key == "r_coolant"
                                  else "K", x_labels=trend_labels) + "</div>")
    else:
        trend_html = ('<p class="empty">This is the first run with these settings. '
                      'Repeat the test every few months with the same options to see '
                      'whether the AIO degrades.</p>')

    alert = ""
    if run["status"] != "completed":
        alert = (f'<div class="alert"><strong>Run {esc(run["status"])}.</strong> '
                 f'{esc(run.get("error") or "Results are based on partial data.")}</div>')

    cfg, dev, sysinfo = run["config"], run["device"], run.get("system", {})
    fans = "; ".join(f"{esc(k)}: {', '.join(map(str, v))} rpm"
                     for k, v in run.get("fan_rpm_at_test_duty", {}).items()) or "none found"
    spec_min = PUMP_SPEC_RPM - PUMP_SPEC_TOL

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIO health check {esc(run['started'][:10])}</title>
<style>{CSS}</style></head>
<body><main>
<header>
  {badge(run)}
  <div>
    <h1>AIO health check</h1>
    <p class="sub">{esc(dev['kraken'])} on {esc(run['host'])}, {esc(run['started'][:16].replace('T', ' '))}</p>
    <p class="verdict v-{run['verdict'].lower()}-text">{esc(verdict_text(run))}</p>
  </div>
</header>
{alert}
<h2>Checks</h2>
{checks_table(run["checks"])}

<h2>Coolant temperature</h2>
<div class="scroll">{svg_chart(coolant_series, y_unit="°C", bands=bands)}</div>

<h2>CPU temperature</h2>
<div class="scroll">{svg_chart(cpu_series, y_unit="°C", bands=bands)}</div>

<h2>Pump speed</h2>
<div class="scroll">{svg_chart(pump_series, y_unit="rpm", bands=bands,
                               ref_lines=[(spec_min, f"spec minimum {spec_min} rpm")])}</div>

<h2>Pump response</h2>
<div class="scroll">{svg_chart(sweep_series, y_unit="rpm", x_unit="% duty",
                               x_labels=[(d, f"{d} %") for d, _ in sweep_points],
                               ref_lines=[(spec_min, "spec minimum at 100 %")])}</div>

<h2>CPU package power</h2>
<div class="scroll">{svg_chart(power_series, y_unit="W", bands=bands)}</div>

<h2>Trend across runs</h2>
{trend_html}

<h2>Test conditions</h2>
<dl>
  <dt>Fan duty (all NZXT channels)</dt><dd>{cfg['fan_duty']} %</dd>
  <dt>Fan speeds at test duty</dt><dd>{fans}</dd>
  <dt>Load</dt><dd>{esc(cfg['load_method'])}, {cfg['load_minutes']} min,
      {fmt(m.get('util_load'), '.0f')} % CPU at {fmt(m.get('mhz_load'), '.0f')} MHz</dd>
  <dt>CPU</dt><dd>{esc(str(sysinfo.get('cpu_model')))},
      {sysinfo.get('usable_cpus')}/{sysinfo.get('online_cpus')} threads usable</dd>
  <dt>Power settings</dt><dd>profile {esc(str(sysinfo.get('power_profile')))},
      EPP {esc(str(sysinfo.get('epp')))}, boost {esc(str(sysinfo.get('boost')))},
      amd_pstate {esc(str(sysinfo.get('amd_pstate')))}</dd>
  <dt>Room temperature</dt><dd>{fmt(m.get('ambient'), '.1f')} °C</dd>
  <dt>Cooldown time constant</dt><dd>{fmt(m.get('cooldown_tau_s'), '.0f')} s</dd>
  <dt>Kraken kernel driver</dt><dd>{esc(dev.get('kraken_hwmon') or 'none (direct HID)')}</dd>
  <dt>Kernel / liquidctl</dt><dd>{esc(run['kernel'])} / {esc(dev['liquidctl'])}</dd>
</dl>
</main></body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Automated health check for an NZXT Kraken X AIO (needs root).")
    p.add_argument("--fan-duty", type=int, default=60,
                   help="fixed duty for all NZXT fan channels during the test (default 60)")
    p.add_argument("--load-minutes", type=float, default=15)
    p.add_argument("--idle-min-minutes", type=float, default=2)
    p.add_argument("--idle-max-minutes", type=float, default=10)
    p.add_argument("--cooldown-minutes", type=float, default=5)
    p.add_argument("--sweep-settle", type=float, default=15,
                   help="seconds per pump step to settle")
    p.add_argument("--sweep-measure", type=float, default=10,
                   help="seconds per pump step to measure")
    p.add_argument("--quick", action="store_true",
                   help="short trial run (5 min load); not comparable with full runs")
    p.add_argument("--ambient", type=float, help="room temperature in °C, if known")
    p.add_argument("--prom-url", help="Prometheus base URL for the room temperature")
    p.add_argument("--prom-query", help="PromQL returning exactly one room-temperature series")
    p.add_argument("--out-dir", type=Path,
                   help=f"results directory (default ~/{RESULTS_DIR} of the sudo user)")
    p.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    args = p.parse_args()
    if args.quick:
        args.load_minutes, args.idle_min_minutes, args.cooldown_minutes = 5, 1, 2
    if not 20 <= args.fan_duty <= 100:
        p.error("--fan-duty must be between 20 and 100")
    if bool(args.prom_url) != bool(args.prom_query):
        p.error("--prom-url and --prom-query must be used together")
    return args


def main() -> None:
    args = parse_args()
    ensure_root()
    install_signal_handlers()

    out_dir, uid, gid = prepare_results_dir(args.out_dir)

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d-%H%M%S")

    print("> Preflight")
    temps, power, activity = CpuTemps(), CpuPower(), CpuActivity()
    snapshot = system_snapshot()
    load_seconds = int(args.load_minutes * 60)
    load = CpuLoad(max_seconds=load_seconds + 60,
                   log_path=out_dir / f"{FILE_PREFIX}-stress-{stamp}.log")
    if load.method == "python":
        warn("stress-ng not found, using a Python busy loop (lower power draw). "
             "Install it for comparable results: sudo pacman -S stress-ng")
    print(f"  CPU sensors: {', '.join(temps.inputs) or 'none'}  |  "
          f"RAPL power: {'yes' if power.available else 'no'}  |  load: {load.method}")
    print(f"  CPU: {snapshot['cpu_model']}  |  threads: {snapshot['usable_cpus']}/"
          f"{snapshot['online_cpus']}  |  profile: {snapshot['power_profile']}  |  "
          f"EPP: {snapshot['epp']}  |  boost: {snapshot['boost']}")
    warn_about_snapshot(snapshot)
    run: dict = {
        "started": started.isoformat(timespec="seconds"),
        "host": platform.node(), "kernel": platform.release(),
        "status": "completed", "error": None, "system": snapshot,
        "config": {"fan_duty": args.fan_duty, "load_minutes": args.load_minutes,
                   "load_method": load.method},
        "ambient_manual": args.ambient, "ambient_samples": [],
        "sweep_steps": [], "fan_pin_problems": [], "fan_rpm_at_test_duty": {},
    }

    print("> Pausing CoolerControl")
    with CoolerControlPause():
        cooling = NzxtCooling()
        run["device"] = {"kraken": cooling.kraken_name, "kraken_hwmon": cooling.kraken_hwmon,
                         "fan_controllers": [n for n, _ in cooling.fan_devices],
                         "liquidctl": cooling.liquidctl_version}
        print(f"  Kraken: {cooling.kraken_name} (kernel driver: "
              f"{cooling.kraken_hwmon or 'none'})")
        print(f"  Fan controllers: {', '.join(run['device']['fan_controllers']) or 'none'}")
        rec = Recorder(cooling, temps, power, activity)
        try:
            run_test(rec, cooling, load, args, run)
        except SafetyAbort as exc:
            run["status"], run["error"] = "aborted", str(exc)
            print(f"\n!!! SAFETY ABORT: {exc}")
        except InvalidLoad as exc:
            run["status"], run["error"] = "invalid", str(exc)
            print(f"\n!!! TEST INVALID: {exc}")
        except KeyboardInterrupt:
            run["status"], run["error"] = "interrupted", "Interrupted by user"
            print("\n> Interrupted - restoring cooling")
        finally:
            load.stop()
            try:
                cooling.set_pump(100)           # safe state until CoolerControl takes over
            except Exception as exc:  # noqa: BLE001
                warn(f"could not set pump to 100 %: {exc}")
            cooling.close()
        print("> Restoring CoolerControl")

    run["samples"] = [asdict(s) for s in rec.samples]
    analyze(run, rec.samples)
    history = load_history(out_dir, FILE_PREFIX)
    add_trend(run, history)
    print_report(run)

    report = save_results(out_dir, FILE_PREFIX, stamp, run, render_html(run, history),
                          uid, gid, extra=(load.log_path,))
    if not args.no_open:
        open_report(report)
    else:
        print(f" Report: {report}")


if __name__ == "__main__":
    main()
