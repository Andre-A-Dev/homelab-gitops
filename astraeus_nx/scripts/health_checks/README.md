# Hardware Health Checks – AstraeusNX

Tools to detect wear on AstraeusNX early: AIO cooling, GPU, RAM and drives. Each check establishes fixed test conditions, verifies that they actually hold, and compares the result with its own baseline. What matters is the trend over months. Absolute limits only serve as a rough safety net.

## Overview

| Script | Checks | Duration | Schedule |
|---|---|---|---|
| `disk_health.py` | SMART of all drives: defect counters, remaining life, wear forecast | seconds | weekly via systemd timer |
| `aio_health.py` | Pump, coolant heat dissipation, CPU cold plate contact, case fans | approx. 30 min | every 3–4 months |
| `gpu_health.py` | GPU cooling (paste/pads), fan bearings, throttling, compute errors | approx. 17 min | every 3–4 months |
| `ram_health.py` | Memory errors, EXPO still active, bandwidth, module temperature | approx. 31 min | every 3–4 months |
| `health_all.py` | All four in sequence with a summary page | approx. 80 min | quarterly appointment |
| `health_common.py` | Shared building blocks, not started directly | – | – |

## Prerequisites

All scripts need root and acquire `sudo` themselves when needed.

| Package | Source | Required for |
|---|---|---|
| `python` ≥ 3.10 | extra | all |
| `liquidctl` | extra | AIO, GPU (NZXT fans, pump) |
| `stress-ng` | extra | AIO (without it: weaker Python load, not comparable) |
| `python-nvidia-ml-py` | extra | GPU |
| `cuda`, `gpu-burn-git` | extra, AUR | GPU |
| `stressapptest` | AUR | RAM |
| `dmidecode` | extra | RAM (without it: no EXPO check) |
| `smartmontools` | extra | drives |
| CoolerControl | – | paused for the AIO and GPU tests and restarted afterwards |

```fish
sudo pacman -S liquidctl stress-ng python-nvidia-ml-py cuda dmidecode smartmontools
paru -S gpu-burn-git stressapptest
```

## Installation

All files live together in `homelab-infra/astraeus_nx/scripts/health_checks/`. The test scripts import `health_common.py` from the same folder.

```fish
chmod +x aio_health.py gpu_health.py ram_health.py disk_health.py health_all.py
git add --chmod=+x aio_health.py gpu_health.py ram_health.py disk_health.py health_all.py
```

### Weekly drive check

The units live under `homelab-infra/astraeus_nx/scripts/systemd/`. The path in `ExecStart` must point to this folder.

```fish
sudo cp ../systemd/disk-health.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now disk-health.timer
systemctl list-timers disk-health.timer
```

If a run finds a FAIL, the script exits with code 2 and the unit shows up in `systemctl --failed`.

## Usage

```fish
sudo ./health_all.py                     # everything, summary page at the end
sudo ./health_all.py --skip gpu          # leave out single checks (repeatable)

sudo ./aio_health.py                     # single checks
sudo ./gpu_health.py
sudo ./ram_health.py
sudo ./disk_health.py
```

Do not use the computer during the active tests. Other load skews the measurement, and the RAM test occupies about 44 GB.

### Options

All scripts accept `--out-dir PATH` (different results folder) and `--no-open` (do not open the report).

| Script | Option | Default | Purpose |
|---|---|---|---|
| `aio_health.py` | `--fan-duty` | 60 | fixed duty for all NZXT fans |
| | `--load-minutes` | 15 | duration of the CPU load |
| | `--idle-min-minutes` / `--idle-max-minutes` | 2 / 10 | bounds for settling at idle |
| | `--cooldown-minutes` | 5 | cool-down phase |
| | `--sweep-settle` / `--sweep-measure` | 15 / 10 s | pump sweep per step |
| | `--quick` | – | trial run with 5 min load, not comparable with full runs |
| | `--ambient` | – | room temperature entered by hand |
| | `--prom-url` + `--prom-query` | – | room temperature from Prometheus; the query must return exactly one series |
| `gpu_health.py` | `--gpu-fan-duty` | 75 | fixed duty for the GPU fans |
| | `--fan-duty` | 60 | fixed duty for the NZXT case fans |
| | `--load-minutes` | 10 | duration of gpu-burn |
| | `--gpu` | 0 | NVML index of the GPU |
| `ram_health.py` | `--minutes` | 30 | duration of stressapptest |
| `disk_health.py` | `--device PATH[:TYPE]` | – | additional drive, e.g. `/dev/sdd:sat` for USB enclosures |
| `health_all.py` | `--skip NAME` | – | leave out `disk`, `aio`, `gpu` or `ram` |

