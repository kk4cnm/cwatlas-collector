# CWAtlas Dashboard (`cwatlas_dash`) — Design

**Date:** 2026-07-03
**Status:** approved pending spec review
**Constraint:** zero changes to collector code. The production collector on airig-01
runs under systemd with `--no-mcp` (stdio transport has no client on stdin), so there
is no MCP endpoint to call today. The dashboard reads the same underlying sources the
MCP observe tools read, with a data layer shaped like those tools so it can become an
MCP client when the streamable-HTTP transport milestone lands.

## Goal

A read-only web UI showing: collector status, what is being captured, collection
statistics (all-time totals plus 1 h / 12 h / 24 h / 7 d windows), and system health.
Flask, bound to `0.0.0.0:8828` (LAN-only stance accepted by the operator; no auth).

## Architecture

New sibling package `cwatlas_dash/` next to `cwatlas_mcp/` in this repo. It imports
`cwatlas_mcp.sdr_client` and `cwatlas_mcp.solar` as libraries and never imports the
scheduler, capture path, or control bus — strictly an observer. Deployed as its own
systemd unit (`deploy/cwatlas-dash.service`, same venv, `Restart=always`) so it lives
and dies independently of the collector. Model B extended: the collector never depends
on the dashboard, and the dashboard renders usefully when the collector is down —
that is precisely when a status page matters.

```
cwatlas_dash/
  __init__.py
  __main__.py          # argparse (--host 0.0.0.0 --port 8828 --data-dir ...), app.run
  sources.py           # MCP-shaped data layer (see below)
  app.py               # Flask routes
  templates/index.html # single page, Jinja shell
  static/dash.js       # vanilla JS: poll /api/summary, render panels + inline SVG
  static/dash.css
deploy/cwatlas-dash.service
tests/test_dash_*.py
```

Flask is added as an optional extra in `pyproject.toml`
(`[project.optional-dependencies] dash = ["flask"]`) so the collector install stays
unchanged.

## Data layer — `sources.py`

One function per panel. Where an MCP observe tool exists, the return shape matches it
exactly (`collection_stats(window)` ≡ `get_collection_stats`, `sdr_status()` ≡
`get_sdr_status`, `adc_overload()` ≡ `get_adc_overload`). Migration to MCP-over-HTTP
later is a rewrite of this file only; `app.py` and the frontend do not change.

Four independent sources, each individually failable:

1. **Catalog (SQLite, read-only).** Opens
   `/mnt/md0/cwatlas/data/catalog.db` via `sqlite3.connect("file:...?mode=ro",
   uri=True)`, one connection per request (WAL supports concurrent readers; the
   read-only URI makes catalog corruption by the dashboard impossible). Queries:
   - `window_stats(since_ts)` — same SQL/shape as `Catalog.window_stats`: captures,
     iq_hours, bytes, contaminated, by_band. Computed for 1 h, 12 h, 24 h, 7 d.
   - all-time totals — same shape as `Catalog.stats` plus iq_hours/bytes.
   - hourly buckets, last 24 h — `COUNT(*)`, `SUM(contaminated)`, IQ seconds grouped
     by hour, for the capture-rate chart.
   - in-flight rows (`ended_utc IS NULL`) — freq, band, started_utc, strength_db,
     keyed_conf; doubles as the "capturing right now" view.
   - recent captures — last N finalized rows for the table.
2. **SDR (AJAX only).** `SdrClient.get_status()`, `get_adc()`, `get_snr()`,
   `get_users()` — plain HTTP GETs. **Never `read_config()` or any WebSocket
   stream**: those occupy one of the device's 12 rx channel slots and would steal
   capture capacity from the collector. 2 s timeout; results cached ~10 s server-side
   so multiple browser tabs cannot hammer the device. `SdrClient` is async, so calls
   are wrapped with `asyncio.run()` inside the cached fetch.
