"""Async client for the Web-888 (KiwiSDR-protocol) SDR server.

Two transports:
  * HTTP/AJAX  — stateless info plane: /status, /snr, /adc, /users, ...
  * WebSocket  — stateful per-channel control plane: /W/F (search), /SND (capture), /EXT

NB: Several protocol details are marked [verify on hw] — the exact IQ/waterfall frame
formats, audio-rate handshake, keepalive cadence and command units must be confirmed
against the real device. This is a skeleton: method bodies that talk binary frames are
stubbed with TODOs so the structure compiles and the shape is reviewable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx          # AJAX
import websockets     # WS control plane


@dataclass
class SdrConfig:
    host: str = "web-888.local"
    port: int = 8073
    password: str = ""          # SET auth t=kiwi p=...
    auth_type: str = "kiwi"     # "kiwi" | "admin"
    ui_srate_hz: float = 61_440_000.0   # [verify on hw] full front-end span
    snd_rate_hz: int = 12_000           # [verify on hw] channel IQ sample rate


class SdrClient:
    def __init__(self, cfg: SdrConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=f"http://{cfg.host}:{cfg.port}", timeout=5.0
        )

    # ---- AJAX info plane -------------------------------------------------
    async def get_status(self) -> dict:
        """GET /status -> parsed key=value status block."""
        r = await self._http.get("/status")
        r.raise_for_status()
        return _parse_kv(r.text)

    async def get_adc(self) -> dict:
        """GET /adc -> ADC overload / level info (ov_mask)."""
        r = await self._http.get("/adc")
        r.raise_for_status()
        return _parse_kv(r.text)

    async def get_snr(self) -> dict:
        r = await self._http.get("/snr")
        r.raise_for_status()
        return _parse_kv(r.text)

    async def get_users(self) -> dict:
        r = await self._http.get("/users")
        r.raise_for_status()
        return r.json()

    # ---- WS helpers ------------------------------------------------------
    def _ws_url(self, stream: str) -> str:
        ts = int(time.time())
        return f"ws://{self.cfg.host}:{self.cfg.port}/{ts}/{stream}"

    async def _open_authed(self, stream: str):
        ws = await websockets.connect(self._ws_url(stream), max_size=None)
        await ws.send(f"SET auth t={self.cfg.auth_type} p={self.cfg.password}")
        # TODO[verify]: read/await the auth reply (MSG badp=...) before proceeding.
        return ws

    # ---- Search plane: waterfall ----------------------------------------
    async def waterfall_stream(
        self, center_hz: float, zoom: int, fps: int = 3
    ) -> AsyncIterator["WfFrame"]:
        """Open a /W/F session and yield magnitude frames (1024 bins).

        span ≈ ui_srate / 2^zoom; resolution ≈ span / 1024.
        """
        ws = await self._open_authed("W/F")
        await ws.send(f"SET zoom={zoom} cf={center_hz:.0f}")   # cf in Hz [verify]
        await ws.send(f"SET wf_speed={fps}")
        await ws.send("SET maxdb=-10 mindb=-110")
        try:
            async for raw in ws:
                # TODO[verify on hw]: decode WF binary frame -> 1024 magnitude bins.
                yield _decode_wf_frame(raw, center_hz, zoom, self.cfg.ui_srate_hz)
        finally:
            await ws.close()

    # ---- Capture plane: IQ sound ----------------------------------------
    async def capture_iq(
        self, freq_hz: float, half_bw_hz: float = 200.0
    ) -> "IqSession":
        """Open a /SND session in IQ mode, narrowed to ±half_bw_hz around freq."""
        ws = await self._open_authed("SND")
        freq_khz = freq_hz / 1000.0
        await ws.send(
            f"SET mod=iq low_cut={-half_bw_hz:.0f} high_cut={half_bw_hz:.0f} "
            f"freq={freq_khz:.3f} param=0"
        )
        await ws.send("SET compression=0")
        # TODO[verify]: audio-rate handshake, e.g. "SET AR OK in=12000 out=12000".
        await ws.send("SET keepalive")
        return IqSession(ws, freq_hz, self.cfg.snd_rate_hz)

    async def set_antenna(self, n: int) -> None:
        """ant_switch EXT: n=0 grounds all inputs. SECONDARY, not the TX interlock."""
        ws = await self._open_authed("EXT")
        await ws.send("SET ext_switch_to_client=ant_switch first_time=1")
        await ws.send(f"SET Antenna={n}")
        await ws.close()

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass
class WfFrame:
    center_hz: float
    bin_hz: float
    bins: list  # length 1024, magnitude (dB)
    ts: float


class IqSession:
    """Holds an open /SND IQ WebSocket. Iterate frames; caller writes them to disk."""

    def __init__(self, ws, freq_hz: float, srate_hz: int):
        self.ws = ws
        self.freq_hz = freq_hz
        self.srate_hz = srate_hz

    async def frames(self) -> AsyncIterator[bytes]:
        try:
            async for raw in self.ws:
                # TODO[verify on hw]: strip 'SND' framing/seq header, return complex IQ.
                yield raw
        finally:
            await self.ws.close()

    async def close(self) -> None:
        await self.ws.close()


# ---- parsing helpers (stubs) --------------------------------------------
def _parse_kv(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _decode_wf_frame(raw, center_hz, zoom, ui_srate_hz) -> WfFrame:
    span = ui_srate_hz / (1 << zoom)
    bin_hz = span / 1024.0
    # TODO[verify on hw]: real WF frame decode (compressed magnitude bytes -> dB).
    return WfFrame(center_hz=center_hz, bin_hz=bin_hz, bins=[], ts=time.time())
