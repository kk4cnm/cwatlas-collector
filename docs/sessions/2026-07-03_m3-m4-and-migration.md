# CWAtlas Session Journal — M3, M4, and the Move to Production

**Date:** 2026-07-03
**People:** Daniel (KK4CNM) + Claude (Fable 5)
**Follows:** `2026-07-01_first-light-to-first-soak.md`

## 1. Second soak review (24 h, decimated)

1,954 captures, **187 h of channel-IQ in 4.04 GB** (first soak: 105 h in
18 GB). Rotation produced 746 exact 600 s segments at an effective 1498.7 Hz;
max-dwell fired 177 times and unique-frequency diversity jumped to 558; the
band mix flipped with the sun both directions (day: 20m/17m; night:
40m/30m/80m/160m). Zero connection failures, zero orphaned rows.
Year projection ≈ 1.5 TB.

One bug surfaced (3 hits, self-healing): a worker that stall-self-released
could then receive the supervisor's release `None` while idle and treat it as
a Detection — `retune(None.freq_hz)`, one discarded healthy connection, ~100 s
pointless backoff. Fixed (`6f37c17`): idle workers ignore stray releases.
Also cleaned the 27 zero-sample orphan rows (and ~230 MB of partial files)
left by the pre-SIGTERM-handling `kill`ed trials.

## 2. M3 — PTT ingest (`ptt.py`)

The Flex 6600 answered UDP discovery at 192.168.2.80 (SmartSDR 4.2.20, GPSDO
locked — both ends of the system are now GPS-disciplined). The worker speaks
the SmartSDR TCP API: `sub tx all`, parse `interlock state=`, TX = anything
outside RECEIVE/READY/NOT_READY/NONE. Design points:

- TX asserts at **PTT_REQUESTED** — before RF, ahead of the relay's 60 ms.
- 1 s unkey hold rides out relay switch-back transients.
- **Fail open**: a dead Flex link clears tx and reconnects — collection never
  freezes because the radio went to sleep. The hardware relay remains the
  actual front-end protection; this is bookkeeping.
- `set_tx(False)` clears channel contamination flags so post-TX file segments
  start clean (files that overlapped TX latch their own flag).

Validated with a full TX cycle against a fake protocol server and a live
connect/subscribe against the real radio. A real on-air keying test is
pending the operator's next QSO.

## 3. M4 — the MCP surface, actually exercised

First-ever production-mode run (MCP on stdio) exposed the classic sin:
`print()` shares stdout with JSON-RPC. All collector logging now goes to
stderr in MCP mode. Findings fixed along the way:

- `notify_tx` and `mark_contaminated` nudges were pushed by tools but
  silently dropped by the supervisor — now applied (`set_tx` /
  `catalog.mark_window`).
- **Band priority reconciliation**: agent nudges are now *multipliers* on the
  solar baseline, in a separate `band_nudge` dict with a 15-minute TTL.
  Solar refreshes and agent enthusiasm can no longer clobber each other, and
  a forgotten nudge can't steer collection forever.
- `pin_frequency` / `request_deep_dwell` force-assign via a synthetic
  detection (releases naturally if the scanner doesn't confirm).
- `get_collection_stats` implemented against the catalog; new
  `get_band_priorities` shows baseline × nudges.

Smoke-tested end-to-end as a real MCP client: 15 tools, 253 detections on
the activity map, nudge visible with TTL countdown, clean shutdown.

## 4. Migration to airig-01

- Graceful **SIGTERM/SIGINT shutdown** added to the runtime first — under
  systemd, `stop` must finalize in-flight files, not orphan them.
- `--no-mcp` flag: stdio MCP under systemd sees /dev/null stdin and would
  exit instantly; the service runs headless until a streamable-HTTP transport
  lands (future milestone, needed anyway for a remote agent).
- Deployed: rsync to `~dnelms/cwatlas/collector`, venv on python3.12 (system
  3.10 is too old), 21 GB corpus + catalog to `/mnt/md0/cwatlas/data`
  (8.9 TB free), catalog `path` column rewritten for the new prefix,
  25-row random spot-check clean.
- `cwatlas-collector.service`: env-configured, `Restart=always`, 30 s
  SIGTERM grace. Started → 9 channels capturing within 20 s.
  `systemctl restart` verified: "stop signal: shutting down cleanly",
  in_flight=0, back up capturing.

**The collector now runs unattended on airig-01.** The Mac is dev-only —
never run both collectors at once; the SDR has one 12-channel budget.

## 5. State of play

| Milestone | Status |
|---|---|
| M3 PTT ingest | ✅ 2026-07-03 (live TX test pending next QSO) |
| M4 MCP tool surface | ✅ 2026-07-03 |
| M5 airig-01 production deployment | ✅ 2026-07-03 |
| MCP streamable-HTTP transport (remote agent) | planned |
| Detector precision / capture triage | planned (MorseBase preprocessing) |
| Band-edge filter on detections (e.g. 1799.46 kHz "160m") | small, noted |
