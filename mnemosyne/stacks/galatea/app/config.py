"""Environment-based configuration (12-factor).

Everything comes from the environment so the same image runs unchanged under
Docker Compose now and k3s later. Required values fail fast at import time:
a control plane that starts half-configured is worse than one that refuses to
start.
"""

import os


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


# --- Upstreams ---------------------------------------------------------------
PRUSA_HOST = _required("PRUSA_HOST")
PRUSA_USERNAME = os.environ.get("PRUSA_USERNAME", "maker")
PRUSA_PASSWORD = _required("PRUSA_PASSWORD")

PLUG_HOST = _required("PLUG_HOST")

CLIMATE_GATEWAY_HOST = _required("CLIMATE_GATEWAY_HOST")
CLIMATE_SENSOR_MAC = _required("CLIMATE_SENSOR_MAC")
# The sensor broadcasts once per minute; ten missed packets means it is gone.
CLIMATE_STALE_AFTER_S = int(os.environ.get("CLIMATE_STALE_AFTER_S", "600"))

HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "5"))
# Short cache for UI polling only. Safety decisions never use the cache.
STATUS_CACHE_TTL_S = float(os.environ.get("STATUS_CACHE_TTL_S", "2"))

# --- Interlock ---------------------------------------------------------------
# Cutting power stops the heatbreak fan. With a hot nozzle, heat creeps up the
# heatbreak and can soften filament there -> clog. Wait until it has cooled.
INTERLOCK_NOZZLE_MAX_C = float(os.environ.get("INTERLOCK_NOZZLE_MAX_C", "50"))

# --- Auth --------------------------------------------------------------------
# Injected by Caddy as X-Galatea-Token after basic auth succeeded. Protects the
# API against anything on the shared Docker networks that bypasses Caddy.
API_TOKEN = _required("GALATEA_API_TOKEN")

# --- Storage -----------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "/app/data/galatea.db")
