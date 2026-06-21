# CWAtlas Collector + MCP Sidecar — Design

> Status: **draft skeleton**, 2026-06-21. Targets a Web-888 SDR (13 RX + 13 waterfall
> channels, ~61.44 MHz front end) arriving ~June 2026. Numbers marked **[verify on hw]**
> must be confirmed against the real device before relying on them.

## 1. Goal

Autonomously monitor amateur-radio HF (+6m) CW activity 24/7 for ~1+ year, capture
**raw narrowband IQ** of CW signals (low SNR, varying keying speed), and catalog them in a
database — building the training corpus for **MorseBase** (the CW-decoding LLM).

The system must keep collecting **even when the managing LLM agent is absent, slow, or
wrong**. The agent steers and curates; it is never in the survival-critical path.

## 2. System context

Three pillars under `~/cwatlas/`:

| Pillar | Dir | Role |
|---|---|---|
| SDR firmware | `web-888_server_os/` | KiwiSDR-fork server on the Web-888; exposes HTTP/AJAX + WebSocket APIs |
| **Collector + MCP** | `collector/` | **this project** — off-device search→capture supervisor + MCP control plane |
| Training | `MorseBase/` | offline labeling + model training on captured IQ |

```
            ┌──────────────── collection host (off-device) ─────────────────┐
            │                                                                │
 Web-888 ───┼─ 13× /W/F (search) ─►  Search workers ─► Activity Map          │
  (SDR)     │                                            │                   │
   ▲   ▲    │                                       Scheduler/Supervisor     │
   │   │    │                                            │                   │
   │   └────┼─ ≤13× /SND IQ (capture) ◄── Capture workers ┘                  │
   │        │                               │                                │
   │        │                          Catalog DB (SQLite) ──► IQ files      │
   │        │                               ▲        ▲                       │
   │        │   MCP sidecar ── reads ───────┘        │ writes nudges         │
   │        │      ▲   (control plane only)──────────┘                       │
   │        └──────┼─────────────────────────────────────────────────────── ┘
   │               │ MCP (stdio or streamable-HTTP)
   │        Managing LLM agent
   │
 PTT/amp-key (hardware interlock, NOT software) ─► coax relay grounds SDR ant on TX
```

## 3. Two planes (the core idea)

**Search plane — waterfall channels.** Cheap and wide. One `/W/F` connection at
`zoom=10` spans ~60 kHz at ~58 Hz/bin **[verify on hw: span = ui_srate / 2^zoom,
ui_srate≈61.44 MHz]** — near-ideal CW predetection bandwidth. A handful of these blanket
every band's small CW "watering hole" (≈400–600 kHz total across HF). We get 13, so search
is never the bottleneck. **Do not enable `wf_share`** (steals waterfall capacity, caps zoom
at 11).

**Capture plane — the 13 RX channels.** Scarce and narrow. Reserved entirely for recording
signals the search plane already found: `mod=iq`, `compression=0`, passband ±150–250 Hz.

The old "13 channels = only 162.5 kHz" worry was a *capture-only* limit and is moot — capture
follows detections, it doesn't hunt.

## 4. Components

| Component | Responsibility |
|---|---|
| `sdr_client` | async wrapper over the Web-888 AJAX (`/status`,`/snr`,`/adc`,…) + WebSocket (`/W/F`,`/SND`,`/EXT`) APIs. Auth, reconnect, keepalive. |
| Search worker | one per searched band; consumes a `/W/F` stream, runs the CW detector, emits/updates `Detection`s. |
| **Activity Map** | live table of `Detection`s (freq, strength, keyed-confidence, last-seen) across all searched bands. The agent's window into "what's on." |
| **Scheduler/Supervisor** | the deterministic brain: turns the Activity Map into channel assignments; enforces dwell/release; respects nudges & TX state. |
| Capture worker | one per active RX channel; holds a `/SND` IQ session, writes IQ + metadata, reports status. |
| Catalog DB | SQLite: `captures`, `detections`, `tx_events`, `channel_state`, `nudges`. The durable record + the decoupling boundary. |
| MCP sidecar | thin control-plane interface; reads state, writes nudges. **Never carries audio/IQ.** |

## 5. The supervisor loop

Runs on a fixed tick (e.g. 1 s). Pure function of state → actions; no LLM in the loop.

```
every tick:
  1. ingest detections from search workers → update Activity Map (decay stale entries)
  2. read pending nudges (pin/prioritize/pause/deep-dwell) from control bus
  3. if TX active (PTT): mark all in-flight captures contaminated; hold new assignments
  4. compute desired assignments:
       candidates = active detections, scored by:
         priority(nudge) > keyed_confidence > strength > coverage_debt(band/freq)
       respect: per-band caps, min dwell, weak-signal "deep-dwell" reserve channels
  5. diff desired vs current channel roster:
       release channels whose signal went idle > release_timeout
       assign idle channels to top unserved candidates (tune, narrow, mod=iq)
  6. persist channel_state + capture rows
```

### Channel allocation policy (initial)

- **Reserve** 1–2 channels for *speculative deep-dwell* on marginal/weak candidates the
  waterfall flagged with low confidence (the low-SNR cases MorseBase most needs).
