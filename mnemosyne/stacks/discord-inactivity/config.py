"""Configuration loading and fail-fast validation.

Everything the admins may want to change lives in config.yaml.
Secrets stay in the environment. A typo must abort the start, not surface
at runtime as half a direct message.
"""

from __future__ import annotations

import string
from datetime import time as dtime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWED_PLACEHOLDERS = {"user", "days", "limit", "reason"}
DISCORD_MESSAGE_LIMIT = 2000


class Strict(BaseModel):
    # Unknown keys are a typo, not a feature.
    model_config = ConfigDict(extra="forbid")


class GuildConfig(Strict):
    id: int
    member_limit: int = 35
    admin_channel_id: int
    audit_channel_id: int

    @field_validator("id", "admin_channel_id", "audit_channel_id")
    @classmethod
    def _looks_like_snowflake(cls, value: int) -> int:
        if value < 10**15:
            raise ValueError(
                f"{value} is not a Discord ID. Enable developer mode and copy the real ID."
            )
        return value


class Thresholds(Strict):
    grace_days: int = 14
    stage_1_days: int = 30
    stage_2_days: int = 60
    stage_3_days: int = 90

    @model_validator(mode="after")
    def _ordered(self) -> "Thresholds":
        if not self.stage_1_days < self.stage_2_days < self.stage_3_days:
            raise ValueError(
                "thresholds must increase: "
                f"stage_1({self.stage_1_days}) < stage_2({self.stage_2_days}) "
                f"< stage_3({self.stage_3_days})"
            )
        if self.grace_days >= self.stage_1_days:
            raise ValueError("grace_days must be shorter than stage_1_days")
        return self


class Modes(Strict):
    notify_admins: bool = False
    send_user_dms: bool = False
    allow_kick: bool = False
    max_dms_per_run: int = 3
    max_admin_reports_per_run: int = 5


class Exemptions(Strict):
    roles: list[str] = Field(default_factory=list)
    ignore_bots: bool = True


class Rejoin(Strict):
    reset_clock: bool = True
    restart_grace: bool = True
    purge_after_days: int = 180


class Snooze(Strict):
    duration_days: int = 30
    max_count: int = 3


class Voice(Strict):
    session_max_hours: int = 8


class Backfill(Strict):
    enabled: bool = True
    lookback_days: int = 120


class Scoring(Strict):
    half_life_days: int = 30
    weights: dict[str, float]

    @field_validator("weights")
    @classmethod
    def _known_kinds(cls, value: dict[str, float]) -> dict[str, float]:
        required = {"message", "voice_minute", "reaction", "rejoin", "heartbeat"}
        missing = required - value.keys()
        if missing:
            raise ValueError(f"scoring.weights is missing: {sorted(missing)}")
        unknown = value.keys() - required
        if unknown:
            raise ValueError(f"scoring.weights has unknown kinds: {sorted(unknown)}")
        return value


class Schedule(Strict):
    scan_at: str = "09:00"
    timezone: str = "Europe/Berlin"
    liveness_interval_seconds: int = 60

    @field_validator("scan_at")
    @classmethod
    def _parseable(cls, value: str) -> str:
        hour, _, minute = value.partition(":")
        if not (hour.isdigit() and minute.isdigit()):
            raise ValueError(f"scan_at must be HH:MM, got {value!r}")
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError(f"scan_at is out of range: {value!r}")
        return value

    @property
    def scan_time(self) -> dtime:
        hour, minute = (int(part) for part in self.scan_at.split(":"))
        return dtime(hour=hour, minute=minute)


class Messages(Strict):
    heartbeat_dm: str
    kick_dm: str

    @field_validator("heartbeat_dm", "kick_dm")
    @classmethod
    def _valid_template(cls, value: str, info: Any) -> str:
        used = {
            field
            for _, field, _, _ in string.Formatter().parse(value)
            if field is not None
        }
        unknown = used - ALLOWED_PLACEHOLDERS
        if unknown:
            raise ValueError(
                f"{info.field_name} uses unknown placeholders {sorted(unknown)}; "
                f"allowed: {sorted(ALLOWED_PLACEHOLDERS)}"
            )
        # Rendered length grows with the substituted values, so leave headroom.
        if len(value) > DISCORD_MESSAGE_LIMIT - 200:
            raise ValueError(
                f"{info.field_name} is {len(value)} chars; Discord caps messages at "
                f"{DISCORD_MESSAGE_LIMIT} and substitution needs headroom"
            )
        return value


class Audit(Strict):
    enabled: bool = True
    purge_messages_after_days: int = 180


class Logging(Strict):
    level: str = "INFO"
    to_discord: bool = False

    @field_validator("level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        if value.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"unknown log level {value!r}")
        return value.upper()

    @field_validator("to_discord")
    @classmethod
    def _never_to_discord(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "technical logs must not go to Discord: the failure you most need to "
                "see is the one the bot cannot report. Use Loki and ntfy instead."
            )
        return value


class Metrics(Strict):
    bind: str = "0.0.0.0:9125"

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.bind.rpartition(":")
        return host or "0.0.0.0", int(port)


class Config(Strict):
    guild: GuildConfig
    thresholds: Thresholds
    modes: Modes = Field(default_factory=Modes)
    exemptions: Exemptions = Field(default_factory=Exemptions)
    rejoin: Rejoin = Field(default_factory=Rejoin)
    snooze: Snooze = Field(default_factory=Snooze)
    voice: Voice = Field(default_factory=Voice)
    backfill: Backfill = Field(default_factory=Backfill)
    scoring: Scoring
    schedule: Schedule = Field(default_factory=Schedule)
    messages: Messages
    audit: Audit = Field(default_factory=Audit)
    logging: Logging = Field(default_factory=Logging)
    metrics: Metrics = Field(default_factory=Metrics)

    @model_validator(mode="after")
    def _backfill_covers_thresholds(self) -> "Config":
        if self.backfill.enabled and self.backfill.lookback_days <= self.thresholds.stage_3_days:
            raise ValueError(
                f"backfill.lookback_days ({self.backfill.lookback_days}) must exceed "
                f"thresholds.stage_3_days ({self.thresholds.stage_3_days}), otherwise the "
                "first scan produces stage 3 cases out of a pure data gap"
            )
        return self

    @model_validator(mode="after")
    def _distinct_channels(self) -> "Config":
        if self.guild.admin_channel_id == self.guild.audit_channel_id:
            raise ValueError(
                "admin_channel_id and audit_channel_id must differ: an unread marker in "
                "the admin channel should mean 'someone has to decide'"
            )
        return self


def load(path: str | Path) -> Config:
    """Load and validate config.yaml. Raises on any problem."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not contain a YAML mapping")
    return Config.model_validate(raw)
