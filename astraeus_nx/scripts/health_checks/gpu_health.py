#!/usr/bin/env python3
"""
gpu_health.py - wear-focused health check for an NVIDIA GPU on Linux.

Uses the shared building blocks in health_common.py (CoolerControl pause,
NZXT fan pinning, report styling, persistence).

Test sequence
  1. Stop CoolerControl, pin every NZXT case fan channel and all GPU fans to a
     fixed duty - and verify each one.
  2. Idle baseline until the GPU temperature settles.
  3. Load with gpu-burn (also counts compute errors) for a fixed time.
  4. Cooldown.
  5. Return GPU fans to automatic control, restart CoolerControl, write a
     console summary, an HTML report and the raw run as JSON for trending.

Wear indicators
  - GPU temperature rise per watt at fixed fan duty (paste/pad ageing;
    RTX 50 cards expose no hotspot sensor, so this is the best proxy)
  - RPM of every GPU fan at a fixed duty (bearing wear)
  - thermal or power-brake clock events under load (cooling or PSU trouble)
  - gpu-burn compute errors (GPU/VRAM stability)

Needs root (service control, NVML fan control):
    sudo ./gpu_health.py
Dependencies: python-nvidia-ml-py, gpu-burn (AUR: gpu-burn-git), liquidctl.
"""

from __future__ import annotations

import argparse
import html
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from ctypes import byref
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import health_common as hc
from health_common import (CSS, FAIL, INFO, INVALID, OK, SEVERITY, WARN, InvalidLoad,
                        SafetyAbort, check, die, fmt, fmt_mmss, mean, slope_per_min, tail,
                        warn)

SAMPLE_INTERVAL_S = 2.0
LOAD_CHECK_S = 20
LOAD_MIN_POWER_SHARE = 0.6    # gpu-burn should pull at least 60 % of the power limit
# Safety: abort only at the GPU's own max operating temperature. Running close to
# T.Limit is fine - the firmware throttles there by design, and that is recorded.
GPU_ABORT_AT_MAX_TEMP = True
FAN_SPREAD_RATIO = 0.85       # a GPU fan below 85 % of the others' median is suspicious
FAN_TREND_WARN = -0.10        # -10 % rpm at the same duty vs. baseline
GPU_R_TREND_WARN, GPU_R_TREND_FAIL = 0.15, 0.30
FILE_PREFIX = "gpu"           # results share health_common.RESULTS_DIR, told apart by prefix

# NVML clock event reason bits (verified against nvidia-ml-py 13.615)
REASONS = {
    0x4: "power cap",
    0x8: "hw slowdown",
    0x20: "sw thermal",
    0x40: "hw thermal",
    0x80: "power brake",
    0x400: "reliability",
}
THERMAL_BITS = 0x20 | 0x40
POWER_BRAKE_BIT = 0x80


# --------------------------------------------------------------------------- #
# GPU access via NVML
# --------------------------------------------------------------------------- #

