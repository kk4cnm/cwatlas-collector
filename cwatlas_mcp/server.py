"""CWAtlas MCP sidecar — the control plane between the managing LLM agent and the
collector. Reads live `CollectorState`, pushes bounded `Nudge`s onto the `ControlBus`.

It NEVER carries IQ/audio (that flows collector <-> SDR directly). Three tool families:
observe, nudge, and TX/safety-adjacent.

Run:  python -m cwatlas_mcp.server          # stdio transport (local agent)
For a remote agent, switch to MCP streamable-HTTP transport (see __main__).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .models import Nudge
from .scheduler import CollectorState, ControlBus
from .sdr_client import SdrClient, SdrConfig

mcp = FastMCP("cwatlas")

# Wired up by the collector at startup (see runtime.attach). The MCP server is a thin
# view over the supervisor's state + control bus; it owns neither.
STATE: Optional[CollectorState] = None
BUS: Optional[ControlBus] = None
SDR: Optional[SdrClient] = None


def attach(state: CollectorState, bus: ControlBus, sdr: SdrClient) -> None:
    global STATE, BUS, SDR
    STATE, BUS, SDR = state, bus, sdr


def _require():
    if STATE is None or BUS is None or SDR is None:
        raise RuntimeError("MCP sidecar not attached to a running collector")
    return STATE, BUS, SDR


# ============================== Observe ==================================
@mcp.tool()
async def get_sdr_status() -> dict:
    """Firmware + hardware health summary (AJAX /status)."""
    _, _, sdr = _require()
    return await sdr.get_status()


@mcp.tool()
async def get_activity_map(band: Optional[str] = None) -> list[dict]:
    """Current CW detections across searched bands: freq, strength, keyed_confidence, age.

    Pass `band` (e.g. "20m") to filter. This is the agent's window into what's on the air.
    """
    state, _, _ = _require()
    dets = [d for d in state.activity.values() if band is None or d.band == band]
    dets.sort(key=lambda d: d.keyed_confidence, reverse=True)
    return [
        {
            "freq_hz": d.freq_hz,
            "band": d.band,
            "strength_db": round(d.strength_db, 1),
            "keyed_confidence": round(d.keyed_confidence, 2),
            "age_s": round(d.age_s, 1),
        }
        for d in dets
    ]


@mcp.tool()
async def get_channel_roster() -> list[dict]:
    """What each of the SDR's RX (capture) channels is currently doing."""
    state, _, _ = _require()
    return [
        {**asdict(cs), "mode": cs.mode.value, "dwell_s": round(cs.dwell_s, 1)}
        for cs in state.channels.values()
    ]


@mcp.tool()
async def get_adc_overload() -> dict:
    """ADC clip/overload state (AJAX /adc). Reactive front-end-overload signal."""
    _, _, sdr = _require()
    return await sdr.get_adc()


@mcp.tool()
async def get_collection_stats(window: str = "24h") -> dict:
    """Coverage/throughput over the long run (catalog DB)."""
    # TODO: query the catalog DB for captures/bytes/band coverage over `window`.
    _require()
    return {"window": window, "note": "TODO: implement against catalog DB"}


# =============================== Nudge ===================================
@mcp.tool()
async def prioritize_band(band: str, weight: float = 2.0) -> str:
    """Bias the scheduler toward `band` (weight > 1 favors it). Supervisor stays in charge."""
    _, bus, _ = _require()
    await bus.push(Nudge("prioritize_band", {"band": band, "weight": weight}))
    return f"queued: prioritize {band} x{weight}"


@mcp.tool()
async def pin_frequency(freq_hz: float, dwell_s: float = 60.0) -> str:
    """Force a channel onto `freq_hz` for at least `dwell_s` seconds."""
    _, bus, _ = _require()
    await bus.push(Nudge("pin_frequency", {"freq_hz": freq_hz, "dwell_s": dwell_s}))
    return f"queued: pin {freq_hz:.0f} Hz for {dwell_s}s"


@mcp.tool()
async def request_deep_dwell(freq_hz: float, seconds: float = 120.0) -> str:
    """Speculatively capture a weak/marginal candidate (the low-SNR cases MorseBase needs)."""
    _, bus, _ = _require()
    await bus.push(Nudge("request_deep_dwell", {"freq_hz": freq_hz, "seconds": seconds}))
    return f"queued: deep-dwell {freq_hz:.0f} Hz for {seconds}s"


@mcp.tool()
async def pause_channel(ch: int) -> str:
    """Stop capture on a channel (e.g. for maintenance)."""
    _, bus, _ = _require()
    await bus.push(Nudge("pause_channel", {"ch": ch}))
    return f"queued: pause channel {ch}"


@mcp.tool()
async def resume_channel(ch: int) -> str:
    _, bus, _ = _require()
    await bus.push(Nudge("resume_channel", {"ch": ch}))
    return f"queued: resume channel {ch}"


# ========================= TX / safety-adjacent ==========================
@mcp.tool()
async def notify_tx(active: bool) -> str:
    """Tell the collector the operator is transmitting. Marks capture windows contaminated
    and gates new assignments. NOTE: this is DATA HYGIENE, not front-end protection —
    that must be a hardware PTT interlock (Flex amp-key -> coax relay), never this call.
    """
    _, bus, _ = _require()
    await bus.push(Nudge("notify_tx", {"active": active}))
    return f"queued: tx={'on' if active else 'off'}"


@mcp.tool()
async def mark_window_contaminated(start_ts: float, end_ts: float, reason: str = "tx") -> str:
    """Flag a time window so MorseBase never trains on contaminated IQ."""
    _, bus, _ = _require()
    await bus.push(Nudge("mark_contaminated",
                         {"start_ts": start_ts, "end_ts": end_ts, "reason": reason}))
    return "queued: contamination window"


@mcp.tool()
async def ground_antenna() -> str:
    """SECONDARY/manual: ground the SDR antenna via the ant_switch EXT (SET Antenna=0).
    Not the TX interlock — that is hardware. Useful for storms / manual safe state.
    """
    _, _, sdr = _require()
    await sdr.set_antenna(0)
    return "antenna grounded (secondary control)"


@mcp.tool()
async def set_antenna(n: int) -> str:
    """Select antenna input n (1..6); 0 grounds all. Secondary control only."""
    _, _, sdr = _require()
    await sdr.set_antenna(n)
    return f"antenna set to {n}"


if __name__ == "__main__":
    # Standalone (no attached collector) is only useful for tool-listing/inspection.
    # In production the collector imports this module, calls attach(), then runs both
    # the supervisor and mcp together (see README). Default transport: stdio.
    mcp.run()
