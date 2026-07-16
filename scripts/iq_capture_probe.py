#!/usr/bin/env python3
"""M1 capture-plane probe: open ONE /SND IQ session on a known signal, decode raw
int16 I/Q (uncompressed in IQ mode), and render the power envelope so CW keying is
visible. Grounded in firmware rx_sound.h snd_pkt_iq_t (20-byte header + int16 IQ).

Usage: python iq_capture_probe.py <host> <freq_khz> [secs]
Single connection, no churn.
"""
import asyncio
import math
import statistics
import struct
import sys
import time

import websockets

IQ_HDR = 20  # id[3]+flags[1]+seq[4]+smeter[2]+gps[1+1+4+4]


async def main(host, freq_khz, secs):
    ts = int(time.time())
    url = f"ws://{host}/{ts}/SND"
    iq = bytearray()
    smeters = []
    async with websockets.connect(url, max_size=None) as ws:
        # Audio streams only once cmd_recv == CMD_ALL (FREQ|MODE|PASSBAND|AGC|AR_OK).
        await ws.send("SET auth t=kiwi p=")
        # CW capture: tune 1 kHz low so the carrier lands at ~+1000 Hz in the IQ
        # baseband (clear of DC), passband 500..1500 Hz. Keeps the keying envelope clean.
        await ws.send(f"SET mod=iq low_cut=500 high_cut=1500 freq={freq_khz - 1.0:.3f}")  # FREQ|MODE|PASSBAND
        # AGC OFF (agc=0 = manual gain): preserves the CW keying envelope. agc=1 would
        # normalize amplitude and erase the on/off signature we need. Still sets CMD_AGC.
        await ws.send("SET agc=0 hang=0 thresh=-130 slope=6 decay=1000 manGain=60")
        await ws.send("SET AR OK in=12000 out=12000")  # AR_OK -> unlocks audio
        await ws.send("SET compression=0")
        await ws.send("SET keepalive")
        end = time.time() + secs
        nframes = 0
        while time.time() < end:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
            except asyncio.TimeoutError:
                break
            b = m if isinstance(m, bytes) else m.encode("latin1")
            if b[:3] != b"SND":
                continue
            nframes += 1
            smeters.append(struct.unpack_from(">H", b, 8)[0])
            iq += b[IQ_HDR:]
    if not nframes:
        print("no SND frames (device busy / channel starved) — back off and retry later")
        return

    n = len(iq) // 4  # int16 I + int16 Q = 4 bytes/sample
    samples = struct.unpack_from(f"<{n*2}h", iq, 0)
    i = samples[0::2]
    q = samples[1::2]
    # remove DC offset so power reflects signal energy, not a constant bias
    mi = statistics.mean(i)
    mq = statistics.mean(q)
    i = [x - mi for x in i]
    q = [x - mq for x in q]
    srate = 12000  # confirmed: 71680 samples / ~6 s ≈ 12 kHz
    print(f"{nframes} SND/IQ frames, {n} IQ samples (~{n/srate:.1f}s @ {srate}Hz), "
          f"smeter raw avg={statistics.mean(smeters):.0f}")

    # power envelope in ~50 ms windows -> sparkline reveals CW on/off keying
    win = max(1, srate // 20)
    env = []
    for k in range(0, n - win, win):
        p = sum(i[j] * i[j] + q[j] * q[j] for j in range(k, k + win)) / win
        env.append(10 * math.log10(p + 1e-9))
    lo, hi = min(env), max(env)
    blocks = " ▁▂▃▄▅▆▇█"
    spark = "".join(blocks[min(8, int((e - lo) / (hi - lo + 1e-9) * 8))] for e in env)
    print(f"power envelope ({len(env)} x 50ms): floor={lo:.0f}dB peak={hi:.0f}dB range={hi-lo:.0f}dB")
    print(spark)
    print("VERDICT:", "CW KEYING CAPTURED (envelope swings on/off)" if (hi - lo) > 6
          else "weak/steady — little signal in passband")


def _default_host() -> str:
    """The SDR from config.toml / $CWATLAS_SDR_HOST, so this script carries no
    one else's LAN address. Pass <host> explicitly to override."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cwatlas_mcp import config
    cfg = config.load()
    host = config.pick(cfg, "sdr.host", "CWATLAS_SDR_HOST")
    port = config.pick(cfg, "sdr.port", default=8073)
    if not host:
        raise SystemExit(f"usage: {Path(__file__).name} <host[:port]> ...  "
                         "(or set [sdr] host in config.toml)")
    return f"{host}:{port}"


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else _default_host()
    fkhz = float(sys.argv[2]) if len(sys.argv) > 2 else 28026.13
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    asyncio.run(main(host, fkhz, secs))