class NvidiaGpu:
    def __init__(self, index: int) -> None:
        try:
            import pynvml as nv
        except ImportError:
            die("pynvml not found - install it with: pacman -S python-nvidia-ml-py")
        self.nv = nv
        nv.nvmlInit()
        self.h = nv.nvmlDeviceGetHandleByIndex(index)
        self.name = nv.nvmlDeviceGetName(self.h)
        self.driver = nv.nvmlSystemGetDriverVersion()
        self.fan_count = nv.nvmlDeviceGetNumFans(self.h)
        self.min_duty, self.max_duty = nv.nvmlDeviceGetMinMaxFanSpeed(self.h)
        self.power_limit_w = nv.nvmlDeviceGetEnforcedPowerLimit(self.h) / 1000
        self.max_temp = self._safe(nv.nvmlDeviceGetTemperatureThreshold, self.h,
                                   nv.NVML_TEMPERATURE_THRESHOLD_GPU_MAX)
        self.fans_pinned = False

    @staticmethod
    def _safe(fn, *args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 - NVML raises many specific error types
            return None

    def fan_rpm(self, fan: int) -> int | None:
        """nvmlDeviceGetFanSpeedRPM for any fan (the binding only queries fan 0)."""
        nv = self.nv
        try:
            info = nv.c_nvmlFanSpeedInfo_t()
            info.version = nv.nvmlFanSpeedInfo_v1
            info.fan = fan
            fn = nv._nvmlGetFunctionPointer("nvmlDeviceGetFanSpeedRPM")
            nv._nvmlCheckReturn(fn(self.h, byref(info)))
            return int(info.speed)
        except Exception:  # noqa: BLE001
            return None

    def read(self) -> dict:
        nv, h = self.nv, self.h
        power = self._safe(nv.nvmlDeviceGetPowerUsage, h)
        return {
            "gpu_c": self._safe(nv.nvmlDeviceGetTemperature, h, nv.NVML_TEMPERATURE_GPU),
            "margin_k": self._safe(nv.nvmlDeviceGetMarginTemperature, h),
            "power_w": power / 1000 if power is not None else None,
            "clock_mhz": self._safe(nv.nvmlDeviceGetClockInfo, h, nv.NVML_CLOCK_GRAPHICS),
            "reasons": self._safe(nv.nvmlDeviceGetCurrentClocksEventReasons, h),
            "fan_duty": [self._safe(nv.nvmlDeviceGetFanSpeed_v2, h, f)
                         for f in range(self.fan_count)],
            "fan_rpm": [self.fan_rpm(f) for f in range(self.fan_count)],
        }

    def set_fans(self, duty: int) -> list[str]:
        """Pin every GPU fan and verify the readback; return a list of problems."""
        problems = []
        for fan in range(self.fan_count):
            try:
                self.nv.nvmlDeviceSetFanSpeed_v2(self.h, fan, duty)
                self.fans_pinned = True
            except Exception as exc:  # noqa: BLE001
                problems.append(f"GPU fan {fan}: write failed ({exc})")
        time.sleep(8)                                   # let the fans spin up
        state = self.read()
        for fan, (d, rpm) in enumerate(zip(state["fan_duty"], state["fan_rpm"], strict=True)):
            if d is None or abs(d - duty) > 3:
                problems.append(f"GPU fan {fan}: duty reads {d} % instead of {duty} %")
            if not rpm:
                problems.append(f"GPU fan {fan}: no rpm reading - bearing trend unavailable")
        return problems

    def reset_fans(self) -> None:
        for fan in range(self.fan_count):
            self._safe(self.nv.nvmlDeviceSetDefaultFanSpeed_v2, self.h, fan)
        self.fans_pinned = False

    def close(self) -> None:
        if self.fans_pinned:
            self.reset_fans()
        self._safe(self.nv.nvmlShutdown)


# --------------------------------------------------------------------------- #
# gpu-burn
# --------------------------------------------------------------------------- #

def find_gpu_burn() -> tuple[str, str]:
    """Locate the gpu-burn binary and its compare kernel via the package file list."""
    binary = shutil.which("gpu_burn") or shutil.which("gpu-burn")
    if not binary:
        die("gpu-burn not found - install it with: paru -S gpu-burn-git")
    owner = subprocess.run(["pacman", "-Qoq", binary], capture_output=True, text=True)
    files = []
    if owner.returncode == 0:
        listing = subprocess.run(["pacman", "-Qlq", owner.stdout.strip()],
                                 capture_output=True, text=True).stdout.split()
        files = [f for f in listing if re.search(r"compare\.(fatbin|ptx)$", f)]
    if not files:
        die(f"no compare kernel (compare.fatbin/.ptx) found for {binary} - "
            f"check: pacman -Ql $(pacman -Qoq {binary})")
    return binary, files[0]


class GpuBurn:
    def __init__(self, binary: str, kernel: str, gpu_index: int, log_path: Path) -> None:
        self.binary, self.kernel, self.gpu_index, self.log_path = (
            binary, kernel, gpu_index, log_path)
        self._proc: subprocess.Popen | None = None
        self._log = None

    def start(self, seconds: int) -> None:
        self._log = open(self.log_path, "w")
        self._proc = subprocess.Popen(
            [self.binary, "-i", str(self.gpu_index), "-c", self.kernel, str(seconds)],
            cwd=str(Path(self.kernel).parent), stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True)

    def exit_code(self) -> int | None:
        return self._proc.poll() if self._proc else None

    def wait(self, timeout: float) -> None:
        """Let gpu-burn finish on its own so it prints its final OK/FAULTY summary."""
        if self._proc:
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                warn("gpu-burn did not finish in time - stopping it")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)
        if self._log:
            self._log.close()
        self._proc = self._log = None

    def result(self) -> dict:
        """Parse error count and final verdict from the gpu-burn log."""
        text = hc.read_text(str(self.log_path)) or ""
        errors = [int(n) for n in re.findall(r"errors:\s*(\d+)", text)]
        verdicts = re.findall(r"GPU \d+:\s*(OK|FAULTY)", text)
        return {"errors": max(errors) if errors else None,
                "verdict": verdicts[-1] if verdicts else None,
                "tail": " | ".join(text.replace("\r", "\n").splitlines()[-3:])}


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #

