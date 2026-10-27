#!/usr/bin/env python3
"""
probe_api_2.py

Second targeted probe of the Bundesagentur fuer Arbeit Jobsuche v6 API.
Fixes the wo=Nuernberg issue (API needs umlaut or koordinaten) and retests
all parameter enums with a working baseline, plus fills remaining gaps:
- arbeitszeit enum values (with valid wo)
- angebotsart enum values (with valid wo)
- arbeitgeber param (with non-empty was)
- berufsfeld param (with valid wo)
- veroeffentlichtseit range (with valid wo)
- pagination behavior (with real results)
- facetten field (with real results)
- size limits
- pav flag effect

Run in PyCharm directly - output goes to stdout and api_probe_results_2.txt
"""

import json
import sys
import time

import requests


# --------------------------------------------------------------------------- #
# Tee stdout + stderr to file
# --------------------------------------------------------------------------- #

class _Tee:
    def __init__(self, *files):
        self.files = files

    def write(self, data):
        for f in self.files:
            f.write(data)
            f.flush()

    def flush(self):
        for f in self.files:
            f.flush()


_log_file = open("api_probe_results_2.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_file)
sys.stderr = _Tee(sys.__stderr__, _log_file)


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
API_KEY = "jobboerse-jobsuche"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"X-API-Key": API_KEY, "User-Agent": USER_AGENT}

# Confirmed working baselines
BASE_WO = {
    "was": "Software Engineer",
    "wo": "Nürnberg",          # umlaut version - confirmed via koordinaten probe
    "umkreis": 50,
    "page": 1,
    "size": 3,
    "pav": "false",
}

BASE_COORDS = {
    "was": "Software Engineer",
    "koordinaten": "49.4521,11.0767",
    "umkreis": 50,
    "page": 1,
    "size": 3,
    "pav": "false",
}

DELAY = 1.5


def get(params: dict, label: str) -> dict | None:
    print(f"\n{'='*60}")
    print(f"PROBE: {label}")
    print(f"Params: {json.dumps(params, ensure_ascii=False)}")
    try:
        resp = requests.get(API_BASE, headers=HEADERS, params=params, timeout=20)
        print(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"  body: {resp.text[:300]}")
            return None
        return resp.json()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return None
    finally:
        time.sleep(DELAY)


def print_summary(data: dict | None, extra_keys: list[str] | None = None) -> None:
    if not data:
        return
    count = data.get("maxErgebnisse", "?")
    returned = len(data.get("ergebnisliste", []))
    wo_out = data.get("woOutput", {})
    suchmodus = wo_out.get("suchmodus", "?") if isinstance(wo_out, dict) else wo_out
    print(f"  maxErgebnisse={count}, returned={returned}, woOutput.suchmodus={suchmodus!r}")
    if extra_keys:
        for key in extra_keys:
            print(f"  {key}: {data.get(key, 'missing')}")


def print_first_item(data: dict | None) -> None:
    items = (data or {}).get("ergebnisliste", [])
    if items:
        print(f"\n  First item:")
        print(json.dumps(dict(items[0]), indent=4, ensure_ascii=False))
    else:
        print("  No items returned.")


def section(title: str) -> None:
    print(f"\n{'#'*60}")
    print(f"# {title}")
    print(f"{'#'*60}")


# --------------------------------------------------------------------------- #
# 1. Baseline validation
# --------------------------------------------------------------------------- #

section("1. BASELINE - wo with umlaut vs koordinaten")

d = get(BASE_WO, "wo=Nürnberg (umlaut)")
print_summary(d)

d = get(BASE_COORDS, "koordinaten=49.4521,11.0767")
print_summary(d)

# Also try without umkreis to see if it's required
d = get({"was": "Software Engineer", "wo": "Nürnberg", "page": 1, "size": 3, "pav": "false"},
        "wo=Nürnberg without umkreis")
print_summary(d)

# Try common city name variants
for wo_val in ("München", "Berlin", "Stuttgart", "Erlangen"):
    d = get({**BASE_WO, "wo": wo_val, "size": 1}, f"wo={wo_val}")
    if d:
        print(f"  {wo_val}: maxErgebnisse={d.get('maxErgebnisse', '?')}, "
              f"suchmodus={d.get('woOutput', {}).get('suchmodus', '?')!r}")


