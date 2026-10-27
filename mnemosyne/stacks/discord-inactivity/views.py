"""Embeds and buttons.

custom_id carries the target user, so interactions survive a restart without
registering persistent views. The raw on_interaction listener dispatches them.
"""

from __future__ import annotations

import discord

PREFIX = "inact"


def heartbeat_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Ich bin noch da",
            style=discord.ButtonStyle.success,
            custom_id=f"{PREFIX}:heartbeat",
        )
    )
    return view


def admin_view(user_id: int, snooze_exhausted: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Kick",
            style=discord.ButtonStyle.danger,
            custom_id=f"{PREFIX}:kick:{user_id}",
        )
    )
    if not snooze_exhausted:
        view.add_item(
            discord.ui.Button(
                label="Snooze",
                style=discord.ButtonStyle.secondary,
                custom_id=f"{PREFIX}:snooze:{user_id}",
            )
        )
    view.add_item(
        discord.ui.Button(
            label="Dauerhaft ausnehmen",
            style=discord.ButtonStyle.primary,
            custom_id=f"{PREFIX}:exempt:{user_id}",
        )
    )
    return view


def admin_embed(
    member: discord.Member,
    inactive_days: int,
    score: float,
    rejoin_count: int,
    kick_count: int,
    snooze_count: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="Inaktiv seit über der Schwelle",
        description=f"{member.mention} ({member.display_name})",
        colour=discord.Colour.orange(),
    )
    embed.add_field(name="Inaktiv", value=f"{inactive_days} Tage", inline=True)
    embed.add_field(name="Score", value=f"{score:.2f}", inline=True)
    embed.add_field(
        name="Dabei seit",
        value=discord.utils.format_dt(member.joined_at, style="D") if member.joined_at else "?",
        inline=True,
    )
    if rejoin_count or kick_count:
        embed.add_field(
            name="Vorgeschichte",
            value=f"{rejoin_count}× zurückgekommen, {kick_count}× gekickt — zweiter Durchlauf",
            inline=False,
        )
    if snooze_count >= 1:
        embed.add_field(name="Bereits vertagt", value=f"{snooze_count}×", inline=True)
    embed.set_footer(text="Vor dem Kick wird der Status neu geprüft.")
    return embed


def resolved_embed(embed: discord.Embed, note: str) -> discord.Embed:
    embed.colour = discord.Colour.green()
    embed.title = "Erledigt"
    embed.set_footer(text=note)
    return embed
