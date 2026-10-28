---
tags:
  - api
  - bundesagentur
  - job-search
  - unofficial-api
  - python
  - reference
created: 2026-06-19
updated: 2026-06-19
status: Done
---

# Bundesagentur für Arbeit (Federal Employment Agency) – Job Search API v6 (unofficial)

> [!warning] Unofficial API
> This API is not publicly documented. All findings are based on the requests
> the official job search website sends and on empirical tests.
> Changes can happen at any time without notice. The official `openapi.yaml`
> in `bundesAPI/jobsuche-api` (GitHub) still documents the outdated v4
> schema – not reliable for v6.

---

## Base URL

```
GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs
```

---

## Authentication

No OAuth, no personal account. Two headers are required:

```http
X-API-Key: jobboerse-jobsuche
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36
```

> [!warning] User-Agent required
> The API sits behind a WAF that answers requests with the default
> `python-requests` user agent (`python-requests/x.x`) with **HTTP 403** as
> soon as filter parameters (`arbeitszeit`, `angebotsart`,
> `veroeffentlichtseit`) are set. The client therefore sends the same user
> agent string as the official web frontend.
>
> Without filter parameters the default UA is accepted (empirically
> confirmed) – the WAF behaviour appears to depend on the parameters and may
> be non-deterministic.

```python
import requests

HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
```

---

## Request parameters

### Required parameters (in practice)

Technically there are no hard required parameters – a request without
`was`/`wo` returns HTTP 200 with generic results (empirically: nationwide
listings without geo filtering). For meaningful search results, however, these
are required:

| Parameter | Type | Description |
|---|---|---|
| `was` | string | Search term (job title, activity). Interpreted as an AND search across multiple words. Empty string allowed (all listings). |
| `wo` | string | Location: city name **with umlaut** (e.g. `Köln`), postal code (e.g. `50667`) or federal state (e.g. `Hessen`). Alternative: the `koordinaten` parameter. |

> [!warning] Place names without umlaut
> A city name with the umlaut transliterated (`Koeln` instead of `Köln`)
> returns `woOutput.suchmodus: "UNGUELTIG"` and 0 hits. Always use umlauts or
> fall back to `koordinaten`.

### Geo parameters

| Parameter | Type | Description |
|---|---|---|
| `wo` | string | Place name, postal code or federal state (see above) |
| `umkreis` | integer | Search radius in km around `wo`. If omitted, the API picks a default (empirically: ~25 km). |
| `koordinaten` | string | Alternative to `wo`: `"lat,lon"` (e.g. `"50.9375,6.9603"`). Returns `woOutput.suchmodus: "UMKREISSUCHE_MIT_KOORDINATEN"`. |

**`woOutput.suchmodus` values:**

| Value | Meaning |
|---|---|
| `UMKREISSUCHE` | City name or postal code resolved correctly |
| `UMKREISSUCHE_MIT_KOORDINATEN` | Coordinate search |
| `BUNDESLANDSUCHE` | Federal state recognized (no radius, whole state) |
| `UNGUELTIG` | Location not recognized (0 results) |

### Pagination

| Parameter | Type | Description |
|---|---|---|
| `page` | integer | Page number, 1-based |
| `size` | integer | Results per page. **Maximum: 250** (`size=500` → HTTP 400). With `size=100`/`200` all available hits are returned (no artificial cap below 250 observed). |

**Pagination behaviour:**
- `maxErgebnisse` gives the total number of hits
- Pages beyond the last one return HTTP 200 with an empty `ergebnisliste` array (no error)
- `ceil(maxErgebnisse / size)` gives the last valid page

### Filter parameters

| Parameter | Type | Values | Description |
|---|---|---|---|
| `arbeitszeit` | string | See table below | Working time model filter. Multiple values can be combined with `;`. |
| `angebotsart` | integer | `1` | Offer type. Only `1` (ARBEIT, employment) returns results for regular positions. |
| `veroeffentlichtseit` | integer | 0–100+ | Only listings from the last N days. `0` = today (0 results observed). The API appears to have an internal cap at ~30 days (30/60/100 returned identical hit counts). |
| `pav` | string | `"false"` / `"true"` | Private recruitment agency. `"false"` = direct employers only (recommended); `"true"` = agency listings only (significantly fewer hits). |
| `berufsfeld` | string | Free text | Occupational field filter. Valid values from `facetten.berufsfeld.counts`. E.g. `"Informatik"`, `"Elektrotechnik"`, `"Softwareentwicklung und Programmierung"`. |
| `arbeitgeber` | string | Free text | Employer filter. An empty `was` value when used → HTTP 400. Appears to depend on exact/internal company names (several well-known large employers returned 0 hits with umkreis=200). |