# --------------------------------------------------------------------------- #
# 2. arbeitszeit enum (with valid wo)
# --------------------------------------------------------------------------- #

section("2. ARBEITSZEIT ENUM - with valid wo=Nürnberg")

arbeitszeit_values = [
    ("vz",    "Vollzeit"),
    ("tz",    "Teilzeit"),
    ("snw",   "Schicht / Nacht / Wochenende"),
    ("ho",    "Home Office"),
    ("mj",    "Minijob (hypothesis)"),
    ("vz;tz", "Vollzeit + Teilzeit"),
    ("vz;ho", "Vollzeit + Home Office"),
    ("tz;ho", "Teilzeit + Home Office"),
]

for value, label in arbeitszeit_values:
    d = get({**BASE_WO, "arbeitszeit": value, "size": 1}, f"arbeitszeit={value} ({label})")
    if d:
        items = d.get("ergebnisliste", [])
        # Check which arbeitszeit booleans are set on the first item
        if items:
            az_fields = {k: v for k, v in items[0].items() if "arbeitszeit" in k.lower()}
            print(f"  -> maxErgebnisse={d.get('maxErgebnisse', '?')}, first item az-fields: {az_fields}")
        else:
            print(f"  -> maxErgebnisse={d.get('maxErgebnisse', '?')}, no items")


# --------------------------------------------------------------------------- #
# 3. angebotsart enum (with valid wo)
# --------------------------------------------------------------------------- #

section("3. ANGEBOTSART ENUM - with valid wo=Nürnberg")

# No angebotsart filter first (to get baseline stellenangebotsart values)
d = get({**BASE_WO, "size": 5}, "no angebotsart filter (baseline)")
if d:
    items = d.get("ergebnisliste", [])
    arts = [item.get("stellenangebotsart", "?") for item in items]
    print(f"  stellenangebotsart values without filter: {arts}")

for value, label in [
    (1,  "Arbeit (hypothesis)"),
    (2,  "Ausbildung (hypothesis)"),
    (4,  "Praktikum/Trainee (hypothesis)"),
    (34, "Selbständigkeit? (hypothesis)"),
    (16, "unknown"),
    (32, "unknown"),
    (64, "unknown"),
]:
    d = get({**BASE_WO, "angebotsart": value, "size": 3}, f"angebotsart={value} ({label})")
    if d:
        items = d.get("ergebnisliste", [])
        arts = [item.get("stellenangebotsart", "?") for item in items]
        print(f"  -> maxErgebnisse={d.get('maxErgebnisse', '?')}, stellenangebotsart values: {arts}")


# --------------------------------------------------------------------------- #
# 4. pav flag (with valid wo)
# --------------------------------------------------------------------------- #

section("4. PAV FLAG - with valid wo=Nürnberg")

for value in ("false", "true"):
    d = get({**BASE_WO, "pav": value, "size": 1}, f"pav={value}")
    if d:
        items = d.get("ergebnisliste", [])
        print(f"  pav={value}: maxErgebnisse={d.get('maxErgebnisse', '?')}")
        # Check if result items have a pav-related field
        if items:
            pav_fields = {k: v for k, v in items[0].items()
                          if any(x in k.lower() for x in ("pav", "privat", "vermittl"))}
            if pav_fields:
                print(f"    pav-related fields in item: {pav_fields}")


# --------------------------------------------------------------------------- #
# 5. Pagination (with real results)
# --------------------------------------------------------------------------- #

section("5. PAGINATION - with real results")

