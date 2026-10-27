"""PrusaLink client (local API, HTTP digest auth).

Endpoints verified against Prusa's OpenAPI spec (Prusa-Link-Web/spec/openapi.yaml):
    GET    /api/v1/status              printer + optional job telemetry
    PUT    /api/v1/job/{id}/pause      204 on success, 409 on wrong state
    PUT    /api/v1/job/{id}/resume     204 on success, 409 on wrong state
    DELETE /api/v1/job/{id}            stop, 204 on success, 409 on wrong state

Known firmware quirk (Prusa-Firmware-Buddy #5488): while the printer shows a
dialog, status reports ATTENTION but resume answers 409 without a body. The
printer needs a decision at its display, which the API cannot make.
"""

import threading
import time
from dataclasses import asdict, dataclass

import requests
from requests.auth import HTTPDigestAuth

from app import config

_BASE = f"http://{config.PRUSA_HOST}/api/v1"
_AUTH = HTTPDigestAuth(config.PRUSA_USERNAME, config.PRUSA_PASSWORD)


class UpstreamError(Exception):
    """PrusaLink could not be reached or answered with an unexpected status."""


@dataclass
class PrinterSnapshot:
    reachable: bool
    state: str | None = None
    nozzle_c: float | None = None
    nozzle_target_c: float | None = None
    bed_c: float | None = None
    bed_target_c: float | None = None
    job_id: int | None = None
    progress: float | None = None
    time_remaining_s: int | None = None
    time_printing_s: int | None = None
    fetched_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


_cache_lock = threading.Lock()
_cache: PrinterSnapshot | None = None


def _fetch() -> PrinterSnapshot:
    now = time.time()
    try:
        resp = requests.get(f"{_BASE}/status", auth=_AUTH, timeout=config.HTTP_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return PrinterSnapshot(reachable=False, fetched_at=now)

    printer = data.get("printer") or {}
    job = data.get("job") or {}
    state = printer.get("state")
    return PrinterSnapshot(
        reachable=True,
        state=str(state).upper() if state else None,
        nozzle_c=printer.get("temp_nozzle"),
        nozzle_target_c=printer.get("target_nozzle"),
        bed_c=printer.get("temp_bed"),
        bed_target_c=printer.get("target_bed"),
        job_id=job.get("id"),
        progress=job.get("progress"),
        time_remaining_s=job.get("time_remaining"),
        time_printing_s=job.get("time_printing"),
        fetched_at=now,
    )


def get_status(fresh: bool = False) -> PrinterSnapshot:
    """Return printer status. fresh=True bypasses the cache — mandatory for
    anything that decides whether a write is safe."""
    global _cache
    with _cache_lock:
        if (
            not fresh
            and _cache is not None
            and time.time() - _cache.fetched_at < config.STATUS_CACHE_TTL_S
        ):
            return _cache
        _cache = _fetch()
        return _cache


def _job_command(method: str, path: str) -> int:
    """Send a job command and return the HTTP status code.

    204 = done, 409 = printer in a state that does not allow it, 404 = job gone.
    Anything else is treated as an upstream failure.
    """
    try:
        resp = requests.request(method, f"{_BASE}{path}", auth=_AUTH, timeout=config.HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise UpstreamError(f"PrusaLink unreachable: {exc}") from exc
    if resp.status_code not in (204, 404, 409):
        raise UpstreamError(f"PrusaLink answered {resp.status_code} for {method} {path}")
    # A command changes state; the next UI poll must not show the old one.
    invalidate_cache()
    return resp.status_code


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def pause(job_id: int) -> int:
    return _job_command("PUT", f"/job/{job_id}/pause")


def resume(job_id: int) -> int:
    return _job_command("PUT", f"/job/{job_id}/resume")


def stop(job_id: int) -> int:
    return _job_command("DELETE", f"/job/{job_id}")