**`arbeitszeit` enum values:**

| Value | Meaning | Response fields |
|---|---|---|
| `vz` | Full-time | `arbeitszeitVollzeit: true` |
| `tz` | Part-time | `arbeitszeitTeilzeit*` fields |
| `snw` | Shift / night / weekend | `arbeitszeitSchichtNachtWochenende: true` |
| `ho` | Home office | `homeofficemoeglich: true` |
| `mj` | Mini-job | `istGeringfuegigeBeschaeftigung: true` (hypothesis) |

> [!warning] `ho` filter barely effective
> `arbeitszeit=ho` returned 0 hits for `Software Engineer` in a large city,
> even though items with `homeofficemoeglich: true` exist (empirically). Many
> employers do not tag home office explicitly. For the maximum number of hits, omit the `arbeitszeit` filter and
> evaluate `homeofficemoeglich`/`homeofficetyp` from the response instead.

**Combination:** Multiple values can be separated with `;`: `vz;ho`, `vz;tz`, `tz;ho`.
The combination acts as OR (not AND): `vz;ho` returns the same hit count as
`vz` alone (empirically confirmed).

---

## Response format

### Top level

```json
{
  "ergebnisliste": [...],
  "maxErgebnisse": 94,
  "page": 1,
  "size": 3,
  "woOutput": {...},
  "facetten": {...}
}
```

| Field | Type | Description |
|---|---|---|
| `ergebnisliste` | array | List of job listings (see item schema) |
| `maxErgebnisse` | integer | Total number of hits (independent of `size`/`page`) |
| `page` | integer | Current page |
| `size` | integer | Requested page size |
| `woOutput` | object | Resolved geo information (see above) |
| `facetten` | object | Aggregations for UI filters (see below) |

### Item schema (`ergebnisliste[]`)

Complete field list, determined empirically. `—` = field missing in this item
(not `null`, but absent).

| Field | Type | Description |
|---|---|---|
| `referenznummer` | string | Unique ID (format `"10001-<number>-S"`). Suitable as a dedup key. |
| `stellenangebotsTitel` | string | Job title |
| `stellenangebotsart` | string | Only `"ARBEIT"` observed so far |
| `firma` | string | Employer name |
| `hauptberuf` | string | Primary occupation title (normalized) |
| `alternativBeruf1` | string | Alternative occupation title |
| `alternativBeruf2` | string | Further alternative occupation title |
| `weitereBerufe` | array\<string\> | Further occupation titles/keywords |
| `alleBerufe` | array\<string\> | All occupations including primary and alternative ones |
| `stellenlokationen` | array | Locations (see sub-schema) |
| `entfernung` | integer | Distance in km from the searched location |
| `eintrittszeitraum` | object | `{"von": "YYYY-MM-DD"}` – earliest start date |
| `datumErsteVeroeffentlichung` | string | First publication (`YYYY-MM-DD`) |
| `veroeffentlichungszeitraum` | object | `{"von": "YYYY-MM-DD"}` |
| `aenderungsdatum` | string | Last change (ISO 8601 with time) |
| `vertragsdauer` | string | `UNBEFRISTET` (permanent) / `KEINE_ANGABE` (not specified) |
| `arbeitszeitVollzeit` | boolean | Full-time position |
| `arbeitszeitSchichtNachtWochenende` | boolean | Shift/night/weekend |
| `arbeitszeitTeilzeitVormittag` | boolean | Part-time mornings |
| `arbeitszeitTeilzeitNachmittag` | boolean | Part-time afternoons |
| `arbeitszeitTeilzeitAbend` | boolean | Part-time evenings |
| `arbeitszeitTeilzeitFlexibel` | boolean | Flexible part-time |
| `istGeringfuegigeBeschaeftigung` | boolean | Mini-job |
| `homeofficemoeglich` | boolean | Home office possible |
| `homeofficetyp` | string | `ANGABE_IN_PROZENT` / `NACH_VEREINBARUNG` |
| `homeofficeprozent` | integer | Home office share in % (if `homeofficetyp=ANGABE_IN_PROZENT`) |
| `verguetungsangabe` | string | `KEINE_ANGABEN` / `JAHRESGEHALT` / further enum values |
| `artDerVerguetung` | string | `GEHALTSSPANNE` / usually missing |
| `gehaltsspanneVon` | float | Annual salary from (EUR, if `artDerVerguetung=GEHALTSSPANNE`) |
| `gehaltsspanneBis` | float | Annual salary to (EUR) |
| `chiffrenummer` | string | Box number (for anonymous listings) |
| `externeURL` | string | Direct link to the job listing (often missing) |
| `arbeitgeberKundennummerHash` | string | Hash of the employer ID (for dedup by company) |

