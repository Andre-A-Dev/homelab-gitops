# Galatea

Control plane for **Pygmalion** (Prusa MK4S): printer status and job control
via PrusaLink, printer power via a Shelly Plug M Gen3, enclosure climate from a
BLU H&T sensor, and a filament inventory.

Galatea is the counterpart to the read-only exporters. Prometheus/Grafana keep
history and alerts; Galatea does what they cannot — switch things.

## Safety model

Galatea is the first homelab component that **writes to the physical world**.

- **Power-off interlock** (`app/interlock.py`). Power off is refused while the
  printer is printing, paused, busy or showing a dialog, and while the nozzle is
  above `INTERLOCK_NOZZLE_MAX_C` (cutting power stops the heatbreak fan; a hot
  nozzle then risks heat creep and a clog). Refusals explain themselves in the UI.
- **Fail closed.** If PrusaLink does not answer or reports no nozzle
  temperature, power off is refused. Unknown is never treated as idle.
- **Fresh state for decisions.** The UI polls through a 2 s cache; every write
  re-reads the printer first. Check and switch run under one lock.
- **Explicit override.** `force: true` bypasses the interlock. The UI only
  offers it after a refusal, behind a confirmation. Forced actions are logged
  and counted separately.
- **Everything is logged** — including refused and failed actions — in the
  `actions` table and shown under *Activity*.
- **Two auth layers.** Caddy does basic auth for people and injects
  `X-Galatea-Token` upstream. The API rejects requests without it, so other
  containers on `caddy_proxy` cannot bypass Caddy to switch the printer.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/printer/status` | cached 2 s |
| POST | `/api/printer/pause` | only while `PRINTING` |
| POST | `/api/printer/resume` | only while `PAUSED`; refused during `ATTENTION` (answer the dialog on the printer) |
| POST | `/api/printer/stop` | body `{"confirm": true}` |
| GET | `/api/plug` | state, power, interlock preview |
| POST | `/api/plug/on` | |
| POST | `/api/plug/off` | body `{"force": true}` to override the interlock |
| GET | `/api/climate` | includes `age_s` and `stale` |
| GET/POST | `/api/filament` | list / create |
| PATCH/DELETE | `/api/filament/<id>` | `{"is_active": true}` marks the loaded spool |
| GET | `/api/actions` | last 50 actions |
| GET | `/healthz` | liveness only, no upstream checks |
| GET | `/metrics` | Galatea's own counters |

`/`, `/static/*`, `/healthz` and `/metrics` work without the token; everything
under `/api/` requires it.

## Verify before deploying

Never build on unverified identifiers. From Mnemosyne:

```bash
# Printer: state and temperatures. During a print, .job.id must be present —
# pause/resume/stop depend on it.
curl -s --digest -u maker:"$PRUSA_PASSWORD" http://<printer>/api/v1/status | jq '{state: .printer.state, nozzle: .printer.temp_nozzle, job: .job}'

# Plug: switch component 0 exists and reports power
curl -s "http://<plug>/rpc/Switch.GetStatus?id=0" | jq '{output, apower}'

# Climate: gateway KVS holds a recent reading for the sensor MAC
curl -s "http://<gateway>/rpc/KVS.Get?key=<mac>" | jq -r .value | jq

# Container user for ./data
id -u; id -g
```

## Deploy

```bash
cd ~/stacks/galatea
mkdir -p data                          # before first start: Docker would create it root-owned
cp .env.example .env && chmod 600 .env # fill in real values
docker compose up -d --build
docker compose ps                      # wait for (healthy)
```

Code changes (`app/`, `Dockerfile`, `requirements.txt`) need a manual
`docker compose up -d --build` — the webhook only restarts a stack when its
`docker-compose.yml` changes.

### Caddy

1. Add `GALATEA_API_TOKEN` (same value as in Galatea's `.env`) to the Caddy
   stack's environment.
2. Hash a password interactively: `docker exec -it caddy caddy hash-password`
3. Add the block from `snippets/Caddyfile.snippet`. Check the directive name
   against `docker exec caddy caddy version` (`basic_auth` from 2.8 on).
4. Recreate Caddy with `down && up`, not `restart` (bind-mount inode swap).
5. Pi-hole: local DNS record `galatea.home` → Mnemosyne.

Confirm the token layer holds — a request that bypasses Caddy must fail:

```bash
docker run --rm --network caddy_proxy curlimages/curl:latest \
  -s -o /dev/null -w '%{http_code}\n' http://galatea:8000/api/plug     # expect 401
```

### Metrics

Add `snippets/prometheus.snippet.yml` to the scrape config. Prometheus must
share a network with Galatea:

```bash
docker inspect prometheus --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

If `caddy_proxy` is not listed, uncomment `monitoring_monitoring` in
`docker-compose.yml` — Galatea then depends on the monitoring stack being up
first.

| Metric | Labels |
|---|---|
| `galatea_actions_total` | `action`, `result` |
| `galatea_interlock_blocks_total` | `reason` |
| `galatea_upstream_errors_total` | `upstream` |

### First live test of the interlock

Test the block without risking a print: preheat the nozzle from the printer
menu (no job), then press *Turn off* in Galatea. Expected: refused with
"Hotend at … °C". Let it cool below the limit — the panel turns green and
*Turn off* works.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Interlock rules and route wiring are covered against mocked devices; no test
ever contacts real hardware.

## Operational notes

- **Plug firmware updates** make the plug unreachable for several minutes.
  Galatea then reports power as unknown and changes nothing. Consider
  disabling auto-update on the printer plug and updating deliberately.
- **Filament consumption** is booked by hand for now (weigh, update). Automatic
  booking from PrusaLink follows once the firmware's usage field is verified.
- **Camera** is Phase 2.

## Files

| Path | Purpose |
|---|---|
| `app/main.py` | App factory, token guard |
| `app/interlock.py` | Power-off rules (pure function) |
| `app/clients/` | PrusaLink and Shelly clients |
| `app/routes/` | API blueprints and UI page |
| `app/db.py` | SQLite schema, action log |
| `app/templates/`, `app/static/` | UI, no external resources |
| `tests/` | pytest suite |
| `snippets/` | Caddy and Prometheus config to paste |