Example with room temperature from Netatmo:

```fish
sudo ./aio_health.py --prom-url http://mnemosyne.local:9090 \
    --prom-query 'netatmo_sensor_temperature_celsius{module="Office"}'
```

`health_all.py` does not pass this option on. The room temperature is therefore missing in the full session, but the verdict does not depend on it.

### Interrupting

Ctrl+C is safe at any time. The running test restores fans, pump and CoolerControl and saves its partial result. `health_all.py` waits for this, skips the remaining tests and shows the summary, provided at least one result exists.

Exit codes: 0 = OK, 2 = at least one FAIL, 130 = interrupted.

## Results

All checks write to `~/.local/share/hardware-health/`. The files belong to the calling user, even when the script runs under sudo or from the timer.

| Prefix | Raw data | Report | Log |
|---|---|---|---|
| `aio-` | `aio-run-*.json` | `aio-report-*.html` | `aio-stress-*.log` |
| `gpu-` | `gpu-run-*.json` | `gpu-report-*.html` | `gpu-burn-*.log` |
| `ram-` | `ram-run-*.json` | `ram-report-*.html` | `ram-stress-*.log` |
| `disk-` | `disk-run-*.json` | `disk-report-*.html` | – (raw SMART data is in the JSON) |
| `all-` | `all-run-*.json` | `all-report-*.html` | – |

A single test opens its report in the browser at the end. The browser starts as your user in your desktop session, never as root. `health_all.py` only opens the summary page.

### Baseline and archive

Each check compares against the **first** comparable run in the folder. Move trial runs, A/B tests (e.g. without the side panel) and skewed runs to `archive/`, otherwise they become the baseline or show up in the trend:

```fish
mv ~/.local/share/hardware-health/gpu-*-20261012-170005.* ~/.local/share/hardware-health/archive/
```

Runs only count as comparable if their conditions match:

| Check | Comparison key |
|---|---|
| AIO | Kraken model, fan duty, load program, load duration, CPU threads, power profile |
| GPU | GPU model, power limit, fan duty (GPU and case), load duration |
| RAM | Test size, duration, module part numbers |
| Drives | Model and serial number per drive (never the `/dev` path) |

If one of these values changes, for example through the X3D turbo mode or a different power profile, a new baseline starts automatically.

## What is checked

### AIO / CPU (`aio_health.py`)

Sequence: pause CoolerControl, set all NZXT fans to a fixed duty and read it back, pump sweep over 40/60/80/100 %, idle until the coolant temperature is stable, full load with stress-ng, cool down, then restart CoolerControl.

| Metric | Meaning | Limit |
|---|---|---|
| Pump at 100 % | bearing wear | ≥ 2,500 rpm (spec 2,800 ± 300) |
| Pump stability | air in the loop, bearing | < 2 % fluctuation |
| Short speed dips | single outliers to ~67 % | info, warning from 2 % of samples |
| Coolant rise per watt | radiator heat dissipation | warning from 0.09 K/W, trend warning from +15 %, failure from +30 % |
| CPU to coolant per watt | thermal paste, mounting pressure | info, watch the trend |
| Same fan duty | one channel noticeably slower | < 70 % of the median gives a warning |
| Fans set | write command actually arrived | otherwise warning, values not comparable |

Safety abort at coolant ≥ 55 °C or if the pump drops below 300 rpm at 100 %. If the CPU load produces less than 90 % utilization, the test counts as invalid.

### GPU (`gpu_health.py`)

Sequence: pause CoolerControl, set case and GPU fans to a fixed duty and read it back, idle, 10 min gpu-burn, cool down, then set the GPU fans back to automatic and restart CoolerControl.

| Metric | Meaning | Limit |
|---|---|---|
| Temperature rise per watt | paste and pads (substitute for the missing hotspot) | trend warning from +15 %, failure from +30 % |
| Internal sensor above core (experimental) | hotspot substitute derived from T.Limit | trend warning from +3 K |
| Fan speed at fixed duty | bearing wear | warning at < 85 % of the others or −10 % against the baseline |
| Throttling due to temperature | cooling does not sustain the power limit | 0 % of samples |
| Power brake events | power supply or cable | 0 % of samples |
| Compute errors (gpu-burn) | GPU or VRAM stability | 0, otherwise FAIL |