3. **System.** `systemctl show cwatlas-collector -p
   ActiveState,SubState,ExecMainStartTimestamp,NRestarts,MemoryCurrent`;
   `journalctl -u cwatlas-collector -n 100 --no-pager -o short-iso` for the log
   panel and an error/warning count; `shutil.disk_usage` on the data dir. If the
   journal is not readable without sudo (systemd-journal group membership — verify
   during implementation), the log panel degrades to an explanatory message.
4. **Solar.** `cwatlas_mcp.solar.band_weights(lat, lon)` recomputed with the same
   coordinates as the collector unit file (read from `CWATLAS_LAT`/`CWATLAS_LON`
   env, defaulting to the unit-file values 33.427 / −82.208). Solar baseline only;
   live agent nudges are in-process state and shown as "n/a (MCP offline)".

## Routes — `app.py`

- `GET /` — Jinja shell.
- `GET /api/summary` — one JSON blob feeding every panel: `{service, sdr, adc,
  disk, totals, windows: {"1h":…, "12h":…, "24h":…, "7d":…}, hourly, inflight,
  solar, journal}`. Each key is either the payload or `{"error": "...", "since":
  ts}` — per-source try/except; the endpoint itself never 500s because a source is
  down.
- `GET /api/captures?limit=50` — recent finalized captures for the table
  (default 50, max 500).

## Frontend

One template, vanilla JS, zero build step, zero CDN/network dependencies (inline SVG
charts). `fetch('/api/summary')` every 15 s; a stale banner appears if two
consecutive polls fail. Panels:

- **Status strip** — collector ActiveState + uptime + restart count; SDR reachable +
  firmware + GPS fix; ADC overload flag; disk free/total on the data volume.
- **Totals** — all-time captures, IQ hours, bytes, contaminated count, in-flight now.
- **Window cards** — 1 h / 12 h / 24 h / 7 d: captures, IQ hours, contamination %,
  per-band breakdown.
- **Capture-rate chart** — last 24 h, hourly bars, contaminated portion visually
  distinct.
- **Live channels** — in-flight captures: freq, band, dwell so far, trigger
  strength/keyed-confidence.
- **Recent captures table** — last 50 finalized rows.
- **Band priorities** — solar weights by band + day/night phase; nudges marked n/a.
- **Journal tail** — last ~30 collector log lines.

Panel visual design follows the dataviz skill guidance at implementation time.

## Error handling

- Every panel renders its own error state ("SDR unreachable since 14:02") without
  affecting the others.
- Collector stopped ⇒ status strip shows it red; catalog panels still work (SQLite
  file is readable regardless); in-flight rows from a crashed collector may linger —
  rows in flight longer than 2× the rotate period (600 s) are flagged "stale?".
- SDR polling can never affect collection: AJAX-only, short timeout, server-side
  cache.

## Testing

- Window/bucket/in-flight SQL against a temp fixture DB with known rows spanning the
  window edges (including an in-flight row and contaminated rows).
- SDR status/ADC parsing against `httpx.MockTransport`.
- `/api/summary` smoke test with all sources mocked: every key present; a raising
  source yields `{"error": ...}` for its key and 200 overall.

## Deployment

`deploy/cwatlas-dash.service`: same venv,
`ExecStart=.venv/bin/python -m cwatlas_dash --host 0.0.0.0 --port 8828`,
`Environment=CWATLAS_DATA_DIR=/mnt/md0/cwatlas/data`, `Restart=always`. Flask's
built-in server (threaded) is sufficient for a single-operator LAN dashboard; if it
ever needs hardening, waitress is a drop-in.

Implementation includes installing and activating the unit on airig-01
(`systemctl enable --now cwatlas-dash`) so the dashboard starts on boot and is
always reachable — same operational posture as the collector itself.

## Out of scope (explicit)

- Any control actions (pause/resume/nudge) — the control bus is in-process only.
- Live activity map and live nudge state — unreachable without MCP; return for free
  when MCP-over-HTTP lands (swap `sources.py` internals).
- Auth/TLS — operator accepted the LAN-only 0.0.0.0 stance.
- IQ/audio anywhere near the dashboard (design invariant).
