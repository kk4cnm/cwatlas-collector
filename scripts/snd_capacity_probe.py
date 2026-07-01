#!/usr/bin/env python3
"""How many simultaneous /SND IQ streams will the device actually serve?

Opens N connections (staggered 0.5s), each with the full validated handshake on
a distinct frequency, streams for `secs`, reports per-connection frame counts
and any MSG hints (too_busy etc). Run after ~90s of device quiet so lingering
channel holds don't skew the answer.

Usage: python snd_capacity_probe.py <host> [n_conns] [secs]
"""
import asyncio
import sys
import time

import websockets


async def one(host: str, idx: int, freq_khz: float, secs: float, results: dict):
    url = f"ws://{host}/{int(time.time())+idx}/SND"
    frames, msgs = 0, []
    try:
        async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
            await ws.send("SET auth t=kiwi p=")
            await ws.send(f"SET mod=iq low_cut=750 high_cut=1250 freq={freq_khz - 1.0:.3f}")
            await ws.send("SET agc=0 hang=0 thresh=-130 slope=6 decay=1000 manGain=60")
            await ws.send("SET AR OK in=12000 out=12000")
            await ws.send("SET compression=0")
            await ws.send("SET keepalive")
            end = time.time() + secs
            while time.time() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
                except asyncio.TimeoutError:
                    break
                b = m if isinstance(m, bytes) else m.encode("latin1")
                if b[:3] == b"SND":
                    frames += 1
                elif b[:3] == b"MSG":
                    t = b[3:].decode("latin1", "replace").strip()
                    for k in ("too_busy", "redirect", "down", "badp", "rx_chan"):
                        if k in t:
                            msgs.append(t[:70])
        results[idx] = (freq_khz, frames, "closed_ok", msgs[:3])
    except websockets.exceptions.ConnectionClosed as e:
        results[idx] = (freq_khz, frames, f"CLOSED by server ({e.code})", msgs[:3])
    except Exception as e:
        results[idx] = (freq_khz, frames, f"error {e!r}", msgs[:3])


async def main(host, n, secs):
    print(f"opening {n} SND conns on {host}, {secs}s each, 0.5s stagger\n")
    results: dict = {}
    tasks = []
    for i in range(n):
        freq = 14000.0 + i * 5.0   # spread across 20m
        tasks.append(asyncio.create_task(one(host, i, freq, secs, results)))
        await asyncio.sleep(0.5)
    await asyncio.gather(*tasks)
    ok = 0
    for i in sorted(results):
        freq, frames, status, msgs = results[i]
        good = frames > 10
        ok += good
        print(f"conn{i:2d} {freq:9.1f} kHz : {frames:4d} SND frames  "
              f"{'STREAMING' if good else 'DEAD'}  [{status}]"
              + (f"  msgs={msgs}" if msgs else ""))
    print(f"\n=> {ok}/{n} connections actually streamed")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.46:8073"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 11
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    asyncio.run(main(host, n, secs))
