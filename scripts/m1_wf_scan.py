#!/usr/bin/env python3
"""M1 search-plane probe: ONE pooled /W/F connection, retuned in place across the
HF CW watering holes. Uses `SET wf_comp=0` (found in rx_cmd.cpp) so frames are RAW
1024-byte dBm bins even at fine zoom — no IMA-ADPCM needed.

Wire facts (firmware rx_waterfall.cpp/h, v2026.609):
  * bin byte encodes dBm: 0..-200 dBm -> 255..55, i.e. dBm = byte - 255
  * header x_bin_server = absolute start bin at this zoom:
      freq_hz(i) = (x_bin_server + i) * bin_hz,  bin_hz = ui_srate / (1024 * 2^zoom)
  * zoom 10 -> 60 kHz span, ~58.6 Hz/bin (near-ideal CW predetection BW)

Usage: python m1_wf_scan.py <host> [frames_per_dwell]
Single connection, retune in place — NEVER one connection per dwell (channel holds
linger ~1 min after close and starve the device).
"""
import asyncio
import statistics
import struct
import sys
import time

import websockets

UI_SRATE = 61_440_000.0
ZOOM = 10
MAX_ZOOM = 14
BINS = 1024
BIN_HZ = UI_SRATE / (BINS * (1 << ZOOM))       # ~58.6 Hz
SPAN_HZ = UI_SRATE / (1 << ZOOM)               # 60 kHz
# x_bin_server is in MAX_ZOOM-bin units (HZperStart), NOT current-zoom bins
# (verified on hw: values were exactly 2^(MAX_ZOOM-zoom) x too large otherwise)
HZ_PER_START = UI_SRATE / (BINS * (1 << MAX_ZOOM))   # ~3.66 Hz
HDR = 16                                       # id4 + x_bin_server + flags_x_zoom + seq
WF_FLAGS_COMPRESSION = 0x00010000

# CW watering holes: (band, dwell center kHz). One zoom-10 view = 60 kHz.
DWELLS = [
    ("160m", 1820.0),
    ("80m",  3530.0),
    ("40m",  7030.0),
    ("30m", 10115.0),
    ("20m", 14030.0),
    ("17m", 18081.0),
    ("15m", 21030.0),
    ("12m", 24905.0),
    ("10m", 28030.0),
]


def decode(frame: bytes):
    """-> (start_hz, [dBm]*1024) or None if compressed/short."""
    if len(frame) < HDR + BINS:
        return None
    x_bin, flags_zoom = struct.unpack_from("<II", frame, 4)
    if flags_zoom & WF_FLAGS_COMPRESSION:
        return None  # shouldn't happen with wf_comp=0
    dbm = [b - 255 for b in frame[HDR:HDR + BINS]]
    return x_bin * HZ_PER_START, dbm


def find_peaks(avg, start_hz, floor_db=10.0):
    """Bins > median+floor_db, grouped into contiguous signals -> [(kHz, dBm, width_bins)]."""
    floor = statistics.median(avg)
    thresh = floor + floor_db
    peaks, k = [], 0
    while k < BINS:
        if avg[k] > thresh:
            j = k
            while j < BINS and avg[j] > thresh:
                j += 1
            best = max(range(k, j), key=lambda m: avg[m])
            peaks.append(((start_hz + (best + 0.5) * BIN_HZ) / 1e3, avg[best], j - k))
            k = j
        else:
            k += 1
    return floor, peaks


async def main(host, per_dwell):
    ts = int(time.time())
    url = f"ws://{host}/{ts}/W/F"
    print(f"one pooled connection: {url}  zoom={ZOOM} span={SPAN_HZ/1e3:.0f}kHz "
          f"bin={BIN_HZ:.1f}Hz wf_comp=0\n")
    # ping_interval=None: firmware doesn't answer WS-level pings (library would
    # close with "keepalive ping timeout"); app-level SET keepalive suffices.
    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        await ws.send("SET auth t=kiwi p=")
        # W/F streams only once cmd_recv == CMD_ALL = ZOOM|START|DB|SPEED
        # (rx_waterfall.cpp:659). zoom+cf sets ZOOM|START; the others:
        await ws.send("SET maxdb=-10 mindb=-110")  # -> CMD_DB
        await ws.send("SET wf_speed=4")            # -> CMD_SPEED
        await ws.send("SET wf_comp=0")   # raw bins at any zoom (rx_cmd.cpp:1979)
        for band, cf_khz in DWELLS:
            # NB: firmware parses cf in *kHz* (rx_waterfall.cpp: `cf *= kHz`)
            await ws.send(f"SET zoom={ZOOM} cf={cf_khz:.3f}")
            await ws.send("SET keepalive")
            want_start = cf_khz * 1e3 - SPAN_HZ / 2
            acc, n, n_stale, first_start = [0.0] * BINS, 0, 0, None
            mx = [-999.0] * BINS   # max-hold: CW is keyed on/off, averaging dilutes it
            deadline = time.time() + 12.0
            while n < per_dwell and time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
                except asyncio.TimeoutError:
                    break
                b = raw if isinstance(raw, bytes) else raw.encode("latin1")
                if b[:3] != b"W/F":
                    continue
                d = decode(b)
                if d is None:
                    continue
                start_hz, dbm = d
                if first_start is None:
                    first_start = start_hz
                # drop stale frames from the previous tune (retune in place => the
                # first frame(s) after SET cf may still carry the old x_bin_server)
                if abs(start_hz - want_start) > SPAN_HZ / 4:
                    n_stale += 1
                    continue
                for k in range(BINS):
                    acc[k] += dbm[k]
                    if dbm[k] > mx[k]:
                        mx[k] = dbm[k]
                n += 1
                start = start_hz
            if not n:
                fs = f"{first_start/1e3:.1f}kHz" if first_start is not None else "none"
                print(f"{band:>5} {cf_khz:>9.1f} kHz : no frames "
                      f"(stale={n_stale}, first x_bin start={fs}, want={want_start/1e3:.1f}kHz)")
                continue
            floor, peaks = find_peaks(mx, start)   # max-hold catches keyed CW
            lo, hi = min(mx), max(mx)
            blocks = " ▁▂▃▄▅▆▇█"
            step = BINS // 96
            spark = "".join(
                blocks[min(8, int((max(mx[m:m + step]) - lo) / (hi - lo + 1e-9) * 8))]
                for m in range(0, BINS - step, step))
            print(f"{band:>5} {cf_khz:>9.1f} kHz : {n} frames, max-hold floor={floor:.0f}dBm, "
                  f"{len(peaks)} signals >floor+10dB")
            print(f"      {spark}")
            for khz, db, w in sorted(peaks, key=lambda p: -p[1])[:8]:
                print(f"        {khz:10.2f} kHz  {db:6.1f} dBm  ~{w * BIN_HZ:.0f} Hz wide")
    print("\ndone (single connection closed cleanly)")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.46:8073"
    per_dwell = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    asyncio.run(main(host, per_dwell))