**`stellenlokationen[]` sub-schema** (illustrative values):

```json
{
  "adresse": {
    "strasse": "Musterstr.",
    "hausnummer": "1",
    "plz": "12345",
    "ort": "Musterstadt, Musterregion",
    "region": "MUSTERLAND",
    "land": "DEUTSCHLAND"
  },
  "breite": 51.0,
  "laenge": 10.0
}
```

`strasse`/`hausnummer` are not present in all entries. `ort` often contains the
region suffix (`"<city>, <region>"`).

### Facets (`facetten`)

Aggregations over all hits – useful for filter UIs or statistics:

| Facet | Description |
|---|---|
| `befristung` | Type of fixed term: `1`=permanent, `2`=?, `3`=? (counts) |
| `verguetung` | Pay information: `"jahr"` = annual salary given |
| `externestellenboersen` | Whether the listing comes from an external job board: `true`/`false` |
| `behinderung` | Positions for people with disabilities: `false` = no restriction |
| `pav` | Share of private recruitment agencies |
| `berufsfeld` | Occupational fields with counts (usable as `berufsfeld` parameter values) |
| `arbeitsort` | Locations with counts |
| `arbeitsort_plz` | Postal codes with counts |
| `veroeffentlichtseit` | Hits by age: `1`/`7`/`14`/`28`/`alle` (all) days |
| `weitereberufe` | Further occupation titles with counts |

---

## Error format

On validation errors (HTTP 400):

```json
{
  "timestamp": "2026-06-19T17:11:30.040228326Z",
  "logref": "98c19",
  "messages": [
    {
      "code": "EINGABEN_UNVOLLSTAENDIG_ODER_FEHLERHAFT",
      "path": "size",
      "detail": "must be less than or equal to 250"
    }
  ]
}
```

| HTTP | Cause |
|---|---|
| 200 | Success (also with 0 hits) |
| 400 | Validation error (e.g. `size > 250`, `arbeitgeber` without `was`) |
| 403 | Wrong/missing API key, or WAF block (user agent) |

---

## Rate limiting

No hard rate limit observed: 10 rapid requests without delay all returned
HTTP 200. The response header `X-API-BLOCKED: 0` points to a blocking system
that does not kick in with moderate use. No `Retry-After` or `X-RateLimit-*`
header visible.

Recommendation: 1–2 seconds of delay between requests as a courtesy towards the
public API.

---

## Minimal Python example

```python
import requests

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

resp = requests.get(API_BASE, headers=HEADERS, params={
    "was": "Software Engineer",
    "wo": "Köln",
    "umkreis": 25,
    "page": 1,
    "size": 50,
    "pav": "false",
    "angebotsart": 1,
    "veroeffentlichtseit": 7,
})

data = resp.json()
print(f"{data['maxErgebnisse']} hits")
for job in data["ergebnisliste"]:
    ort = job["stellenlokationen"][0]["adresse"].get("ort", "?") \
          if job.get("stellenlokationen") else "?"
    url = job.get("externeURL") or \
          f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{job['referenznummer']}"
    print(f"- {job['stellenangebotsTitel']} | {job.get('firma', '?')} | {ort}")
    print(f"  {url}")
```

---

## Open questions / unknowns

- **`angebotsart` values 2/4/16/32/34/64**: all returned 0 hits for
  `was=Software Engineer` – whether these values return results for other
  search terms (vocational training, internship) was not tested.
- **`befristung` facet**: values `1`/`2`/`3` – exact meaning unclear
  (1=permanent is a hypothesis).
- **`arbeitgeber` parameter**: Appears to depend on exact internal company
  names; several well-known large employers returned 0 hits. Possibly a hash/ID is
  required instead of plain text.
- **`mj` value for `arbeitszeit`**: 0 hits – could be a valid value that simply
  does not occur for IT positions.

---

*Determined empirically with ad-hoc probe scripts · Claude · claude.ai*