@dataclass
class GpuSample:
    t: float
    phase: str
    gpu_c: float | None
    margin_k: float | None
    power_w: float | None
    clock_mhz: float | None
    reasons: int | None
    fan_rpm: list = field(default_factory=list)


class GpuRecorder:
    def __init__(self, gpu: NvidiaGpu, cooling: hc.NzxtCooling) -> None:
        self.gpu, self.cooling = gpu, cooling
        self.samples: list[GpuSample] = []
        self.t0 = time.monotonic()
        self._tty = sys.stdout.isatty()

    def sample(self, phase: str) -> GpuSample:
        g = self.gpu.read()
        s = GpuSample(t=round(time.monotonic() - self.t0, 1), phase=phase,
                      gpu_c=g["gpu_c"], margin_k=g["margin_k"], power_w=g["power_w"],
                      clock_mhz=g["clock_mhz"], reasons=g["reasons"], fan_rpm=g["fan_rpm"])
        self.samples.append(s)
        limit = self.gpu.max_temp
        if GPU_ABORT_AT_MAX_TEMP and limit and s.gpu_c is not None and s.gpu_c >= limit:
            raise SafetyAbort(f"GPU core reached {s.gpu_c} C (max operating {limit} C)")
        return s

    def run_for(self, phase: str, seconds: float, stop_when=None, shown_offset: float = 0,
                shown_total: float | None = None, newline: bool = True) -> None:
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < seconds:
            tick = time.monotonic() + SAMPLE_INTERVAL_S
            s = self.sample(phase)
            line = (f"[{phase:<8} {fmt_mmss(shown_offset + elapsed)}/"
                    f"{fmt_mmss(shown_total or seconds)}]  GPU {fmt(s.gpu_c, '.0f')} C  "
                    f"{fmt(s.power_w, '.0f')} W  {fmt(s.clock_mhz, '.0f')} MHz  "
                    f"fans {'/'.join(str(r) for r in s.fan_rpm)} rpm")
            if self._tty:
                print("\r" + line.ljust(110), end="", flush=True)
            if stop_when is not None and stop_when(self.samples, start):
                break
            time.sleep(max(0.0, tick - time.monotonic()))
        if self._tty and newline:
            print()


def gpu_idle_stable(min_seconds: float):
    def predicate(samples, start) -> bool:
        if time.monotonic() - start < min_seconds:
            return False
        slope = slope_per_min([(s.t, s.gpu_c) for s in tail(samples, "idle", 60)])
        return slope is not None and abs(slope) < 0.3
    return predicate


# --------------------------------------------------------------------------- #
# Test sequence
# --------------------------------------------------------------------------- #

