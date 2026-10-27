"""Shelly Gen3 client — local RPC, no cloud.

Plug:    Switch.GetStatus / Switch.Set on component id 0 (Plug M Gen3).
Climate: a BLU H&T sensor, relayed by the gateway script (blu_gateway.js) into
         the gateway's KVS under the sensor's MAC. Same read path as the
         shelly-exporter, so both consumers see identical data.
"""

import json
import time

import requests

from app import config


class UpstreamError(Exception):
    """The Shelly device could not be reached or returned something unusable."""


def _rpc_get(host: str, method: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(f"http://{host}/rpc/{method}", params=params, timeout=config.HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise UpstreamError(f"{method} on {host} failed: {exc}") from exc


# --- Plug --------------------------------------------------------------------

def get_plug() -> dict:
    data = _rpc_get(config.PLUG_HOST, "Switch.GetStatus", {"id": 0})
    return {
        "on": bool(data.get("output")),
        "power_w": data.get("apower"),
        "voltage_v": data.get("voltage"),
        "current_a": data.get("current"),
        "energy_kwh": (data.get("aenergy") or {}).get("total", 0) / 1000,
    }


def set_plug(on: bool) -> None:
    _rpc_get(config.PLUG_HOST, "Switch.Set", {"id": 0, "on": "true" if on else "false"})


# --- Climate -----------------------------------------------------------------

def get_climate() -> dict:
    """Last reading cached by the gateway, with an explicit staleness verdict.

    The gateway serves its last value forever — a dead coin cell looks exactly
    like perfectly stable air. The timestamp is the only honest signal.
    """
    raw = _rpc_get(
        config.CLIMATE_GATEWAY_HOST, "KVS.Get", {"key": config.CLIMATE_SENSOR_MAC}
    )
    try:
        data = json.loads(raw.get("value") or "{}")
    except ValueError as exc:
        raise UpstreamError(f"KVS value is not JSON: {exc}") from exc

    ts = data.get("ts")
    age_s = int(time.time() - ts) if ts else None
    return {
        "temperature_c": data.get("temperature"),
        "humidity_pct": data.get("humidity"),
        "battery_pct": data.get("battery"),
        "rssi_dbm": data.get("rssi"),
        "age_s": age_s,
        "stale": age_s is None or age_s > config.CLIMATE_STALE_AFTER_S,
    }
