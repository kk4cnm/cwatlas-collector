"""M3: operator-TX ingest — FlexRadio SmartSDR TCP status -> Supervisor.set_tx().

The hardware antenna-disconnect relay (keyed by the Flex amp-key line, 60 ms TX
delay) is the ACTUAL front-end protection. This module is data hygiene only:
while the operator transmits, the supervisor flags in-flight capture files
contaminated and holds new channel assignments. If the Flex is off / unreachable
the worker fails OPEN (tx=False) — collection must never freeze because the
radio went to sleep; the relay still mutes contaminated audio at the antenna.

SmartSDR TCP API notes (port 4992, line-oriented ASCII):
    on connect the radio sends   V<protocol-version>  then  H<client-handle>
    commands are                 C<seq>|<cmd>          replies R<seq>|<code>|...
    "sub tx all" subscribes to transmit/interlock status lines like
        S<handle>|interlock state=TRANSMITTING reason=... source=...
`state` is the radio's TX state machine. Anything outside the known
receive-side states is treated as transmitting — unknown states contaminate
a little extra data rather than let TX leak into the training set.

Discovery: the radio broadcasts a VITA-49 packet on UDP 4992 ~1/s whose ASCII
payload carries model=/ip=/callsign= (validated live against the 6600).
"""
from __future__ import annotations

import asyncio
import re
import socket
import time

FLEX_PORT = 4992
RX_STATES = {"NONE", "RECEIVE", "READY", "NOT_READY"}
PING_EVERY_S = 10.0

_IP_RE = re.compile(r"\bip=(\d+\.\d+\.\d+\.\d+)")
_STATE_RE = re.compile(r"\|interlock .*?\bstate=(\w+)")


def _discover_sync(timeout_s: float) -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", FLEX_PORT))
        s.settimeout(timeout_s)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            data, addr = s.recvfrom(2048)
            txt = data.decode("ascii", "ignore")
            m = _IP_RE.search(txt)
            if m:
                return m.group(1)
            if "FLEX" in txt:          # payload present but no ip= field: trust UDP source
                return addr[0]
    except (socket.timeout, OSError):
        pass
    finally:
        s.close()
    return None


async def discover_flex(timeout_s: float = 6.0) -> str | None:
    """Listen for the radio's UDP discovery broadcast; returns its IP or None."""
    return await asyncio.get_running_loop().run_in_executor(
        None, _discover_sync, timeout_s)


async def flex_ptt_worker(sup, host: str, port: int = FLEX_PORT,
                          reconnect_s: float = 15.0,
                          unkey_hold_s: float = 1.0) -> None:
    """Track the Flex interlock state forever; drive sup.set_tx() on transitions.

    unkey_hold_s: keep tx asserted briefly after unkey so relay switch-back
    transients don't land in a "clean" capture window.
    """
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            print(f"[ptt] flex connect {host}:{port} failed ({exc!r}); "
                  f"retrying in {reconnect_s:.0f}s")
            await asyncio.sleep(reconnect_s)
            continue
        tx = False
        try:
            writer.write(b"C1|sub tx all\n")
            await writer.drain()
            print(f"[ptt] connected to Flex at {host}:{port}, subscribed to tx status")
            seq, last_ping = 2, time.time()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                except asyncio.TimeoutError:
                    line = b""
                else:
                    if not line:            # EOF: radio closed the connection
                        raise ConnectionResetError("flex closed connection")
                if time.time() - last_ping > PING_EVERY_S:
                    writer.write(f"C{seq}|ping\n".encode())
                    await writer.drain()
                    seq, last_ping = seq + 1, time.time()
                m = _STATE_RE.search(line.decode("ascii", "ignore"))
                if not m:
                    continue
                now_tx = m.group(1).upper() not in RX_STATES
                if now_tx and not tx:
                    tx = True
                    sup.set_tx(True)
                    print(f"[ptt] TX (interlock {m.group(1)}) — captures flagged, "
                          f"assignments held")
                elif tx and not now_tx:
                    await asyncio.sleep(unkey_hold_s)   # ride out relay switch-back
                    tx = False
                    sup.set_tx(False)
                    print(f"[ptt] RX (interlock {m.group(1)})")
        except (OSError, ConnectionResetError, asyncio.IncompleteReadError) as exc:
            print(f"[ptt] flex link lost ({exc!r}); reconnecting in {reconnect_s:.0f}s")
        finally:
            if tx:
                sup.set_tx(False)   # fail open: never freeze collection on a dead link
            writer.close()
        await asyncio.sleep(reconnect_s)