def run_test(rec: GpuRecorder, gpu: NvidiaGpu, cooling: hc.NzxtCooling, burn: GpuBurn,
             args, run: dict) -> None:
    # Case fans are pinned too: the bottom intakes feed the GPU directly, so their
    # speed changes the GPU temperature and must be identical between runs.
    print(f"\n> NZXT case fans {args.fan_duty} %, GPU fans {args.gpu_fan_duty} %")
    run["fan_pin_problems"] = cooling.set_fans(args.fan_duty)
    run["fan_pin_problems"] += gpu.set_fans(args.gpu_fan_duty)
    for problem in run["fan_pin_problems"]:
        warn(problem)
    state = gpu.read()
    run["gpu_fan_rpm_at_test_duty"] = state["fan_rpm"]
    print(f"  GPU fans: {state['fan_rpm']} rpm at {state['fan_duty']} %")

    print("\n> Idle baseline (waiting for stable GPU temperature)")
    rec.run_for("idle", args.idle_max_minutes * 60,
                stop_when=gpu_idle_stable(args.idle_min_minutes * 60))

    total = int(args.load_minutes * 60)
    print(f"\n> Load (gpu-burn, {args.load_minutes} min)")
    burn.start(total + 10)       # runs slightly past the recording window, then ends itself
    try:
        rec.run_for("load", LOAD_CHECK_S, shown_total=total, newline=False)
        if burn.exit_code() is not None:
            raise InvalidLoad(f"gpu-burn exited early: {burn.result()['tail']}")
        power = mean(s.power_w for s in tail(rec.samples, "load", 10))
        if power is not None and power < gpu.power_limit_w * LOAD_MIN_POWER_SHARE:
            raise InvalidLoad(f"GPU only draws {power:.0f} W under gpu-burn "
                              f"(limit {gpu.power_limit_w:.0f} W) - see {burn.log_path}")
        rec.run_for("load", total - LOAD_CHECK_S, shown_offset=LOAD_CHECK_S, shown_total=total)
        burn.wait(timeout=60)
    finally:
        burn.stop()
    run["burn"] = burn.result()

    print(f"\n> Cooldown ({args.cooldown_minutes} min)")
    rec.run_for("cooldown", args.cooldown_minutes * 60)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def analyze(run: dict, samples: list[GpuSample]) -> None:
    m: dict = {}
    checks: list[dict] = []

    if run["status"] == "aborted":
        checks.append(check("Safety limits", run["error"], "no limit reached", FAIL))
    if run["status"] == "invalid":
        checks.append(check("Test validity", run["error"], "GPU fully loaded", FAIL,
                            "No verdict on the GPU - fix the load and run again"))
    if run.get("fan_pin_problems"):
        checks.append(check("Fans pinned to test duty", "; ".join(run["fan_pin_problems"]),
                            "every fan at its test duty", WARN,
                            "Results are not comparable with other runs"))

    # --- compute errors ---------------------------------------------------- #
    burn = run.get("burn") or {}
    if burn:
        errors, verdict = burn.get("errors"), burn.get("verdict")
        bad = (errors or 0) > 0 or verdict == "FAULTY"
        checks.append(check("Compute errors (gpu-burn)",
                            f"{fmt(errors, 'd')} errors, verdict {verdict or 'n/a'}",
                            "0 errors, OK", FAIL if bad else OK,
                            "Calculation errors point to unstable GPU or VRAM" if bad else ""))

    # --- GPU fans ----------------------------------------------------------- #
    rpms = run.get("gpu_fan_rpm_at_test_duty") or []
    m["gpu_fan_rpm"] = rpms
    valid = [r for r in rpms if r]
    if len(valid) >= 2:
        median = statistics.median(valid)
        slow = [i for i, r in enumerate(rpms) if r and r < median * FAN_SPREAD_RATIO]
        checks.append(check("GPU fans at the same duty",
                            " / ".join(f"fan {i}: {r}" for i, r in enumerate(rpms)),
                            f"each >= {FAN_SPREAD_RATIO * 100:.0f} % of the others",
                            WARN if slow else OK,
                            f"Fan {slow} slower than its siblings - bearing wear?" if slow
                            else ""))

    # --- thermal performance ------------------------------------------------ #
    idle = tail(samples, "idle", 60)
    load_ss = tail(samples, "load", 120)
    load_all = [s for s in samples if s.phase == "load"]
    if len(idle) >= 5 and len(load_ss) >= 10:
        t_idle, t_load = mean(s.gpu_c for s in idle), mean(s.gpu_c for s in load_ss)
        p_idle, p_load = mean(s.power_w for s in idle), mean(s.power_w for s in load_ss)
        m.update(gpu_idle=t_idle, gpu_load=t_load, power_idle=p_idle, power_load=p_load,
                 clock_load=mean(s.clock_mhz for s in load_ss),
                 margin_min=min((s.margin_k for s in load_all if s.margin_k is not None),
                                default=None))
        if p_load and p_idle is not None and p_load - p_idle > 50:
            m["r_gpu"] = (t_load - t_idle) / (p_load - p_idle)
            checks.append(check("GPU temperature rise per watt",
                                f"{m['r_gpu']:.3f} K/W  ({t_load - t_idle:.1f} K for "
                                f"{p_load - p_idle:.0f} W)", "watch the trend", INFO,
                                "Rising over time points to drying paste or pads"))
        slope = slope_per_min([(s.t, s.gpu_c) for s in load_ss])
        if slope is not None:
            checks.append(check("GPU reached steady state", f"{slope:+.2f} K/min at the end",
                                "< 0.3 K/min", OK if abs(slope) < 0.3 else WARN))
        if m["margin_min"] is not None:
            checks.append(check("Headroom to thermal limit (T.Limit)",
                                f"{m['margin_min']} K at the tightest point",
                                ">= 3 K", OK if m["margin_min"] >= 3 else WARN,
                                "" if m["margin_min"] >= 3 else
                                "Test ran at the limit - throttling is imminent"))
        # T.Limit shrinks faster than (max temp - core temp): it apparently follows
        # a hotter internal sensor. The gap is the closest thing to the hidden hotspot.
        limit = run["device"].get("max_temp")
        offsets = [(limit - s.gpu_c) - s.margin_k for s in load_ss
                   if limit and s.gpu_c is not None and s.margin_k is not None]
        if offsets:
            m["internal_offset"] = statistics.median(offsets)
            checks.append(check("Internal sensor above core (experimental)",
                                f"{m['internal_offset']:+.0f} K under load", "watch the trend",
                                INFO, "Derived from T.Limit - a stand-in for the hidden hotspot"))


    # --- clock events under load ------------------------------------------ #
    flagged = [s.reasons for s in load_all if s.reasons is not None]
    if flagged:
        def share(bit: int) -> float:
            return sum(1 for r in flagged if r & bit) / len(flagged)
        thermal, brake = share(THERMAL_BITS), share(POWER_BRAKE_BIT)
        m["thermal_share"], m["brake_share"] = thermal, brake
        checks.append(check("Thermal throttling under load", f"{thermal * 100:.0f} % of samples",
                            "0 %", OK if thermal == 0 else WARN,
                            "" if thermal == 0 else "Cooling cannot hold the power limit"))
        checks.append(check("Power brake events", f"{brake * 100:.0f} % of samples", "0 %",
                            OK if brake == 0 else WARN,
                            "" if brake == 0 else "External power brake - check PSU and cables"))
        m["reasons_seen"] = sorted({name for bit, name in REASONS.items()
                                    for r in flagged if r & bit})

    worst = max((SEVERITY[c["status"]] for c in checks), default=0)
    run["metrics"], run["checks"] = m, checks
    run["verdict"] = INVALID if run["status"] == "invalid" else {0: OK, 1: WARN, 2: FAIL}[worst]


