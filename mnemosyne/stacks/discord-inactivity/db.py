"""SQLite persistence.

Only user IDs, timestamps and counters. Message content is never stored.

Calls are short and the guild has ~35 members, so plain sqlite3 behind a lock
is fine; an async driver would be complexity without benefit here.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS activity (
    guild_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,

    first_joined_at   INTEGER,
    joined_at         INTEGER,

    last_message_at   INTEGER,
    last_voice_at     INTEGER,
    last_reaction_at  INTEGER,
    last_heartbeat_at INTEGER,

    score             REAL    DEFAULT 0,
    score_updated_at  INTEGER,

    rejoin_count      INTEGER DEFAULT 0,
    kick_count        INTEGER DEFAULT 0,
    snooze_until      INTEGER,
    snooze_count      INTEGER DEFAULT 0,

    exempt            INTEGER DEFAULT 0,
    stage             INTEGER DEFAULT 0,
    notified_stage    INTEGER DEFAULT 0,
    left_at           INTEGER,

    PRIMARY KEY (guild_id, user_id)
);

-- Written every liveness_interval_seconds. The gap between the last write and
-- startup IS the downtime, no matter how the process died.
CREATE TABLE IF NOT EXISTS liveness (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS downtime (
    started_at INTEGER NOT NULL,
    ended_at   INTEGER
);

CREATE TABLE IF NOT EXISTS voice_session (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTIVITY_COLUMNS = {
    "message": "last_message_at",
    "voice_minute": "last_voice_at",
    "reaction": "last_reaction_at",
    "heartbeat": "last_heartbeat_at",
    "rejoin": "joined_at",
}


@dataclass(slots=True)
class Member:
    guild_id: int
    user_id: int
    first_joined_at: int | None
    joined_at: int | None
    last_message_at: int | None
    last_voice_at: int | None
    last_reaction_at: int | None
    last_heartbeat_at: int | None
    score: float
    score_updated_at: int | None
    rejoin_count: int
    kick_count: int
    snooze_until: int | None
    snooze_count: int
    exempt: int
    stage: int
    notified_stage: int
    left_at: int | None

    @property
    def last_activity_at(self) -> int:
        """Newest signal we have. joined_at is the baseline for quiet newcomers."""
        return max(
            value
            for value in (
                self.joined_at or 0,
                self.last_message_at or 0,
                self.last_voice_at or 0,
                self.last_reaction_at or 0,
                self.last_heartbeat_at or 0,
            )
        )


def now_ts() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ meta

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # -------------------------------------------------------------- liveness

    def liveness_read(self) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT last_seen_at FROM liveness WHERE id = 1").fetchone()
        return row["last_seen_at"] if row else None

    def liveness_write(self, ts: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO liveness (id, last_seen_at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (ts,),
            )
            self._conn.commit()

    def downtime_add(self, started_at: int, ended_at: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO downtime (started_at, ended_at) VALUES (?, ?)",
                (started_at, ended_at),
            )
            self._conn.commit()

    def downtime_seconds_between(self, start: int, end: int) -> int:
        """Total downtime overlapping [start, end]. Open windows count until `end`."""
        if end <= start:
            return 0
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(
                    MIN(COALESCE(ended_at, :end), :end) - MAX(started_at, :start)
                ), 0) AS seconds
                FROM downtime
                WHERE COALESCE(ended_at, :end) > :start AND started_at < :end
                """,
                {"start": start, "end": end},
            ).fetchone()
        return int(row["seconds"] or 0)

    def downtime_total(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(COALESCE(ended_at, started_at) - started_at), 0) AS s "
                "FROM downtime"
            ).fetchone()
        return int(row["s"] or 0)

    # --------------------------------------------------------------- members

    def upsert_member(self, guild_id: int, user_id: int, joined_at: int) -> None:
        """Create the row if missing. Never overwrites an existing clock."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activity (guild_id, user_id, first_joined_at, joined_at,
                                      score, score_updated_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET left_at = NULL
                """,
                (guild_id, user_id, joined_at, joined_at, now_ts()),
            )
            self._conn.commit()

    def get(self, guild_id: int, user_id: int) -> Member | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM activity WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return Member(**dict(row)) if row else None

    def all_present(self, guild_id: int) -> list[Member]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activity WHERE guild_id = ? AND left_at IS NULL",
                (guild_id,),
            ).fetchall()
        return [Member(**dict(row)) for row in rows]

    def touch(
        self,
        guild_id: int,
        user_id: int,
        kind: str,
        ts: int,
        new_score: float,
    ) -> None:
        """Record one activity event and drop the member back to stage 0.

        Any counted event resets the stage immediately - no grace, no half way
        back. Whoever is active is active.
        """
        column = ACTIVITY_COLUMNS[kind]
        with self._lock:
            self._conn.execute(
                f"""
                UPDATE activity
                   SET {column} = MAX(COALESCE({column}, 0), ?),
                       score = ?,
                       score_updated_at = ?,
                       stage = 0,
                       notified_stage = 0
                 WHERE guild_id = ? AND user_id = ?
                """,
                (ts, new_score, ts, guild_id, user_id),
            )
            self._conn.commit()

    def set_stage(self, guild_id: int, user_id: int, stage: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE activity SET stage = ? WHERE guild_id = ? AND user_id = ?",
                (stage, guild_id, user_id),
            )
            self._conn.commit()

    def set_notified_stage(self, guild_id: int, user_id: int, stage: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE activity SET notified_stage = ? WHERE guild_id = ? AND user_id = ?",
                (stage, guild_id, user_id),
            )
            self._conn.commit()

    def set_exempt(self, guild_id: int, user_id: int, exempt: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE activity SET exempt = ? WHERE guild_id = ? AND user_id = ?",
                (1 if exempt else 0, guild_id, user_id),
            )
            self._conn.commit()

    def snooze(self, guild_id: int, user_id: int, until: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE activity
                   SET snooze_until = ?, snooze_count = snooze_count + 1
                 WHERE guild_id = ? AND user_id = ?
                """,
                (until, guild_id, user_id),
            )
            self._conn.commit()

    def record_kick(self, guild_id: int, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE activity SET kick_count = kick_count + 1 WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self._conn.commit()

    def mark_left(self, guild_id: int, user_id: int, ts: int) -> None:
        """Wipe timestamps and score on leave; keep only counters for context.

        This is what makes the deletion promise honest while still letting
        admins see a pattern of repeated rejoins.
        """
        with self._lock:
            self._conn.execute(
                """
                UPDATE activity
                   SET left_at = ?,
                       last_message_at = NULL,
                       last_voice_at = NULL,
                       last_reaction_at = NULL,
                       last_heartbeat_at = NULL,
                       joined_at = NULL,
                       score = 0,
                       score_updated_at = NULL,
                       stage = 0,
                       notified_stage = 0,
                       snooze_until = NULL
                 WHERE guild_id = ? AND user_id = ?
                """,
                (ts, guild_id, user_id),
            )
            self._conn.execute(
                "DELETE FROM voice_session WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self._conn.commit()

    def record_rejoin(self, guild_id: int, user_id: int, ts: int, new_score: float) -> None:
        """A rejoin counts as activity: the clock restarts, history is discarded."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE activity
                   SET joined_at = ?,
                       left_at = NULL,
                       rejoin_count = rejoin_count + 1,
                       score = ?,
                       score_updated_at = ?,
                       stage = 0,
                       notified_stage = 0,
                       snooze_until = NULL
                 WHERE guild_id = ? AND user_id = ?
                """,
                (ts, new_score, ts, guild_id, user_id),
            )
            self._conn.commit()

    def purge_left(self, older_than: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM activity WHERE left_at IS NOT NULL AND left_at < ?",
                (older_than,),
            )
            self._conn.commit()
        return cursor.rowcount

    # --------------------------------------------------------- voice session

    def voice_open(self, guild_id: int, user_id: int, started_at: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO voice_session (guild_id, user_id, started_at) "
                "VALUES (?, ?, ?)",
                (guild_id, user_id, started_at),
            )
            self._conn.commit()

    def voice_close(self, guild_id: int, user_id: int) -> int | None:
        """Return the session start, or None if there was no open session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM voice_session WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM voice_session WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self._conn.commit()
        return int(row["started_at"])

    def voice_open_sessions(self, guild_id: int) -> list[tuple[int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, started_at FROM voice_session WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        return [(int(row["user_id"]), int(row["started_at"])) for row in rows]
