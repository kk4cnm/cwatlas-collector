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
        # ping_interval=None: the firmware doesn't answer WS-level pings, so the
        # library would close the socket with "keepalive ping timeout" after ~40s.
        # Liveness is app-level: send "SET keepalive" periodically instead.
        ws = await websockets.connect(
            self._ws_url(stream), max_size=None, ping_interval=None
        )
        # Verified on hw (2026.609): empty password is accepted on a private SDR with
        # no user password set (server replies MSG badp=0).
        await ws.send(f"SET auth t={self.cfg.auth_type} p={self.cfg.password}")
        return ws

    async def read_config(self, stream: str = "W/F", timeout: float = 4.0) -> dict:
        """Connect and collect the MSG config the server emits on connect.

        Returns a flat dict including rx_chans, version_maj/min, center_freq,
        bandwidth, adc_clk_nom. Use rx_chans from here at runtime — this unit
        reports 12, not the marketed 13.
        """
        import asyncio

        cfg: dict = {}
        ws = await self._open_authed(stream)
        try:
            end = time.time() + timeout
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
                except asyncio.TimeoutError:
                    break
                tag, payload = split_frame(raw)
                if tag == "MSG":
                    cfg.update(parse_msg(payload))
        finally:
            await ws.close()
        return cfg

    # ---- Search plane: waterfall ----------------------------------------
    async def waterfall_stream(
        self, center_hz: float, zoom: int, fps: int = 3
    ) -> AsyncIterator["WfFrame"]:
        """Open a /W/F session and yield magnitude frames (1024 bins).

        span ≈ ui_srate / 2^zoom; resolution ≈ span / 1024.
        """
        ws = await self._open_authed("W/F")
        # W/F streams only once cmd_recv == CMD_ALL = ZOOM|START|DB|SPEED
        # (rx_waterfall.cpp:659) — omit maxdb/mindb and you get silence, no error.
        # NB: cf is parsed in *kHz* by the firmware (`cf *= kHz`), not Hz.
        await ws.send(f"SET zoom={zoom} cf={center_hz / 1e3:.3f}")  # -> ZOOM|START
        await ws.send("SET maxdb=-10 mindb=-110")                   # -> DB
        await ws.send(f"SET wf_speed={fps}")                        # -> SPEED
        # raw (uncompressed) bins at ANY zoom — avoids IMA-ADPCM entirely
        await ws.send("SET wf_comp=0")
        await ws.send("SET keepalive")
        try:
            async for raw in ws:
                frame = raw if isinstance(raw, bytes) else raw.encode("latin1")
                if frame[:3] == b"W/F":
                    # _decode_wf_frame parses the full frame (header + bins).
                    yield _decode_wf_frame(frame, center_hz, zoom, self.cfg.ui_srate_hz)
                # MSG/other tags carry config/keepalive; ignore here (see read_config()).
        finally:
            await ws.close()

    # ---- Capture plane: IQ sound ----------------------------------------
    async def capture_iq(
        self, freq_hz: float, half_bw_hz: float = 200.0
    ) -> "IqSession":
        """Open a /SND session in IQ mode, narrowed to ±half_bw_hz around freq."""
        ws = await self._open_authed("SND")
        # Audio streams only once cmd_recv == CMD_ALL = FREQ|MODE|PASSBAND|AGC|AR_OK
        # (rx_sound_cmd.h) — hw-validated sequence:
        # CW capture: tune 1 kHz low so the carrier sits at ~+1000 Hz in baseband
        # (clear of DC); passband tracks the requested half-bandwidth around it.
        freq_khz = (freq_hz - 1000.0) / 1000.0
        lo, hi = 1000.0 - half_bw_hz, 1000.0 + half_bw_hz
        await ws.send(
            f"SET mod=iq low_cut={lo:.0f} high_cut={hi:.0f} freq={freq_khz:.3f}"
        )  # -> FREQ|MODE|PASSBAND
        # AGC OFF (manual gain): agc=1 would normalize amplitude and erase the CW
        # keying envelope — the very signal MorseBase trains on.
        await ws.send("SET agc=0 hang=0 thresh=-130 slope=6 decay=1000 manGain=60")
        await ws.send(f"SET AR OK in={self.cfg.snd_rate_hz} out={self.cfg.snd_rate_hz}")
        await ws.send("SET compression=0")
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
    center_hz: float   # what we asked for (requested cf)
    start_hz: float    # authoritative: from x_bin_server; freq(i) = start_hz + (i+.5)*bin_hz
    bin_hz: float
    bins: list  # length 1024, dBm per bin
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
                tag, payload = split_frame(raw)
                if tag == "SND":
                    # TODO[next]: parse SND seq/flags header, return complex IQ samples.
                    yield payload
                # MSG frames carry keepalive/config; skip.
        finally:
            await self.ws.close()

    async def close(self) -> None:
        await self.ws.close()


# ---- frame + parsing helpers --------------------------------------------
def split_frame(raw) -> tuple[str, bytes]:
    """KiwiSDR wraps every WS frame as a 3-byte ASCII tag + payload.

    Tags seen on hw: 'MSG' (urlencoded key=val config/keepalive), 'W/F'
    (waterfall), 'SND' (audio/IQ). Verified against firmware v2026.609.
    """
    b = raw if isinstance(raw, bytes) else raw.encode("latin1")
    return b[:3].decode("latin1", "replace"), b[3:]


def parse_msg(payload: bytes) -> dict:
    """Parse a 'MSG' payload (space-separated, possibly urlencoded key=val) to a dict."""
    from urllib.parse import unquote

    out: dict = {}
    for tok in payload.decode("latin1", "replace").split():
        k, _, v = tok.partition("=")
        out[k] = unquote(v)
    return out


def _parse_kv(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


MAX_ZOOM = 14


def _decode_wf_frame(frame: bytes, center_hz, zoom, ui_srate_hz) -> WfFrame:
    """Decode a full W/F frame (pass the whole frame incl. the 'W/F' tag).

    Wire format (from firmware rx_waterfall.h `wf_pkt_t`, ARM little-endian;
    hw-validated 2026-07-01 on v2026.609):
        [0:4]   id4          : b"W/F\\x00"
        [4:8]   x_bin_server : u32  start position in *MAX_ZOOM-bin units*
                                    (HZperStart = ui_srate/(1024*2^MAX_ZOOM) ~ 3.66 Hz),
                                    NOT current-zoom bins
        [8:12]  flags_x_zoom : u32  (zoom in low 16; flags in high 16;
                                     compression bit = 0x00010000)
        [12:16] seq          : u32
        [16: ]  data         : with `SET wf_comp=0` -> 1024 raw bytes, one per bin,
                               dBm = byte - 255 (0..-200 dBm -> 255..55)
                               else zoom!=0 -> IMA-ADPCM (don't: send wf_comp=0)

    freq of bin i = start_hz + (i + 0.5) * bin_hz.
    """
    import struct

    span = ui_srate_hz / (1 << zoom)
    bin_hz = span / 1024.0
    hz_per_start = ui_srate_hz / (1024 * (1 << MAX_ZOOM))
    x_bin, flags_zoom = struct.unpack_from("<II", frame, 4)
    compressed = bool(flags_zoom & 0x00010000)
    if not compressed and len(frame) >= 16 + 1024:
        bins = [b - 255 for b in frame[16:16 + 1024]]  # dBm per bin
    else:
        bins = []  # compressed — collector always sends wf_comp=0, so shouldn't happen
    return WfFrame(center_hz=center_hz, start_hz=x_bin * hz_per_start,
                   bin_hz=bin_hz, bins=bins, ts=time.time())
