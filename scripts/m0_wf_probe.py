#!/usr/bin/env python3
"""M0 read-only probe: open a Web-888 /W/F WebSocket, capture the authoritative
channel counts (the 'wf_setup' MSG) and confirm waterfall frames flow.

Usage: python m0_wf_probe.py <host> [center_hz] [zoom]
Read-only: only SET auth / zoom / wf_speed are sent; nothing is configured.
"""
import asyncio
import sys
import time

import websockets


async def probe(host: str, center_hz: int, zoom: int) -> None:
    ts = int(time.time())
    url = f"ws://{host}/{ts}/W/F"
    print(f"connecting: {url}  (cf={center_hz} Hz, zoom={zoom})")
    async with websockets.connect(url, max_size=None) as ws:
        # KiwiSDR handshake: authenticate, then request a waterfall view.
        await ws.send("SET auth t=kiwi p=")
        await ws.send(f"SET zoom={zoom} cf={center_hz}")
        await ws.send("SET wf_speed=4")

        text_msgs: list[str] = []
        binary_frames = 0
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                binary_frames += 1
            else:
                text_msgs.append(msg)
                # The setup line carries rx_chans / wf_chans / wf_chans_real.
                if "wf_setup" in msg or "rx_chans" in msg:
                    print(f"  >> {msg}")

        print(f"\ntext messages: {len(text_msgs)}, binary (waterfall) frames: {binary_frames}")
        for m in text_msgs:
            if "wf_setup" not in m and "rx_chans" not in m:
                print(f"  -- {m}")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.46:8073"
    cf = int(sys.argv[2]) if len(sys.argv) > 2 else 10_125_000   # 30m CW band center
    zoom = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    asyncio.run(probe(host, cf, zoom))
