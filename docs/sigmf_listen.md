# sigmf_listen — audition CWAtlas captures by ear

`scripts/sigmf_listen.py` renders a `.sigmf-data` capture as a normal 16-bit WAV so you
can spot-check by ear that the collector is recording real, clean CW. It also queries
`catalog.db` so you can pick *which* captures are worth auditioning instead of opening
files at random.

## Why captures don't play directly

A `.sigmf-data` file is not audio. It is headerless complex IQ — interleaved little-endian
int16 I/Q pairs (`ci16_le`) at a **1500 Hz** sample rate, with the CW carrier parked near
**+250 Hz** in baseband (the device passband 750–1250 Hz is shifted −750 Hz and decimated
12 k→1.5 k on capture). All of that lives in the companion `.sigmf-meta` JSON. A media
player fed the raw file guesses an ordinary PCM layout and rate, so it plays noise.

To make it listenable the tool:

1. reads format, sample rate, and carrier offset from the `.sigmf-meta`;
2. upsamples the complex signal by FFT zero-padding (exact for a band-limited signal,
   numpy only — no scipy dependency);
3. mixes the carrier from its baseband offset to an audible sidetone pitch
   (default 600 Hz) and takes the real part;
4. normalizes and writes a mono 16-bit WAV (default 12 kHz) next to the capture, so it
   can be played in VLC/QuickTime straight off the network share.

## Usage

```bash
# List the N strongest clean captures (contaminated / in-flight rows excluded)
.venv/bin/python scripts/sigmf_listen.py --top 20
.venv/bin/python scripts/sigmf_listen.py --top 20 --band 40m --date 2026-07-04 --min-conf 0.9

# Convert by catalog id (path looked up in catalog.db)
.venv/bin/python scripts/sigmf_listen.py --id 1902

# Convert by path — basename, .sigmf-data, or .sigmf-meta all accepted
.venv/bin/python scripts/sigmf_listen.py /mnt/md0/cwatlas/data/2026-07-04/40m_7003.95kHz_20260704T002444Z_ch4

# Options
#   --pitch HZ   sidetone pitch (default 600)
#   --rate HZ    output sample rate (default 12000)
#   -o PATH      output wav path (default: <capture basename>.wav beside the capture)
```

The `--top` listing shows id, band, kHz, detection strength (dB), keyed confidence, and
duration, ordered by strength — a quick menu of what to listen to.

## What "good" sounds like

A healthy capture is a single clean keyed tone at the chosen pitch with silence between
elements. First verification (capture id 1902, 40 m, 60 dB) showed a ~614 Hz dominant
tone with key-down run lengths clustering at ~40–80 ms and ~160–200 ms — the classic
1:3 dit/dah ratio at roughly 25 WPM.

Notes:

- **A few Hz of pitch offset is expected.** The detector's bin center is rarely exactly
  on the transmitter's carrier, so the tone lands near — not exactly at — `--pitch`.
- **Per-file normalization**: levels are scaled to 0.9 peak per file, so loudness does
  not compare across captures; use `strength_db` from the catalog for that.
- Only `ci16_le` captures are supported (the only datatype the collector writes).

## Quick one-liner alternative

Without the script, `sox` can render the raw IQ audibly by treating it as 2-channel PCM
and keeping the I channel — the tone plays at its native ~250 Hz (low, but audible):

```bash
sox -t raw -r 1500 -e signed -b 16 -c 2 file.sigmf-data -r 12k out.wav remix 1
```

The script's complex mix to 600 Hz is more comfortable and avoids folding other signals
in the passband on top of the target.