The RTX 50 series does not report a hotspot temperature via NVML. T.Limit, however, apparently follows a hotter internal sensor; the experimental value is calculated from it. Safety abort when the core reaches 88 °C. If gpu-burn draws less than 60 % of the power limit after 20 seconds, the test counts as invalid.

### RAM (`ram_health.py`)

Sequence: record the module speed via dmidecode, run stressapptest over 75 % of the installed RAM in 4 GB steps (44 GB with 64 GB installed), record the DDR5 module temperatures meanwhile, then search the kernel log for hardware errors. CoolerControl stays active.

| Metric | Meaning | Limit |
|---|---|---|
| Memory errors | unstable memory or memory controller | 0, otherwise FAIL |
| Kernel hardware errors | machine check or EDAC messages | 0, otherwise FAIL |
| Configured speed | EXPO lost after a BIOS update? | warning on change since the last run |
| Copy bandwidth | settings lost, performance drop | warning from −10 % against the baseline |
| Module temperature | airflow over the modules | warning from 85 °C |

After BIOS or EXPO changes, memtest86+ remains the more thorough test, because it checks the complete memory without a running operating system.

### Drives (`disk_health.py`)

Passive, without load and without self-tests. Drives are identified by model and serial number.

| Metric | Limit |
|---|---|
| Overall SMART status, NVMe warnings, spare blocks | FAIL on a problem |
| Reallocated, pending and uncorrectable sectors, NVMe media errors | any increase is a FAIL |
| Samsung: spare blocks, write, erase and runtime errors, newly added bad flash blocks | baseline, an increase gives a warning |
| Interface CRC errors | an increase gives a warning (usually cable or USB enclosure) |
| Remaining life | warning below 20 %, failure below 5 % |
| Wear forecast | from 30 days between two runs, warning below 1 year |
| Temperature | warning from 70 °C (SSD) or 55 °C (HDD) |
| Unsafe shutdowns | info with share of power cycles and increase |

Drives that were present last time and are now missing are only noted, not counted as errors.

## Baselines (as of 2026-10-12)

| Check | Values |
|---|---|
| AIO | 0.074 K/W coolant, pump 2,753 rpm, all fans 60 %, power profile `performance` |
| GPU | 0.153 K/W, internal sensor +4 K, fans 2,289 / 2,302 / 2,301 rpm at 75 % |
| RAM | 0 errors, 69,707 MB/s, modules max. 70.5 °C, 6400 MT/s |
| Drives | all ≥ 98 % remaining life, 870 EVO with 1 and 5 bad flash blocks respectively as baseline, approx. 20 % unsafe shutdowns |

## Known pitfalls

- **Fans not actually set:** Writing all channels in one go via `sync` left one channel at its old value. The scripts therefore write each channel individually and read it back. If a pinning warning remains anyway, the run is not comparable.
- **Fan mapping:** Each NZXT controller has only three fan channels. Several fans hang on splitters, and only one of them reports its speed. Channels with 0 rpm are unused or have no tach output.
- **Turbo mode and power profile:** The X3D turbo mode halves the core count, `power-saver` reduces the load. Both change the measurements and therefore start a new baseline.
- **USB drives:** USB enclosures often need `--device /dev/sdX:sat`.
- **Report does not open:** Without a detectable Wayland or X session, only the path is printed. Then run `xdg-open <path>` as the normal user.
- **gpu-burn does not start:** The log `gpu-burn-*.log` names the cause. The script aborts after 20 s as invalid.

## Structure

`health_common.py` contains everything that more than one check needs:

- **Verdicts:** constants (`OK`, `WARN`, `FAIL`, `INFO`, `INVALID`) and `check()`
- **Measurement helpers:** sliding windows and slope over time
- **System:** root and sudo handling, results folder, loading the history, saving, opening the report
- **Hardware:** `CoolerControlPause` and `NzxtCooling` (pump and fans via liquidctl, with read-back)
- **Output:** console table, HTML table, SVG charts, check badge, CSS

Each check is a standalone script with sequence, evaluation, report and `verdict_text()`. `health_all.py` starts the checks as separate processes and builds the summary from their JSON results.
