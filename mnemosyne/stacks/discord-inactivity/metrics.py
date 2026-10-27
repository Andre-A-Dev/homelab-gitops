"""Prometheus metrics.

Bot health never travels through Discord: the state you most need to know
about is the one the bot can no longer report. Alerting goes through
Prometheus -> Alertmanager -> ntfy.
"""

from __future__ import annotations

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

UP = Gauge("discord_up", "Process is running")
GATEWAY_CONNECTED = Gauge("discord_gateway_connected", "Gateway session is established")
LAST_EVENT_TS = Gauge(
    "discord_last_event_timestamp_seconds", "Unix time of the last counted activity event"
)
LAST_SCAN_TS = Gauge(
    "discord_last_scan_timestamp_seconds", "Unix time of the last completed daily scan"
)
MEMBERS_TOTAL = Gauge("discord_guild_members_total", "Members currently in the guild")
MEMBERS_BY_STAGE = Gauge(
    "discord_members_by_stage", "Members per inactivity stage", ["stage"]
)
LOWEST_SCORE = Gauge(
    "discord_lowest_activity_score", "Lowest decayed activity score among non-exempt members"
)
HEARTBEATS = Counter("discord_heartbeat_responses_total", "Heartbeat buttons clicked")
KICKS = Counter("discord_kicks_total", "Members kicked for inactivity")
DMS_FAILED = Counter(
    "discord_dms_failed_total", "Direct messages that could not be delivered"
)
DOWNTIME_SECONDS = Gauge(
    "discord_downtime_seconds_total", "Accumulated gateway downtime, excluded from inactivity"
)
UNREADABLE_CHANNELS = Gauge(
    "discord_unreadable_channels", "Channels the bot cannot read - activity blind spots"
)


async def _handler(_request: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST.split(";")[0])


async def start_server(host: str, port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/metrics", _handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    UP.set(1)
    return runner