- **Min dwell** so a channel isn't thrashed off a real QSO by a transient.
- **Release** when the signal's keyed energy stays below threshold for `release_timeout`
  (CW QSOs have gaps — tune this so we don't drop mid-QSO).
- **Coverage debt**: prefer freqs/bands under-sampled recently, so the year-long corpus
  doesn't over-represent the loudest signals.
- **Fairness caps** per band so one busy band can't starve others.

## 6. CW detection in the search plane

We don't decode here — we answer *"is this bin keyed CW?"*:

1. Per bin, track power vs a rolling noise floor → candidate if above threshold.
2. **Keying signature**: CW gates on/off at dot/dash cadence (~20–120 ms elements).
   Discriminate from carriers (steady) and SSB/data (different envelope stats) via
   envelope variance and/or autocorrelation of the bin's power-vs-time to find a keying
   period. Output a `keyed_confidence` ∈ [0,1].
3. Bin collisions (<~58 Hz apart) are fine for "activity here"; the capture channel's
   precise narrow filter resolves them later.

**Reuse:** `web-888_server_os/extensions/CW_skimmer` (csdr) and `CW_decoder`
(UHSDR/danilo) are useful references for the detector and as **weak-label generators** to
bootstrap MorseBase labels — reliable mainly at higher SNR; the hard low-SNR tail still
needs human/consensus/self-supervised labeling. Keep decoders **out** of the capture path;
always record raw IQ as ground truth.

## 7. Capture format & storage budget

- Per channel: complex IQ at the channel sample rate (default ~12 kHz **[verify]**),
  `compression=0`. Narrow the passband to ±150–250 Hz around the signal.
- Frequencies are trustworthy to a few Hz (GPS + 0.5 ppm TCXO) — log exact center freq,
  band, timestamp (GPS-disciplined), SNR, and detector confidence as metadata.
- **Budget sanity:** 13 ch × 12 kHz × complex × 2 B ≈ 600 kB/s *if all always recording*
  ≈ ~50 GB/day worst case. Activity-gating + decimating to the occupied bandwidth cuts this
  hard. **Do a real budget calc before committing a year.** Consider per-capture WAV/SigMF
  + lifecycle (cold storage for older shards).
- Recommend **SigMF** for IQ recordings (metadata sidecar) — interoperable with the ML side.

## 8. TX coordination & data hygiene (operator is KK4CNM, co-located FlexRadio 6600)

**Front-end protection is hardware, not software.** Software RX suspend does nothing for
analog overload. Primary interlock: Flex amp-key/TX RCA output → fail-safe, sequenced coax
relay / RF limiter that grounds the SDR antenna *before* RF appears. Never a network/MCP
round-trip.

The collector's job is **data hygiene**: read TX state (Flex PTT via GPIO/serial, or
SmartSDR CAT) and on TX → mark affected capture windows **contaminated** (so MorseBase never
trains on your own signal/splatter), pause logging, log a `tx_event`, resume after.
Monitor ADC overload via `ov_mask` / the `/adc` AJAX endpoint as a reactive alert (+ optional
auto-ground via the `ant_switch` EXT `SET Antenna=0`, secondary only).

## 9. MCP tool surface (control plane only)

Three families. All read live state or enqueue a nudge; **none stream IQ/audio**.

**Observe**
- `get_sdr_status()` → firmware/channel/health summary (AJAX `/status`).
- `get_activity_map(band=None)` → current detections (freq, strength, keyed_confidence, age).
- `get_channel_roster()` → what each of the 13 RX channels is capturing.
- `get_collection_stats(window)` → coverage/throughput over the long run (catalog DB).
- `get_adc_overload()` → ADC clip/overload state (AJAX `/adc`).

**Nudge** (bounded writes; supervisor stays authoritative)
- `prioritize_band(band, weight)` — bias scoring.
- `pin_frequency(freq_hz, dwell_s)` — force a channel onto a freq.
- `pause_channel(ch) / resume_channel(ch)`.
- `request_deep_dwell(freq_hz, seconds)` — speculative weak-signal capture.

**TX / safety-adjacent**
- `notify_tx(active: bool)` — mark capture windows contaminated, gate assignment.
- `mark_window_contaminated(start_ts, end_ts, reason)`.
- `set_antenna(n) / ground_antenna()` — `ant_switch` EXT; **secondary**, not the interlock.

## 10. Resilience (why this is Model B)

- Collector + capture run as one long-lived asyncio service; **MCP is a separate concern**.
- Decoupling boundary is the **catalog DB**: MCP writes nudges to a table/queue, the
  supervisor polls it. Kill MCP → collection continues. Kill the agent → collection
  continues. (Skeleton uses an in-memory `ControlBus`; production = DB-backed for full
  process isolation.)
- Capture workers auto-reconnect WS on drop; supervisor re-derives roster from state.
- Everything durable in SQLite so a process restart resumes coherently.

## 11. Open questions — verify when hardware arrives

- Real `wf_chans` / `rx_chans` from the FPGA signature; confirm 13+13 without `wf_share`.
- Exact `/SND` IQ handshake: audio-rate negotiation (`SET AR OK …`), keepalive cadence,
  channel sample rate, IQ frame format/endianness.
- `/W/F` frame format, achievable `wf_speed` fps, `cf` units (Hz) vs `start` (bins),
  `zoom→span` constant (`ui_srate`).
- Auth: `SET auth t=kiwi|admin p=…` flow and whether 13 concurrent local sessions need a
  password / hit connection limits.
- IQ passband control: how `low_cut`/`high_cut` interact with `mod=iq` decimation.

## 12. Roadmap

1. **M0 — read-only proof:** sidecar connects, `get_sdr_status` / `get_activity_map` work
   against one `/W/F` band. No capture yet.
2. **M1 — single capture:** detect → assign one channel → record IQ + metadata to SQLite.
3. **M2 — full supervisor:** 13-channel scheduling, dwell/release, coverage debt.
4. **M3 — TX hygiene:** PTT ingest + contamination marking.
5. **M4 — agent nudges:** wire the bounded-write tools to the control bus.
6. **M5 — scale + storage lifecycle:** SigMF, cold storage, year-run hardening.
