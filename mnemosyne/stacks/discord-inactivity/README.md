# discord-inactivity

Meldet Admins, wenn ein Mitglied im 35-Plätze-Limit inaktiv geworden ist.
Speichert ausschliesslich User-IDs, Zeitstempel und Zähler — nie Nachrichteninhalte.

Konzept und Begründung der Design-Entscheidungen: `General_Discord_Inactivity_Bot.md`.

## Stand

Phase 0 lauffähig. Alle späteren Stufen sind implementiert, aber über `modes:`
abgeschaltet. In der Auslieferungskonfiguration sammelt der Bot nur Daten und
schickt niemandem etwas.

## Discord Developer Portal

Intents aktivieren: **Server Members Intent**. `Message Content` und `Presence`
werden bewusst **nicht** benötigt.

Bot-Permissions: `View Channels`, `Read Message History`, `Send Messages`,
`Embed Links`, `Kick Members`.

> Die Bot-Rolle muss in der Hierarchie **über** allen Mitgliedern stehen, die
> sie kicken können soll. Sonst schlägt der Kick erst zur Laufzeit fehl.
> Einmal mit einem Testaccount verifizieren.

## Setup

```bash
cp config.example.yaml config.yaml     # IDs eintragen
cp .env.example .env                   # Token eintragen
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

Beim ersten Start läuft der Backfill über 120 Tage Channel-History. Danach steht
`backfill_done` in der `meta`-Tabelle; ein Neustart wiederholt ihn nicht.

## Betriebsstufen

Die drei Schalter unter `modes:` werden nacheinander umgelegt, nicht gemeinsam:

| Schritt | `notify_admins` | `send_user_dms` | `allow_kick` | Dauer |
|---|---|---|---|---|
| 1 — reines Sammeln | false | false | false | 60 Tage |
| 2 — Admin-Gegenprüfung | true | false | false | 2–4 Wochen |
| 3 — User-Heartbeat live | true | true | false | 2–4 Wochen |
| 4 — Vollbetrieb | true | true | true | dauerhaft |

Stage-Übergänge werden auch im Dry Run geschrieben, `notified_stage` dagegen nur
nach einer tatsächlich versendeten Nachricht. Beim Scharfschalten arbeitet der
Scan den Rückstau über `max_dms_per_run` ab, statt an einem Tag alles zu senden.

## Commands

| Command | Wer | Wirkung |
|---|---|---|
| `/inactivity report` | Admin | Liste aufsteigend nach Activity Score |
| `/inactivity check @user` | Admin | Einzelstatus inkl. Zählern |
| `/inactivity kick @user [grund]` | Admin | DM an den User, dann Kick |
| `/inactivity snooze @user` | Admin | vertagt um `snooze.duration_days` |
| `/inactivity exempt @user` | Admin | dauerhafte Ausnahme |
| `/inactivity me` | alle | eigene gespeicherte Werte (DSGVO-Auskunft) |

## Monitoring

`/metrics` auf Port 9125, erreichbar im `monitoring`-Netzwerk.

```yaml
# prometheus.yml
- job_name: discord-inactivity
  static_configs:
    - targets: ["discord-inactivity:9125"]
```

Relevante Alerts:

| Alert | Bedingung |
|---|---|
| `DiscordBotDown` | `discord_gateway_connected == 0 for 15m` |
| `DiscordScanStale` | `time() - discord_last_scan_timestamp_seconds > 172800` |
| `DiscordPendingDecisions` | `discord_members_by_stage{stage="3"} > 0 for 14d` |
| `DiscordBlindSpot` | `discord_unreadable_channels > 0` |

Bot-Health läuft bewusst über ntfy, nicht über Discord: Den Ausfall, den man am
dringendsten sehen will, kann der Bot selbst nicht mehr melden.

## Backup

SQLite nie im Betrieb per `tar` sichern. In `backup-services.sh`:

```bash
# Consistent snapshot of a live SQLite database
sqlite3 /mnt/codex/discord-inactivity/activity.db \
  ".backup /mnt/backup/discord-inactivity/activity-$(date +%F).db"
```

## Tests

```bash
python3 tests/test_smoke.py
```

Prüft Config-Validierung (Fehlkonfiguration muss den Start verhindern),
Decay-Mathematik, Downtime-Abzug, Stage-Reset, Rejoin und Purge — alles ohne
Discord-Verbindung.

## Betriebshinweise

- **`config.yaml` ist ein Datei-Bind-Mount** und damit inode-anfällig. Nach einem
  Git-Pull `docker compose up -d --force-recreate`, nicht nur `restart`.
- **Downtime** wird aus der `liveness`-Tabelle abgeleitet, nicht aus einem
  SIGTERM-Handler. Stromausfall und OOM-Kill laufen durch keinen Handler.
- **Nicht lesbare Channels** werden beim Start gemeldet und als Metrik geführt.
  Das ist die wahrscheinlichste Ursache für den ersten Fehlalarm.
- **Backfill deckt nur Nachrichten ab.** Reaktionen und Voice lassen sich nicht
  rückwirkend rekonstruieren; wer ausschliesslich reagiert, wirkt anfangs still.
