# CWAtlas Collector + MCP Sidecar

[![ci](https://github.com/kk4cnm/cwatlas-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/kk4cnm/cwatlas-collector/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Autonomous **search→capture** collector that monitors amateur HF (+6m) CW around the
clock and records **raw narrowband IQ** of every keyed signal it finds. The output is
**MorseBase**: the training corpus for a CW-decoding model.

The point is the corpus. Existing CW decoders fall apart on the things that make real
on-air CW hard — low SNR, hand-keyed timing, QSB, drift, adjacent-signal splatter. You
cannot train a model on that without a large, honestly-labeled body of raw IQ that
contains it. This collector's job is to spend a year building that body of IQ, and to
keep doing it unattended.

**Status: in production** on `airig-01` since 2026-07-03, feeding a Web-888 SDR into
`/mnt/md0/cwatlas/data`. As of 2026-07-16 the corpus holds **~34,600 captures / ~2,997
IQ-hours / ~81 GB**. See [DESIGN.md](DESIGN.md) for the architecture and the reasoning
behind it; this file covers what the system actually does today.

## How it works

Two planes, which is the core idea — and the reason for this hardware. With only a
handful of receivers you are permanently choosing between *looking* and *recording*. Given
enough channels the two stop competing, and you can watch large portions of the bands
continuously while other channels zero in on the CW that turns up:

- **Search plane** — waterfall (`/W/F`) connections, cheap and wide. They blanket the CW
  "watering holes" (~400–600 kHz total across HF) and feed an Activity Map. Search is
  never the bottleneck.
- **Capture plane** — the RX (`/SND`) channels, scarce and narrow. Reserved entirely for
  recording signals search already found: `mod=iq`, `compression=0`, passband ±250 Hz.
  Capture follows detections; it never hunts.

A **supervisor** loop sits between them and owns every channel assignment: it scores
detections (keying confidence × SNR × solar band weight), assigns channels, and enforces
dwell policy. It is deliberately LLM-free — collection has to survive the agent being
absent, slow, or wrong.

The **MCP sidecar** is a control plane only. An agent can observe (`get_activity_map`,
`get_channel_roster`, `get_collection_stats`) and *nudge* (`prioritize_band`,
`pin_frequency`, `request_deep_dwell`), but nudges are advisory — they go on a bus the
supervisor reads. The agent never drives channels and IQ never flows through a tool call.

### What the hardware actually gave us

DESIGN.md was written against a spec sheet and guessed 13 RX channels. The real device
(`Web888_v2026.609`) reports **12**, of which **11** are usable for capture (the scanner
holds one). Two are reserved for deep-dwell, so steady state is **~9 concurrent
captures** — the `ch=[cap cap ... idl idl]` line in the journal.

## The corpus

Each capture is a **SigMF pair** under `/mnt/md0/cwatlas/data/YYYY-MM-DD/`:

```text
20m_14015.73kHz_20260715T213231Z_ch2.sigmf-data   # ci16_le, headerless
20m_14015.73kHz_20260715T213231Z_ch2.sigmf-meta   # JSON sidecar
```

IQ is decimated **12 kHz → 1.5 kHz inline** (`dsp.py`, DECIM=8) with the CW carrier
landing at **+250 Hz baseband**. Files rotate every 600 s; a long dwell becomes several
segments. Note this means captures **play as noise** in a media player — see
[auditioning](#auditioning-captures).

`catalog.db` (SQLite, WAL) is the index into that corpus — one row per capture, with the
detection SNR and confidence that triggered it, GPS-disciplined start time, band, exact
center frequency, and a `contaminated` flag. **`ended_utc IS NULL` means in flight;** a
row still NULL long after the 600 s rotate window is an orphan, not live work.

Each capture also carries a `run_id` into the `runs` table: what was running when it was
made — receiver firmware, collector git commit (and whether the tree was dirty), and the
effective config, including the band weights and detector thresholds in force at the
time. Captures made before 2026-07-16 predate this and are adopted by an explicit
`kind='synthetic'` run whose NULLs mean *unrecorded*, not *failed to record*. See
[docs/provenance.md](docs/provenance.md).

### Storage budget (answered, not estimated)

DESIGN.md §7 called for a real budget calc before committing to a year. Measured over the
first two weeks:

| | per IQ-hour | note |
| --- | --- | --- |
| 12 kHz (early M1 captures, 611 rows) | 173 MB | pre-decimator |
| 1.5 kHz (current, 33,990 rows) | 21.6 MB | **8× cheaper** |

Steady state is **~5 GB/day → ~1.7 TB/year**, against 11.9 TB of array. A year-run fits
comfortably; the "50 GB/day worst case" in DESIGN.md assumed no decimation and no
activity gating. Cold-storage lifecycle (M5) is therefore not yet urgent.

## Running it

Two systemd units, both `enabled`:

```bash
systemctl status cwatlas-collector    # the collector itself
systemctl status cwatlas-dash         # read-only status dashboard, :8828
```

Configuration is a **TOML file describing your station** — nothing about one
operator's rig lives in source:

```bash
cp config.example.toml config.toml   # then edit; config.toml is gitignored
```

| Section | Purpose |
| --- | --- |
| `[sdr] host/port` | Web-888 address. **No default** — the collector won't guess |
| `[paths] data_dir` | corpus root |
| `[station] lat/lon` | solar band weighting; omit for neutral priorities |
| `[tx] flex_host` | co-located FlexRadio, for TX hygiene (`""` disables) |
| `[capture] rotate_s` | max seconds per file segment |

Precedence is **CLI > environment > config.toml > built-in default**; the env
names (`CWATLAS_SDR_HOST`, `CWATLAS_DATA_DIR`, `CWATLAS_LAT`/`LON`,
`CWATLAS_FLEX_HOST`) still work and still win, so an existing env-driven
deployment keeps running unchanged. The systemd units point at the file with
`CWATLAS_CONFIG=`. A `--config`/`CWATLAS_CONFIG` path that doesn't exist is an
error rather than a silent fallback: believing your settings are loaded when
they aren't means mis-weighted bands all night, looking like nothing is wrong.

The collector runs `--no-mcp` under systemd: **the MCP tools are built but dormant in
production**, because stdio MCP needs a client on stdin and systemd gives it `/dev/null`.
Attach an agent by running the collector manually, or wait for the streamable-HTTP
transport. This is why the dashboard reports `solar: live nudges require MCP`.

The dashboard (`:8828`) is a read-only Flask sidecar — it opens the catalog `mode=ro` and
touches the SDR only over its status endpoints. `/api/summary` is the whole payload.

### Dev, without hardware

```bash
cd ~/cwatlas/collector
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,dash]"
python -m pytest tests/ -q
```

## Auditioning captures

Captures are 1.5 kHz complex int16 with the carrier at ~+250 Hz — noise to a media
player. `scripts/sigmf_listen.py` renders them as WAV at an audible sidetone pitch and
picks strong ones out of the catalog:

```bash
.venv/bin/python scripts/sigmf_listen.py --top 20 --band 40m   # strongest clean captures
.venv/bin/python scripts/sigmf_listen.py --id 1902             # write <capture>.wav
```

Details in [docs/sigmf_listen.md](docs/sigmf_listen.md).

## Design invariants (don't break these)

- **Never churn connections.** Each capture worker opens its `/SND` session once and
  **retunes in place**. A closed connection's channel is held ~1 min server-side; rapid
  open/close starves the whole device. Learned twice on hardware.
- **MCP is control plane only** — never stream IQ or audio through tool calls.
- **The supervisor is authoritative** — agent tools enqueue nudges; they don't drive
  channels. Collection must survive the agent being down.
- **TX front-end protection is hardware** — the Flex amp-key → sequenced coax relay is the
  interlock. `notify_tx` / `ground_antenna` are data hygiene and secondary controls, never
  the thing standing between your amplifier and the SDR front end.
- **Capture raw IQ.** On-device CW decoders are weak-label helpers only, kept out of the
  capture path — the corpus must not inherit their mistakes.
- **Signals must earn a channel**, not merely exist (`min_capture_score`). Closed-band junk
  squatting on a slot for `release_timeout_s` is worse than an idle slot.
- **A catalog row must always be closed.** If finalize is skipped, the row reads as
  "capturing" forever — there is no later pass that cleans it up.
- **Provenance never stops collection.** Event writes are wrapped and can only print;
  a failure to record history must not cost a capture. Corollary: the `contaminated`
  flag commits *before* its event, never in one transaction — rolling back would undo
  the flag, and hygiene beats provenance. See [docs/provenance.md](docs/provenance.md).

Several scheduler constants are scar tissue and are commented as such in
`scheduler.py`; `release_timeout_s` shorter than the scan revisit period caused
release/reassign ping-pong (154 rows in 3 min, most 0-sample), and `max_dwell_s` exists
because one 14 dB signal held a channel for 265 minutes in the first overnight soak.

## Layout

```text
cwatlas_mcp/
  config.py      # site config: config.toml < env < CLI
  runtime.py     # wires supervisor + search/PTT workers + MCP together
  scheduler.py   # Activity Map + supervisor (the LLM-free brain) + ControlBus
  capture.py     # one persistent worker per RX channel; writes SigMF + catalog row
  detector.py    # CW keying detection in the search plane
  dsp.py         # 12k -> 1.5k decimation, carrier placement
  catalog.py     # SQLite corpus index (+ runs / capture_events provenance)
  migrations.py  # schema evolution, keyed on PRAGMA user_version
  provenance.py  # what was running: firmware, git state, effective config
  sdr_client.py  # async AJAX + WebSocket client for the Web-888
  ptt.py         # Flex TX ingest -> contamination marking
  solar.py       # day/night band priority weighting
  server.py      # MCP sidecar: observe / nudge / TX tool families
cwatlas_dash/    # read-only Flask status dashboard (:8828) + OTLP telemetry
scripts/         # sigmf_listen, backfill_orphans, hardware probes
docs/            # session logs, design notes
```

## Roadmap

M0 read-only proof · M1 single capture · M2 full supervisor · M3 TX hygiene — **done and
in production**. M4 agent nudges is **built but dormant** (needs an MCP transport that
survives systemd; see above). M5 storage lifecycle is **open**, and per the budget above,
not yet pressing.

Details in DESIGN.md §12 — though note DESIGN.md is a pre-hardware document: where it
carries a **[verify on hw]** marker and this README states a measured number, trust this
one.

## License

The **collector software** is Apache-2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)) — Copyright 2026 Daniel Nelms (KK4CNM).

**MorseBase — the corpus the collector produces — is a separate work and is not
covered by that license.** Its terms are still to be decided.

## Gotchas

- **Don't mount anything on `/mnt`.** It shadows `/mnt/md0`, and the collector's data dir
  goes with it. On 2026-07-15 this stranded 7 catalog rows for 18.5 h; captures in flight
  kept writing through open fds but could not finalize. The finalize path is now hardened
  and `scripts/backfill_orphans.py --apply` recovers rows stranded this way, but use
  `/mnt/scratch` and skip the excitement. Other services on this host (signoz) broke the
  same way at the same moment.
- **The SDR has 12 channels and 12 user slots.** The dashboard's `users` count includes the
  collector's own connections.
- **`disk error` vs `stream error` in the journal** is a real distinction: the former is the
  filesystem, the latter the radio. Chasing the SDR over what turned out to be a
  `FileNotFoundError` cost an hour once.