def fingerprint(run: dict) -> dict:
    cfg = run["config"]
    return {"gpu": run["device"]["gpu"], "power_limit_w": run["device"]["power_limit_w"],
            "fan_duty": cfg["fan_duty"], "gpu_fan_duty": cfg["gpu_fan_duty"],
            "load_minutes": cfg["load_minutes"]}


def add_trend(run: dict, history: list[dict]) -> None:
    comparable = [h for h in history if fingerprint(h) == fingerprint(run)
                  and h.get("status") == "completed"]
    run["trend"] = {"comparable_runs": len(comparable)}
    if not comparable or run["status"] != "completed":
        return
    base, m = comparable[0], run["metrics"]
    bm, since = base["metrics"], base["started"][:10]
    if m.get("r_gpu") and bm.get("r_gpu"):
        change = m["r_gpu"] / bm["r_gpu"] - 1
        run["checks"].append(check(
            "Trend: GPU temperature rise per watt", f"{change * 100:+.0f} %  "
            f"({bm['r_gpu']:.3f} -> {m['r_gpu']:.3f})", f"< +{GPU_R_TREND_WARN * 100:.0f} %",
            hc.grade(change, GPU_R_TREND_WARN, GPU_R_TREND_FAIL), f"Baseline from {since}"))
    if m.get("internal_offset") is not None and bm.get("internal_offset") is not None:
        delta = m["internal_offset"] - bm["internal_offset"]
        run["checks"].append(check(
            "Trend: internal sensor above core", f"{delta:+.0f} K vs. baseline",
            "< +3 K", WARN if delta >= 3 else OK, f"Baseline from {since}"))
    old, new = bm.get("gpu_fan_rpm") or [], m.get("gpu_fan_rpm") or []
    drops = [(i, n / o - 1) for i, (o, n) in enumerate(zip(old, new, strict=False)) if o and n]
    if drops:
        worst = min(d for _, d in drops)
        run["checks"].append(check(
            "Trend: GPU fan speed at same duty",
            " / ".join(f"fan {i}: {d * 100:+.0f} %" for i, d in drops),
            f"> {FAN_TREND_WARN * 100:.0f} %", WARN if worst < FAN_TREND_WARN else OK,
            f"Baseline from {since}"))
    if run["verdict"] != INVALID:
        worst = max(SEVERITY[c["status"]] for c in run["checks"])
        run["verdict"] = {0: OK, 1: WARN, 2: FAIL}[worst]


