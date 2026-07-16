# CWAtlas Session Journal — First Light to First Soak

**Dates:** 2026-06-21 → 2026-07-02
**People:** Daniel (KK4CNM) + Claude (Opus 4.8 / Fable)
**Repos:** `web-888_server_os` (firmware fork), `collector` (this repo)

A curated narrative of the sessions that took CWAtlas from a code review to an
autonomous overnight collector. Written for posterity — future contributors
(human or model) should read this before touching the collector's connection
or scheduling logic, because every rule in here was paid for with a failed
live trial.

---

## 1. Origins (2026-06-21)

Started as a security review of the Web-888 SDR server codebase (a KiwiSDR
fork). Found and de-hardcoded a leaked timezonedb.com API key
(`net/services.cpp`, branch `security/dehardcode-tzdb-key`), later adding the
admin-UI field that made the fix complete.

The review became a design conversation: **CWAtlas** — a 24/7 autonomous
system monitoring amateur HF for CW, recording raw IQ into a labeled corpus to
train **MorseBase**, an LLM that decodes Morse like a skilled human operator.
Target: ~1+ year of collection. The Web-888 was chosen for its ~13 simultaneous
receive channels and 61.44 MHz direct-sampling front end.

Architecture decided that day (all confirmed by hardware later):

- **Two planes.** SEARCH = waterfall channels (wideband activity map);
  CAPTURE = RX channels in IQ mode, narrowed onto detected signals.
- **Model B autonomy.** A deterministic, LLM-free supervisor owns all channel
  decisions. The MCP sidecar *observes* state and enqueues bounded *nudges* —
  collection must survive the agent being down.
- **MCP is control plane only.** Bulk IQ flows over direct WebSockets;
  never through tool calls.
- **Raw IQ capture** (`mod=iq`, `compression=0`, AGC off). On-device CW
  decoders are weak-label helpers only.
- **TX protection is hardware.** A Flex 6600 shares the antenna field; the
  interlock is a relay keyed by the amp-key line (60 ms TX delay for
  sequencing). The collector's TX role is *data hygiene*: flag contaminated
  capture windows.

## 2. Hardware arrival & M0 (2026-06-27)

Board arrived (smelling of mothballs). Learned the hard way that the vendor
image is **loose files on FAT32/MBR**, not a dd image — balenaEtcher's "no
boot volume" complaint was the tell. Flashed alpha 20260609, booted, DHCP'd
at 192.168.2.46:8073, 12 channels reported (not the marketed 13 — read
`rx_chans` at runtime).

Protocol reverse-engineered from firmware source: every WS frame is binary
with a 3-byte ASCII tag (`MSG`/`W/F`/`SND`); `MSG` payloads are urlencoded
key=val; auth is connection-bound (`SET auth t=kiwi p=`).

**First reception** that evening (ARRL Field Day): CW at 28026.13 kHz heard
through a too-short VHF attic antenna — 10 m couples best on a mismatched
short antenna.

**First capture:** after fixing three things in sequence —
1. the SND stream is gated on `cmd_recv == CMD_ALL` (FREQ|MODE|PASSBAND|AGC|
   AR_OK) — "no frames" was a missing `SET AR OK`, not device failure;
2. AGC must be OFF (`agc=0`) or amplitude normalization erases the keying
   envelope — the very training signal;
3. offset-tune CW 1 kHz low so the tone sits at ~+1 kHz baseband, clear of DC —
we captured real Field Day keying (12 dB envelope) on 28028.28 kHz.

## 3. Search plane & the first autonomous handoff (2026-07-01)

HF antenna up, TX interlock installed. Four more protocol landmines found and
defused (all now encoded in `sdr_client.py`):

1. **W/F has its own CMD_ALL gate** (`rx_waterfall.cpp:659`):
   ZOOM|START|DB|SPEED. Omit `SET maxdb/mindb` and you get silence, no error.
   (This also revealed our M0 "waterfall works" was mis-validated — it had
   counted binary MSG frames as waterfall frames.)
2. **`SET zoom=N cf=F` takes kHz**, not Hz (`cf *= kHz` in firmware).
3. **`SET wf_comp=0` disables waterfall compression at any zoom** — raw
   1024-byte dBm bins (dBm = byte − 255), killing the planned IMA-ADPCM
   decoder outright. ~58 Hz/bin at zoom 10 is a near-ideal CW search
   resolution.
4. **`x_bin_server` is in MAX_ZOOM-bin units** (~3.66 Hz each), not
   current-zoom bins. Also: the firmware ignores WS-level pings — pass
   `ping_interval=None` or the client library kills healthy sockets at ~40 s.

With those fixed, `scripts/m1_wf_scan.py` swept all 9 HF CW watering holes on
ONE pooled connection and detected CW at **14026.22 kHz** — which
`iq_capture_probe.py` then captured (17 dB keying envelope). **The first
detect→tune→record loop with no human-chosen frequency.**

## 4. M2: the supervisor loop, and everything it taught us (2026-07-01)