d = get({**BASE_WO, "size": 50, "page": 1}, "page=1 size=50")
if d:
    max_results = d.get("maxErgebnisse", 0)
    print(f"  maxErgebnisse: {max_results}")
    print_summary(d)

    last_page = max(1, -(-max_results // 50))
    print(f"  calculated last page: {last_page}")

    d2 = get({**BASE_WO, "size": 50, "page": last_page}, f"page={last_page} (last)")
    print_summary(d2)

    d3 = get({**BASE_WO, "size": 50, "page": last_page + 1}, f"page={last_page+1} (beyond)")
    print_summary(d3)

# Size limits
for size in (50, 100, 200, 500):
    d = get({**BASE_WO, "size": size, "page": 1}, f"size={size}")
    if d:
        print(f"  size={size}: maxErgebnisse={d.get('maxErgebnisse', '?')}, "
              f"returned={len(d.get('ergebnisliste', []))}")


# --------------------------------------------------------------------------- #
# 6. Facetten (with real results)
# --------------------------------------------------------------------------- #

section("6. FACETTEN - with real results")

d = get({**BASE_WO, "size": 1}, "facetten with real results")
if d:
    if "facetten" in d:
        print(f"  facetten present:")
        print(json.dumps(d["facetten"], indent=2, ensure_ascii=False)[:3000])
    else:
        print("  facetten: NOT present in response")
    print(f"\n  All top-level keys: {list(d.keys())}")


# --------------------------------------------------------------------------- #
# 7. arbeitgeber + berufsfeld params
# --------------------------------------------------------------------------- #

section("7. ARBEITGEBER + BERUFSFELD PARAMS")

# arbeitgeber with non-empty was
for employer in ("Siemens", "BMW", "Bosch", "Continental"):
    d = get({**BASE_WO, "arbeitgeber": employer, "umkreis": 200, "size": 3},
            f"arbeitgeber={employer} (with was)")
    if d:
        items = d.get("ergebnisliste", [])
        firmen = [item.get("firma", "?") for item in items]
        print(f"  arbeitgeber={employer}: maxErgebnisse={d.get('maxErgebnisse', '?')}, "
              f"firmen={firmen}")

# berufsfeld with valid wo
for berufsfeld in ("Informatik", "Elektrotechnik", "Maschinenbau"):
    d = get({**BASE_WO, "berufsfeld": berufsfeld, "size": 1}, f"berufsfeld={berufsfeld}")
    if d:
        print(f"  berufsfeld={berufsfeld!r}: maxErgebnisse={d.get('maxErgebnisse', '?')}")


# --------------------------------------------------------------------------- #
# 8. veroeffentlichtseit range (with valid wo)
# --------------------------------------------------------------------------- #

section("8. VEROEFFENTLICHTSEIT RANGE")

for days in (0, 1, 3, 7, 14, 30, 60, 100):
    d = get({**BASE_WO, "veroeffentlichtseit": days, "size": 1},
            f"veroeffentlichtseit={days}")
    if d:
        print(f"  veroeffentlichtseit={days:>3}: maxErgebnisse={d.get('maxErgebnisse', '?')}")


# --------------------------------------------------------------------------- #
# 9. woOutput full structure
# --------------------------------------------------------------------------- #

section("9. WOOUTPUT - full structure for different wo variants")

for wo_val, label in [
    ("Nürnberg", "city name with umlaut"),
    ("90402",    "PLZ"),
    ("Bayern",   "Bundesland"),
]:
    d = get({**BASE_WO, "wo": wo_val, "size": 1}, f"wo={wo_val!r} ({label})")
    if d:
        print(f"  woOutput: {json.dumps(d.get('woOutput', 'missing'), ensure_ascii=False)}")

# koordinaten woOutput
d = get({**BASE_COORDS, "size": 1}, "koordinaten woOutput")
if d:
    print(f"  woOutput: {json.dumps(d.get('woOutput', 'missing'), ensure_ascii=False)}")


# --------------------------------------------------------------------------- #
# 10. Response item - full field inventory
# --------------------------------------------------------------------------- #

section("10. FULL ITEM FIELD INVENTORY")

d = get({**BASE_WO, "size": 3}, "full item field inventory")
if d:
    items = d.get("ergebnisliste", [])
    if items:
        # Collect all unique keys across all returned items
        all_keys = set()
        for item in items:
            all_keys.update(item.keys())
        print(f"\n  All observed field names across {len(items)} items:")
        for key in sorted(all_keys):
            values = [item.get(key, "—") for item in items]
            print(f"    {key}: {values}")


print("\n\nDone.")