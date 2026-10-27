"""Prometheus metrics — Galatea's own control state only.

Printer telemetry, plug power and climate are already exported by
prusa-exporter and shelly-exporter. Exporting them again here would create a
second source of truth for the same numbers. Galatea reports only what nobody
else can: what it was asked to do and what it refused.
"""

from prometheus_client import Counter

ACTIONS = Counter(
    "galatea_actions_total",
    "Control actions requested via Galatea",
    ["action", "result"],
)

INTERLOCK_BLOCKS = Counter(
    "galatea_interlock_blocks_total",
    "Power-off requests refused by the interlock",
    ["reason"],
)

UPSTREAM_ERRORS = Counter(
    "galatea_upstream_errors_total",
    "Failed calls to upstream devices",
    ["upstream"],
)