Built: `detector.py` (max-hold SNR + ON/OFF transition keying signature),
`catalog.py` (SQLite), `capture.py` (channel workers → SigMF), rewired
`scheduler.py` and `runtime.py`. Three live trials, each finding a real bug:

- **Trial 1 (154 rows in 3 min, mostly 0-sample):** per-capture SND
  connections starved the device — closed connections hold their channel
  ~1 min server-side. → **Persistent channel workers that retune in place.**
  Also: scheduler timeouts must exceed the scanner's ~2 min band-revisit
  period, or release/reassign ping-pongs.
- **Trial 2 (channels died and never recovered):** `asyncio.wait_for` around
  an async generator's `__anext__` cancels it mid-`recv`; the generator's
  `finally` then **silently closes the WebSocket**. → explicit cancel-safe
  `next_chunk()`. Plus a worker/supervisor state race (workers may only
  self-release slots they still own).
- **Trial 3 (only ~5 of 9 streams alive):** burst connection opens get dropped
  by the server even below capacity, and correlated 65 s retries kept the
  starvation alive. → **pace all WS opens ~1 s apart** (global lock) +
  decorrelated backoffs. `snd_capacity_probe.py` proved the device happily
  serves 11 simultaneous full-rate IQ streams when opens are staggered.
- **Trial 4: flawless.** 9 continuous captures (350 s+ files), in-place
  retunes, zero errors.

## 5. Solar-aware band weighting (2026-07-01, Daniel's idea)

HF propagation follows the sun, so the collector should too: solar elevation
(NOAA approximation, `solar.py`) → day/gray/night per-band weights →
supervisor's `band_priority`. High bands weighted up by day and hard down at
night; low bands the reverse; gray-line boost at twilight. Weights bias
*capture assignment only* — the scanner still sweeps everything.

First live observation: weights alone don't stop closed-band junk when
channels are idle (9 daytime 160m carriers grabbed every slot). Fix:
**`min_capture_score`** — a candidate must *earn* a channel under current
weighting, not merely exist. Same signal: rejected at noon, eligible after
dark. Station grid EM83vk.

## 6. First overnight soak (2026-07-01 17:52 → 2026-07-02 05:52 EDT)

12 hours, **zero errors, zero connection failures**, clean shutdown.
378 real captures, 105 hours of channel-IQ, 18 GB. Band mix tracked
propagation: after the 21:23 night transition, 40 m (43 h) > 30 m (30 h) >
20 m > 80 m > 160 m, high bands correctly starved. Spot-checks found genuine
CW (e.g. 7030.61 at 159 transitions/min) alongside honest junk (steady
carriers that snuck past the detector).

Two findings drove the next round of work:

1. **Storage:** the SND stream is fixed at 12 kHz complex regardless of
   passband — our 500 Hz CW window is ~24× oversampled on disk; ~96% of every
   file was filter-suppressed noise. At 18 GB/night, a year is 15–35 TB.
2. **Channel hogs:** busy frequencies never go stale, so one 14 dB signal held
   a channel 4.4 hours.

## 7. The fix set (2026-07-02 morning)

- **Inline decimation** (`dsp.py`): mix −750 Hz (exactly 16 samples/cycle at
  12 kHz → table-lookup mixer, zero phase drift), 257-tap FIR, decimate 8× to
  1500 Hz. Carrier lands at +250 Hz. Validated: 0.0 dB in-band, −78 dB
  rejection, bit-exact across chunk boundaries. **18 GB/night → ~2.3 GB/night;
  a year ≈ 1 TB** — which fits comfortably on airig-01's 9 TB mirrored
  archival array (the planned production host).
- **File rotation:** 10-minute segments (capture continues seamlessly).
- **Max-dwell + cooldown:** 30 min cap per assignment; the released signal
  can't re-win a slot for 3 min, so the channel actually goes back into
  competition.

Verified live (4-min trial, 90 s rotation): exact 1500 Hz segments, seamless
rotation, real CW in the decimated files (carrier +167 Hz, 15.8 dB envelope,
434 transitions/90 s).

## 8. State of play

| Milestone | Status |
|---|---|
| M0 read-only hardware validation | ✅ 2026-06-27 |
| M1 search + capture planes validated | ✅ 2026-07-01 |
| M2 autonomous supervisor loop | ✅ 2026-07-01 |
| Solar weighting + score floor | ✅ 2026-07-01 |
| Decimation + rotation + max-dwell | ✅ 2026-07-02 |
| M3 PTT ingest (data hygiene) | next |
| M4 MCP tool surface validation | next |
| M5 storage lifecycle / airig-01 migration | planned |

**The cardinal rules (violate at your peril):**
1. Never churn connections — hold them and retune in place.
2. Every stream has a handshake gate; silence means *incomplete handshake*,
   not "broken device".
3. Scheduler time constants must respect the scan revisit period.
4. Pace connection opens; decorrelate retries.
5. The supervisor owns channel state; workers own files.
6. Capture raw (but decimated) IQ; judge signal quality offline, not in the
   capture path.
