"""SQLite storage: filament spools and the action log.

One connection per request (Flask g), WAL mode so the UI can read while an
action is being logged. The schema is created idempotently on startup —
no migration framework until there is a second schema version to migrate to.
"""

import os
import sqlite3
import time

from flask import g

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS spools (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material        TEXT    NOT NULL,
    color           TEXT    NOT NULL,
    manufacturer    TEXT,
    diameter_mm     REAL    NOT NULL DEFAULT 1.75,
    total_weight_g  REAL    NOT NULL,
    remaining_g     REAL    NOT NULL,
    spool_weight_g  REAL,
    cost_eur        REAL,
    purchased_at    TEXT,
    is_active       INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- Every write to the physical world is recorded, including refused ones.
CREATE TABLE IF NOT EXISTS actions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    action  TEXT    NOT NULL,
    result  TEXT    NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _check_writable() -> None:
    """Explain a permission problem instead of raising a sqlite3 stack trace.

    The bind-mounted data directory is created by Docker as root when it does
    not exist, while the container runs as PUID:PGID. sqlite3 then only says
    "unable to open database file", which does not point at the actual fix.
    """
    directory = os.path.dirname(config.DB_PATH) or "."
    if not os.path.isdir(directory):
        raise SystemExit(
            f"Data directory {directory} does not exist. "
            "Create it on the host before starting: mkdir -p data"
        )
    if not os.access(directory, os.W_OK | os.X_OK):
        raise SystemExit(
            f"Data directory {directory} is not writable by uid {os.getuid()}:{os.getgid()}. "
            "Fix on the host: sudo chown $(id -u):$(id -g) data "
            "(and check PUID/PGID in .env)."
        )


def init_db() -> None:
    _check_writable()
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def log_action(action: str, result: str, detail: str = "") -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO actions (ts, action, result, detail) VALUES (?, ?, ?, ?)",
        (int(time.time()), action, result, detail),
    )
    conn.commit()
