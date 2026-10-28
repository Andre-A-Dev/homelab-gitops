# discord-inactivity

Notifies admins when a member within the 35-seat limit has become inactive.
Stores only user IDs, timestamps and counters — never message content.

Concept and rationale for the design decisions: `General_Discord_Inactivity_Bot.md`.

## Status

In development; the stack runs under the `inactive` profile. Phase 0 is operational. All later stages are implemented but switched off via
`modes:`. In the shipped configuration the bot only collects data and sends
nothing to anyone.

## Discord Developer Portal

Enable intents: **Server Members Intent**. `Message Content` and `Presence` are
deliberately **not** required.

Bot permissions: `View Channels`, `Read Message History`, `Send Messages`,
`Embed Links`, `Kick Members`.

> The bot role must sit **above** every member it is supposed to be able to
> kick in the role hierarchy. Otherwise the kick only fails at runtime.
> Verify once with a test account.

## Setup

```bash
cp config.example.yaml config.yaml     # fill in IDs
cp .env.example .env                   # fill in token
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

On first start, the backfill runs over 120 days of channel history. Afterwards
`backfill_done` is set in the `meta` table; a restart does not repeat it.

## Operating stages

The three switches under `modes:` are flipped one after another, not together:

| Step | `notify_admins` | `send_user_dms` | `allow_kick` | Duration |
|---|---|---|---|---|
| 1 — collection only | false | false | false | 60 days |
| 2 — admin cross-check | true | false | false | 2–4 weeks |
| 3 — user heartbeat live | true | true | false | 2–4 weeks |
| 4 — full operation | true | true | true | permanent |

Stage transitions are written in dry run too, whereas `notified_stage` is only
written after a message was actually sent. When going live, the scan works off
the backlog via `max_dms_per_run` instead of sending everything on one day.

## Commands

| Command | Who | Effect |
|---|---|---|
| `/inactivity report` | Admin | List sorted ascending by activity score |
| `/inactivity check @user` | Admin | Individual status including counters |
| `/inactivity kick @user [reason]` | Admin | DM to the user, then kick |
| `/inactivity snooze @user` | Admin | Postpones by `snooze.duration_days` |
| `/inactivity exempt @user` | Admin | Permanent exemption |
| `/inactivity me` | Everyone | Own stored values (GDPR access request) |

## Monitoring

`/metrics` on port 9125, reachable in the `monitoring` network.

```yaml
# prometheus.yml
- job_name: discord-inactivity
  static_configs:
    - targets: ["discord-inactivity:9125"]
```

Relevant alerts:

| Alert | Condition |
|---|---|
| `DiscordBotDown` | `discord_gateway_connected == 0 for 15m` |
| `DiscordScanStale` | `time() - discord_last_scan_timestamp_seconds > 172800` |
| `DiscordPendingDecisions` | `discord_members_by_stage{stage="3"} > 0 for 14d` |
| `DiscordBlindSpot` | `discord_unreadable_channels > 0` |

Bot health deliberately goes through ntfy, not Discord: the outage you most
urgently want to see is the one the bot can no longer report itself.

## Backup

Never back up SQLite with `tar` while it is running. In `backup-services.sh`:

```bash
# Consistent snapshot of a live SQLite database
sqlite3 /mnt/codex/discord-inactivity/activity.db \
  ".backup /mnt/backup/discord-inactivity/activity-$(date +%F).db"
```

## Tests

```bash
python3 test_smoke.py
```

Covers config validation (a misconfiguration must prevent startup), decay math,
downtime deduction, stage reset, rejoin and purge — all without a Discord
connection.

## Operating notes

- **`config.yaml` is a file bind mount** and therefore prone to the inode issue.
  After a git pull, run `docker compose up -d --force-recreate`, not just
  `restart`.
- **Downtime** is derived from the `liveness` table, not from a SIGTERM handler.
  Power loss and OOM kills never pass through a handler.
- **Unreadable channels** are reported at startup and tracked as a metric. They
  are the most likely cause of the first false alarm.
- **Backfill only covers messages.** Reactions and voice cannot be
  reconstructed retroactively; someone who only reacts looks silent at first.