def verdict_text(run: dict) -> str:
    verdict = run["verdict"]
    if verdict == INVALID:
        return "Test invalid - no verdict on the GPU"
    if verdict == OK:
        return "Healthy - no signs of wear"
    bad = [c["name"] for c in run["checks"] if c["status"] == verdict]
    if verdict == FAIL and any(n.startswith("Compute errors") for n in bad):
        return "Compute errors - GPU or VRAM unstable"
    if verdict == FAIL and any(n.startswith("Safety") for n in bad):
        return "Stopped at a safety limit - no verdict on wear yet"
    if verdict == FAIL:
        return "Cooling degraded - check fans, dust and thermal paste"
    return "Watch it - some values are off"


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def print_report(run: dict) -> None:
    m = run["metrics"]
    print("\n" + "=" * 78)
    print(f" GPU health check  {run['device']['gpu']}  "
          f"({run['started'][:16].replace('T', ' ')})")
    print("=" * 78)
    hc.print_checks(run["checks"])
    print("-" * 78)
    print(f" GPU idle/load: {fmt(m.get('gpu_idle'), '.0f')} / {fmt(m.get('gpu_load'), '.0f')} C"
          f"    power idle/load: {fmt(m.get('power_idle'), '.0f')} / "
          f"{fmt(m.get('power_load'), '.0f')} W    clock: {fmt(m.get('clock_load'), '.0f')} MHz")
    print(f" Comparable earlier runs: {run['trend']['comparable_runs']}")
    print("-" * 78)
    print(" Verdict: " + hc.color(verdict_text(run), run["verdict"]))
    print("=" * 78)


