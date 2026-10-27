"""Bot logic: event capture, backfill, daily scan, admin commands."""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import metrics, scoring, views
from .config import Config
from .db import Database, now_ts

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


class InactivityBot(commands.Bot):
    def __init__(self, cfg: Config, db: Database) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True          # privileged, needed to enumerate members
        intents.guild_messages = True   # author + timestamp only, no content intent
        intents.guild_reactions = True
        intents.voice_states = True

        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.cfg = cfg
        self.db = db
        self.tz = ZoneInfo(cfg.schedule.timezone)
        self._metrics_runner = None
        self._started_scan = False

    # ------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        host, port = self.cfg.metrics.host_port
        self._metrics_runner = await metrics.start_server(host, port)
        log.info("metrics listening on %s:%s", host, port)

        self._reconcile_downtime()

        self.tree.add_command(InactivityCommands(self))
        guild = discord.Object(id=self.cfg.guild.id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        self.liveness_loop.start()

    def _reconcile_downtime(self) -> None:
        """Turn the gap since the last liveness write into a downtime window.

        This deliberately does not rely on a clean SIGTERM: the longest gaps
        come from power loss and OOM kills, which never run a shutdown handler.
        """
        last_seen = self.db.liveness_read()
        now = now_ts()
        if last_seen is None:
            self.db.liveness_write(now)
            return
        gap = now - last_seen
        if gap > 2 * self.cfg.schedule.liveness_interval_seconds:
            self.db.downtime_add(last_seen, now)
            log.warning(
                "detected %.1f h of downtime, excluded from inactivity", gap / 3600
            )
        self.db.liveness_write(now)

    async def on_ready(self) -> None:
        metrics.GATEWAY_CONNECTED.set(1)
        metrics.DOWNTIME_SECONDS.set(self.db.downtime_total())
        log.info("connected as %s", self.user)

        guild = self.get_guild(self.cfg.guild.id)
        if guild is None:
            log.error("guild %s not visible - is the bot invited?", self.cfg.guild.id)
            return

        self._verify_configuration(guild)
        self._sync_members(guild)
        self._restore_voice_sessions(guild)
        await self._audit_channel_access(guild)
        await self._backfill(guild)

        if not self._started_scan:
            self.scan_loop.start()
            self._started_scan = True

    async def on_disconnect(self) -> None:
        metrics.GATEWAY_CONNECTED.set(0)

    async def on_resumed(self) -> None:
        metrics.GATEWAY_CONNECTED.set(1)

    def _verify_configuration(self, guild: discord.Guild) -> None:
        """Fail loudly on misconfiguration instead of running quietly wrong."""
        for name, channel_id in (
            ("admin_channel_id", self.cfg.guild.admin_channel_id),
            ("audit_channel_id", self.cfg.guild.audit_channel_id),
        ):
            if guild.get_channel(channel_id) is None:
                log.error("%s=%s does not exist in this guild", name, channel_id)

        known = {role.name for role in guild.roles}
        for role_name in self.cfg.exemptions.roles:
            if role_name not in known:
                log.error(
                    "exemptions.roles contains %r, which does not exist - typo?", role_name
                )

        me = guild.me
        if me is not None and not me.guild_permissions.kick_members:
            log.error("missing Kick Members permission")

    # ----------------------------------------------------------- bookkeeping

    def _sync_members(self, guild: discord.Guild) -> None:
        for member in guild.members:
            if member.bot and self.cfg.exemptions.ignore_bots:
                continue
            joined = int(member.joined_at.timestamp()) if member.joined_at else now_ts()
            self.db.upsert_member(guild.id, member.id, joined)
        metrics.MEMBERS_TOTAL.set(len(guild.members))

    def _restore_voice_sessions(self, guild: discord.Guild) -> None:
        """A restart during an ongoing call must not lose the session."""
        live = {
            member.id
            for channel in guild.voice_channels
            for member in channel.members
            if not member.bot
        }
        stored = {user_id for user_id, _ in self.db.voice_open_sessions(guild.id)}
        for user_id in live - stored:
            self.db.voice_open(guild.id, user_id, now_ts())
        for user_id in stored - live:
            self._close_voice(guild.id, user_id)

    async def _audit_channel_access(self, guild: discord.Guild) -> None:
        """Activity in a channel the bot cannot read does not exist for it.

        This is the most likely cause of the first false positive, so it is
        reported explicitly instead of silently skewing the data.
        """
        unreadable = [
            channel.name
            for channel in guild.text_channels
            if not channel.permissions_for(guild.me).read_message_history
        ]
        metrics.UNREADABLE_CHANNELS.set(len(unreadable))
        if unreadable:
            log.warning("cannot read %d channel(s): %s", len(unreadable), ", ".join(unreadable))
            await self.audit(
                f"⚠️ Ich kann {len(unreadable)} Channel nicht lesen: "
                f"{', '.join(unreadable)}. Aktivität dort zählt nicht mit."
            )

    async def audit(self, text: str) -> None:
        if not self.cfg.audit.enabled:
            return
        channel = self.get_channel(self.cfg.guild.audit_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(text[:1900])
            except discord.HTTPException:
                log.exception("audit message failed")

    # -------------------------------------------------------------- scoring

    def record(self, guild_id: int, user_id: int, kind: str, ts: int, amount: float = 1.0) -> None:
        row = self.db.get(guild_id, user_id)
        if row is None:
            self.db.upsert_member(guild_id, user_id, ts)
            row = self.db.get(guild_id, user_id)
            if row is None:
                return
        weight = self.cfg.scoring.weights[kind] * amount
        new_score = scoring.bumped(
            row.score, row.score_updated_at, ts, self.cfg.scoring.half_life_days, weight
        )
        self.db.touch(guild_id, user_id, kind, ts, new_score)
        metrics.LAST_EVENT_TS.set(ts)

    def _close_voice(self, guild_id: int, user_id: int) -> None:
        started = self.db.voice_close(guild_id, user_id)
        if started is None:
            return
        cap = self.cfg.voice.session_max_hours * 3600
        minutes = min(now_ts() - started, cap) / 60
        if minutes >= 1:
            self.record(guild_id, user_id, "voice_minute", now_ts(), amount=minutes)

    # --------------------------------------------------------------- events

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if message.guild.id != self.cfg.guild.id:
            return
        self.record(
            message.guild.id, message.author.id, "message",
            int(message.created_at.timestamp()),
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id != self.cfg.guild.id:
            return
        if payload.member is not None and payload.member.bot:
            return
        self.record(payload.guild_id, payload.user_id, "reaction", now_ts())

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or member.guild.id != self.cfg.guild.id:
            return
        if before.channel is None and after.channel is not None:
            self.db.voice_open(member.guild.id, member.id, now_ts())
        elif before.channel is not None and after.channel is None:
            self._close_voice(member.guild.id, member.id)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != self.cfg.guild.id or member.bot:
            return
        row = self.db.get(member.guild.id, member.id)
        joined = int(member.joined_at.timestamp()) if member.joined_at else now_ts()
        if row is None:
            self.db.upsert_member(member.guild.id, member.id, joined)
            return
        # A rejoin counts as activity: the clock restarts, old timestamps are gone.
        weight = self.cfg.scoring.weights["rejoin"]
        self.db.record_rejoin(member.guild.id, member.id, joined, weight)
        await self.audit(f"↩️ {member.display_name} ist zurückgekommen, Uhr startet neu.")

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id != self.cfg.guild.id:
            return
        self.db.mark_left(member.guild.id, member.id, now_ts())

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        if not custom_id.startswith(f"{views.PREFIX}:"):
            return
        _, action, *rest = custom_id.split(":")
        if action == "heartbeat":
            await self._on_heartbeat(interaction)
        elif rest:
            await self._on_admin_action(interaction, action, int(rest[0]))

    async def _on_heartbeat(self, interaction: discord.Interaction) -> None:
        self.record(self.cfg.guild.id, interaction.user.id, "heartbeat", now_ts())
        metrics.HEARTBEATS.inc()
        await interaction.response.send_message(
            "Alles klar, du bleibst. Danke!", ephemeral=True
        )
        await self.audit(f"💚 {interaction.user} hat bestätigt, noch dabei zu sein.")

    async def _on_admin_action(
        self, interaction: discord.Interaction, action: str, user_id: int
    ) -> None:
        guild = self.get_guild(self.cfg.guild.id)
        if guild is None:
            return
        actor = guild.get_member(interaction.user.id)
        if actor is None or not actor.guild_permissions.kick_members:
            await interaction.response.send_message(
                "Dafür fehlen dir die Rechte.", ephemeral=True
            )
            return

        member = guild.get_member(user_id)
        if member is None:
            await interaction.response.send_message(
                "Die Person ist nicht mehr auf dem Server.", ephemeral=True
            )
            return

        if action == "exempt":
            self.db.set_exempt(guild.id, user_id, True)
            await interaction.response.send_message(
                f"{member.display_name} ist dauerhaft ausgenommen.", ephemeral=True
            )
            await self.audit(f"🛡️ {interaction.user} hat {member} dauerhaft ausgenommen.")
        elif action == "snooze":
            until = now_ts() + self.cfg.snooze.duration_days * SECONDS_PER_DAY
            self.db.snooze(guild.id, user_id, until)
            await interaction.response.send_message(
                f"{member.display_name} für {self.cfg.snooze.duration_days} Tage vertagt.",
                ephemeral=True,
            )
            await self.audit(f"💤 {interaction.user} hat {member} vertagt.")
        elif action == "kick":
            await self._kick_flow(interaction, member, "Inaktivität")
        else:
            return

        if interaction.message is not None:
            await interaction.message.edit(view=None)

    # ----------------------------------------------------------------- kick

    async def _kick_flow(
        self, interaction: discord.Interaction, member: discord.Member, reason: str
    ) -> None:
        if not self.cfg.modes.allow_kick:
            await interaction.response.send_message(
                "Kick ist noch deaktiviert (`modes.allow_kick: false`).", ephemeral=True
            )
            return

        # The embed may be days old. Never act on the state of back then.
        stage, inactive_days = self._evaluate(member)
        if stage < 3:
            await interaction.response.send_message(
                f"{member.display_name} ist wieder aktiv (Stage {stage}, "
                f"{inactive_days} Tage). Abgebrochen.",
                ephemeral=True,
            )
            if interaction.message is not None:
                await interaction.message.edit(view=None)
            return

        delivered = await self.kick_with_notice(member, reason, inactive_days)
        await interaction.response.send_message(
            f"{member.display_name} wurde gekickt."
            + ("" if delivered else " DM war nicht zustellbar."),
            ephemeral=True,
        )

    async def kick_with_notice(
        self, member: discord.Member, reason: str, inactive_days: int
    ) -> bool:
        """DM first, kick second.

        After the kick there is no mutual guild left, so the bot can no longer
        open a DM channel with the user.
        """
        text = self.cfg.messages.kick_dm.format(
            user=member.display_name,
            days=inactive_days,
            limit=self.cfg.guild.member_limit,
            reason=reason[:200],
        )
        delivered = True
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            delivered = False
            metrics.DMS_FAILED.inc()

        await member.kick(reason=f"Inactivity: {reason[:400]}")
        self.db.record_kick(member.guild.id, member.id)
        metrics.KICKS.inc()
        await self.audit(
            f"🚪 {member} gekickt nach {inactive_days} Tagen Inaktivität."
            + ("" if delivered else " ⚠️ DM war nicht zustellbar.")
        )
        return delivered

    # ------------------------------------------------------------ evaluation

    def _is_exempt(self, member: discord.Member) -> bool:
        if member.bot and self.cfg.exemptions.ignore_bots:
            return True
        row = self.db.get(member.guild.id, member.id)
        if row is not None and row.exempt:
            return True
        exempt_roles = set(self.cfg.exemptions.roles)
        return any(role.name in exempt_roles for role in member.roles)

    def _evaluate(self, member: discord.Member) -> tuple[int, int]:
        """Return (stage, effective inactive days) for one member."""
        row = self.db.get(member.guild.id, member.id)
        if row is None:
            return 0, 0
        now = now_ts()
        last = row.last_activity_at or now
        downtime = self.db.downtime_seconds_between(last, now)
        inactive = scoring.effective_inactive_seconds(last, now, downtime)
        inactive_days = int(inactive / SECONDS_PER_DAY)

        if row.joined_at and (now - row.joined_at) < self.cfg.thresholds.grace_days * SECONDS_PER_DAY:
            return 0, inactive_days

        stage = scoring.stage_for(
            inactive_days,
            self.cfg.thresholds.stage_1_days,
            self.cfg.thresholds.stage_2_days,
            self.cfg.thresholds.stage_3_days,
        )
        return stage, inactive_days

    # ----------------------------------------------------------------- loops

    @tasks.loop(seconds=60)
    async def liveness_loop(self) -> None:
        self.db.liveness_write(now_ts())

    @liveness_loop.before_loop
    async def _before_liveness(self) -> None:
        await self.wait_until_ready()

    # Placeholder time; main() replaces it with the configured local time.
    @tasks.loop(time=dtime(hour=9))
    async def scan_loop(self) -> None:
        await self.run_scan()

    @scan_loop.before_loop
    async def _before_scan(self) -> None:
        await self.wait_until_ready()

    async def run_scan(self) -> None:
        guild = self.get_guild(self.cfg.guild.id)
        if guild is None:
            return

        self._cap_runaway_voice_sessions(guild)
        purged = self.db.purge_left(now_ts() - self.cfg.rejoin.purge_after_days * SECONDS_PER_DAY)
        if purged:
            log.info("purged %d rows of members who never came back", purged)

        by_stage = {1: 0, 2: 0, 3: 0}
        pending_dms: list[tuple[discord.Member, int]] = []
        pending_reports: list[tuple[discord.Member, int]] = []
        lowest = None
        now = now_ts()

        for member in guild.members:
            if self._is_exempt(member):
                continue
            row = self.db.get(guild.id, member.id)
            if row is None:
                continue

            score = scoring.decayed(
                row.score, row.score_updated_at, now, self.cfg.scoring.half_life_days
            )
            lowest = score if lowest is None else min(lowest, score)

            stage, inactive_days = self._evaluate(member)
            if stage != row.stage:
                self.db.set_stage(guild.id, member.id, stage)
            if stage:
                by_stage[stage] += 1

            if row.snooze_until and row.snooze_until > now:
                continue
            if stage <= row.notified_stage:
                continue

            if stage == 1:
                # Dashboard only, nothing to announce.
                self.db.set_notified_stage(guild.id, member.id, 1)
            elif stage == 2:
                pending_dms.append((member, inactive_days))
            elif stage == 3:
                pending_reports.append((member, inactive_days))

        # Lowest score first: the least active get handled first, and the ramp
        # guard spreads a backlog over several days instead of one burst.
        pending_dms.sort(key=lambda item: item[1], reverse=True)
        pending_reports.sort(key=lambda item: item[1], reverse=True)

        for member, days in pending_dms[: self.cfg.modes.max_dms_per_run]:
            await self._send_heartbeat_dm(member, days)
        for member, days in pending_reports[: self.cfg.modes.max_admin_reports_per_run]:
            await self._send_admin_report(member, days)

        for stage, count in by_stage.items():
            metrics.MEMBERS_BY_STAGE.labels(stage=str(stage)).set(count)
        metrics.MEMBERS_TOTAL.set(len(guild.members))
        metrics.LOWEST_SCORE.set(lowest or 0.0)
        metrics.LAST_SCAN_TS.set(now)
        metrics.DOWNTIME_SECONDS.set(self.db.downtime_total())
        log.info(
            "scan done: stages=%s dms_pending=%d reports_pending=%d",
            by_stage, len(pending_dms), len(pending_reports),
        )

    def _cap_runaway_voice_sessions(self, guild: discord.Guild) -> None:
        """A session with no leave event (client crash) must not count forever."""
        cap = self.cfg.voice.session_max_hours * 3600
        for user_id, started_at in self.db.voice_open_sessions(guild.id):
            if now_ts() - started_at > cap:
                self._close_voice(guild.id, user_id)
                member = guild.get_member(user_id)
                if member is not None and member.voice is not None:
                    self.db.voice_open(guild.id, user_id, now_ts())

    async def _send_heartbeat_dm(self, member: discord.Member, days: int) -> None:
        if not self.cfg.modes.send_user_dms:
            log.info("[dry-run] would DM %s (%d days)", member, days)
            return
        text = self.cfg.messages.heartbeat_dm.format(
            user=member.display_name,
            days=days,
            limit=self.cfg.guild.member_limit,
            reason="",
        )
        try:
            await member.send(text, view=views.heartbeat_view())
        except (discord.Forbidden, discord.HTTPException):
            metrics.DMS_FAILED.inc()
            await self.audit(f"⚠️ DM an {member} nicht zustellbar ({days} Tage inaktiv).")
        else:
            await self.audit(f"✉️ Heartbeat-DM an {member} ({days} Tage inaktiv).")
        self.db.set_notified_stage(member.guild.id, member.id, 2)

    async def _send_admin_report(self, member: discord.Member, days: int) -> None:
        if not self.cfg.modes.notify_admins:
            log.info("[dry-run] would report %s (%d days)", member, days)
            return
        channel = self.get_channel(self.cfg.guild.admin_channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("admin channel %s unavailable", self.cfg.guild.admin_channel_id)
            return
        row = self.db.get(member.guild.id, member.id)
        if row is None:
            return
        score = scoring.decayed(
            row.score, row.score_updated_at, now_ts(), self.cfg.scoring.half_life_days
        )
        await channel.send(
            embed=views.admin_embed(
                member, days, score, row.rejoin_count, row.kick_count, row.snooze_count
            ),
            view=views.admin_view(
                member.id, snooze_exhausted=row.snooze_count >= self.cfg.snooze.max_count
            ),
        )
        self.db.set_notified_stage(member.guild.id, member.id, 3)

    # -------------------------------------------------------------- backfill

    async def _backfill(self, guild: discord.Guild) -> None:
        """Seed last_message_at from channel history.

        Without this, everyone looks inactive on day one. Note that reactions
        and voice cannot be reconstructed, so a member who only ever reacts
        still looks quiet until live capture catches up.
        """
        if not self.cfg.backfill.enabled or self.db.meta_get("backfill_done"):
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.backfill.lookback_days)
        events: list[tuple[int, int]] = []
        for channel in guild.text_channels:
            if not channel.permissions_for(guild.me).read_message_history:
                continue
            try:
                async for message in channel.history(limit=None, after=cutoff, oldest_first=True):
                    if message.author.bot:
                        continue
                    events.append((int(message.created_at.timestamp()), message.author.id))
            except discord.HTTPException:
                log.exception("backfill failed for #%s", channel.name)

        events.sort()
        scores: dict[int, tuple[float, int]] = {}
        weight = self.cfg.scoring.weights["message"]
        for ts, user_id in events:
            score, updated_at = scores.get(user_id, (0.0, ts))
            scores[user_id] = (
                scoring.bumped(score, updated_at, ts, self.cfg.scoring.half_life_days, weight),
                ts,
            )

        for user_id, (score, ts) in scores.items():
            if self.db.get(guild.id, user_id) is None:
                self.db.upsert_member(guild.id, user_id, ts)
            self.db.touch(guild.id, user_id, "message", ts, score)

        self.db.meta_set("backfill_done", str(now_ts()))
        log.info("backfill complete: %d messages, %d members seeded", len(events), len(scores))
        await self.audit(
            f"📥 Backfill abgeschlossen: {len(events)} Nachrichten aus "
            f"{self.cfg.backfill.lookback_days} Tagen, {len(scores)} Mitglieder erfasst."
        )


class InactivityCommands(app_commands.Group):
    def __init__(self, bot: InactivityBot) -> None:
        super().__init__(name="inactivity", description="Inaktivitätsverwaltung")
        self.bot = bot

    def _admin_only(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        return isinstance(member, discord.Member) and member.guild_permissions.kick_members

    @app_commands.command(name="report", description="Aktuelle Inaktivitätsliste")
    async def report(self, interaction: discord.Interaction) -> None:
        if not self._admin_only(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        rows = []
        for member in guild.members:
            if self.bot._is_exempt(member):
                continue
            stage, days = self.bot._evaluate(member)
            record = self.bot.db.get(guild.id, member.id)
            score = scoring.decayed(
                record.score, record.score_updated_at, now_ts(),
                self.bot.cfg.scoring.half_life_days,
            ) if record else 0.0
            rows.append((score, stage, days, member.display_name))

        rows.sort()
        lines = [
            f"`{score:6.2f}` Stage {stage} · {days:>3} Tage · {name}"
            for score, stage, days, name in rows[:25]
        ]
        body = "\n".join(lines) or "Keine Daten."
        await interaction.followup.send(
            f"**Inaktivität, aufsteigend nach Score**\n{body}", ephemeral=True
        )

    @app_commands.command(name="check", description="Status einer Person")
    async def check(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not self._admin_only(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        stage, days = self.bot._evaluate(member)
        row = self.bot.db.get(member.guild.id, member.id)
        if row is None:
            await interaction.response.send_message("Keine Daten.", ephemeral=True)
            return
        score = scoring.decayed(
            row.score, row.score_updated_at, now_ts(), self.bot.cfg.scoring.half_life_days
        )
        await interaction.response.send_message(
            f"**{member.display_name}** · Stage {stage} · {days} Tage inaktiv · "
            f"Score {score:.2f}\n"
            f"Rejoins: {row.rejoin_count} · Kicks: {row.kick_count} · "
            f"Snoozes: {row.snooze_count} · Ausgenommen: {'ja' if row.exempt else 'nein'}",
            ephemeral=True,
        )

    @app_commands.command(name="kick", description="DM schicken und danach kicken")
    async def kick(
        self, interaction: discord.Interaction, member: discord.Member, grund: str = "Inaktivität"
    ) -> None:
        if not self._admin_only(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        await self.bot._kick_flow(interaction, member, grund)

    @app_commands.command(name="snooze", description="Fall vertagen")
    async def snooze(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not self._admin_only(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        until = now_ts() + self.bot.cfg.snooze.duration_days * SECONDS_PER_DAY
        self.bot.db.snooze(member.guild.id, member.id, until)
        await interaction.response.send_message(
            f"{member.display_name} vertagt.", ephemeral=True
        )
        await self.bot.audit(f"💤 {interaction.user} hat {member} vertagt.")

    @app_commands.command(name="exempt", description="Dauerhaft ausnehmen")
    async def exempt(
        self, interaction: discord.Interaction, member: discord.Member, aktiv: bool = True
    ) -> None:
        if not self._admin_only(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        self.bot.db.set_exempt(member.guild.id, member.id, aktiv)
        await interaction.response.send_message(
            f"{member.display_name} ist {'ausgenommen' if aktiv else 'wieder normal erfasst'}.",
            ephemeral=True,
        )
        await self.bot.audit(
            f"🛡️ {interaction.user} hat {member} "
            f"{'ausgenommen' if aktiv else 'wieder aufgenommen'}."
        )

    @app_commands.command(name="me", description="Was über mich gespeichert ist")
    async def me(self, interaction: discord.Interaction) -> None:
        row = self.bot.db.get(self.bot.cfg.guild.id, interaction.user.id)
        if row is None:
            await interaction.response.send_message(
                "Über dich ist nichts gespeichert.", ephemeral=True
            )
            return

        def fmt(ts: int | None) -> str:
            return f"<t:{ts}:R>" if ts else "—"

        await interaction.response.send_message(
            "**Über dich gespeichert** (nur IDs und Zeitstempel, keine Inhalte):\n"
            f"Beigetreten: {fmt(row.joined_at)}\n"
            f"Letzte Nachricht: {fmt(row.last_message_at)}\n"
            f"Letzte Voice-Aktivität: {fmt(row.last_voice_at)}\n"
            f"Letzte Reaktion: {fmt(row.last_reaction_at)}\n"
            f"Letzte Bestätigung: {fmt(row.last_heartbeat_at)}\n"
            f"Rejoins: {row.rejoin_count}",
            ephemeral=True,
        )
