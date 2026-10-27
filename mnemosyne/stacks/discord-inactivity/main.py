"""Entry point: load config, open the database, run the bot."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .bot import InactivityBot
from .config import load
from .db import Database, now_ts

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")
DB_PATH = os.environ.get("DB_PATH", "/data/activity.db")


def main() -> int:
    try:
        cfg = load(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - a bad config must stop the start
        print(f"config error in {CONFIG_PATH}:\n{exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=cfg.logging.level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    log = logging.getLogger("main")

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN is not set")
        return 2

    db = Database(DB_PATH)
    bot = InactivityBot(cfg, db)

    # Configure the daily scan from config before the loop starts.
    bot.scan_loop.change_interval(time=cfg.schedule.scan_time.replace(tzinfo=bot.tz))
    bot.liveness_loop.change_interval(seconds=cfg.schedule.liveness_interval_seconds)

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        def shutdown() -> None:
            # Best effort only: power loss and OOM kills never get here, which
            # is why downtime is derived from the liveness table instead.
            db.liveness_write(now_ts())
            asyncio.create_task(bot.close())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown)

        async with bot:
            await bot.start(token)

    try:
        asyncio.run(runner())
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