def render_html(run: dict) -> str:
    esc, samples, m = html.escape, run["samples"], run["metrics"]

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

    fan_classes = ("pump", "power", "heat")
    fan_series = [{"name": f"fan {i}", "cls": fan_classes[i % 3],
                   "points": series(lambda s, i=i: s["fan_rpm"][i]
                                    if i < len(s["fan_rpm"]) else None)}
                  for i in range(len(m.get("gpu_fan_rpm") or []))]
    chart = hc.svg_chart
    dev, cfg = run["device"], run["config"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU health check {esc(run['started'][:10])}</title><style>{CSS}</style></head>
<body><main>
<header>{hc.badge(run)}<div><h1>GPU health check</h1>
<p class="sub">{esc(dev['gpu'])} on {esc(run['host'])}, {esc(run['started'][:16].replace('T', ' '))}</p>
<p class="verdict v-{run['verdict'].lower()}-text">{esc(verdict_text(run))}</p></div></header>
<h2>Checks</h2>
{hc.checks_table(run["checks"])}
<h2>GPU temperature</h2>
<div class="scroll">{chart([{"name": "GPU", "cls": "heat", "points": series(lambda s: s["gpu_c"])}],
                          y_unit="°C", bands=bands)}</div>
<h2>GPU power</h2>
<div class="scroll">{chart([{"name": "power", "cls": "power",
                             "points": series(lambda s: s["power_w"])}], y_unit="W", bands=bands,
                           ref_lines=[(dev['power_limit_w'], "power limit")])}</div>
<h2>GPU fan speed</h2>
<p class="sub">Fan 0 blue, fan 1 violet, fan 2 orange.</p>
<div class="scroll">{chart(fan_series, y_unit="rpm", bands=bands)}</div>
<h2>Test conditions</h2>
<dl>
  <dt>GPU fans / NZXT fans</dt><dd>{cfg['gpu_fan_duty']} % / {cfg['fan_duty']} %</dd>
  <dt>Load</dt><dd>gpu-burn, {cfg['load_minutes']} min</dd>
  <dt>Power limit</dt><dd>{dev['power_limit_w']:.0f} W</dd>
  <dt>Clock events seen</dt><dd>{esc(', '.join(m.get('reasons_seen', [])) or 'none')}</dd>
  <dt>Driver</dt><dd>{esc(dev['driver'])}</dd>
</dl></main></body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Wear-focused GPU health check (needs root).")
    p.add_argument("--gpu", type=int, default=0, help="NVML GPU index (default 0)")
    p.add_argument("--gpu-fan-duty", type=int, default=75,
                   help="fixed GPU fan duty; high enough to stay clear of T.Limit (default 75)")
    p.add_argument("--fan-duty", type=int, default=60, help="NZXT case fan duty")
    p.add_argument("--load-minutes", type=float, default=10)
    p.add_argument("--idle-min-minutes", type=float, default=2)
    p.add_argument("--idle-max-minutes", type=float, default=8)
    p.add_argument("--cooldown-minutes", type=float, default=3)
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--no-open", action="store_true", help="do not open the HTML report")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    hc.ensure_root()
    hc.install_signal_handlers()
    out_dir, uid, gid = hc.prepare_results_dir(args.out_dir)

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d-%H%M%S")
    print("> Preflight")
    binary, kernel = find_gpu_burn()
    gpu = NvidiaGpu(args.gpu)
    if not gpu.min_duty <= args.gpu_fan_duty <= gpu.max_duty:
        die(f"--gpu-fan-duty must be within {gpu.min_duty}-{gpu.max_duty} %")
    print(f"  GPU: {gpu.name}, {gpu.fan_count} fans, limit {gpu.power_limit_w:.0f} W, "
          f"max temp {gpu.max_temp} C  |  gpu-burn: {binary} ({kernel})")
    burn = GpuBurn(binary, kernel, args.gpu, out_dir / f"{FILE_PREFIX}-burn-{stamp}.log")

    run: dict = {
        "started": started.isoformat(timespec="seconds"), "host": platform.node(),
        "status": "completed", "error": None,
        "device": {"gpu": gpu.name, "driver": gpu.driver, "power_limit_w": gpu.power_limit_w,
                   "max_temp": gpu.max_temp},
        "config": {"fan_duty": args.fan_duty, "gpu_fan_duty": args.gpu_fan_duty,
                   "load_minutes": args.load_minutes},
        "fan_pin_problems": [],
    }

    print("> Pausing CoolerControl")
    with hc.CoolerControlPause():
        cooling = hc.NzxtCooling()
        rec = GpuRecorder(gpu, cooling)
        try:
            run_test(rec, gpu, cooling, burn, args, run)
        except SafetyAbort as exc:
            run["status"], run["error"] = "aborted", str(exc)
            print(f"\n!!! SAFETY ABORT: {exc}")
        except InvalidLoad as exc:
            run["status"], run["error"] = "invalid", str(exc)
            print(f"\n!!! TEST INVALID: {exc}")
        except KeyboardInterrupt:
            run["status"], run["error"] = "interrupted", "Interrupted by user"
        finally:
            burn.stop()
            gpu.close()                         # back to automatic GPU fan control
            cooling.close()
        print("> Restoring CoolerControl")

    run.setdefault("burn", burn.result() if burn.log_path.exists() else {})
    run["samples"] = [asdict(s) for s in rec.samples]
    analyze(run, rec.samples)
    add_trend(run, hc.load_history(out_dir, FILE_PREFIX))
    print_report(run)

    report = hc.save_results(out_dir, FILE_PREFIX, stamp, run, render_html(run), uid, gid,
                             extra=(burn.log_path,))
    if not args.no_open:
        hc.open_report(report)
    else:
        print(f" Report: {report}")

if __name__ == "__main__":
    main()
