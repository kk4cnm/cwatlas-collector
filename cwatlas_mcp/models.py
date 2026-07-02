"""Core data structures shared by the collector and the MCP sidecar.

These are deliberately plain dataclasses so they serialize cleanly to the catalog
DB and to MCP tool responses. Keep them free of behavior.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ChannelMode(str, Enum):
    IDLE = "idle"
    CAPTURING = "capturing"
    DEEP_DWELL = "deep_dwell"  # speculative weak-signal capture
    PAUSED = "paused"


@dataclass
class Detection:
    """A candidate CW signal found by a search-plane (waterfall) worker."""

    freq_hz: float
    band: str                      # e.g. "20m"
    strength_db: float             # power above rolling noise floor
    keyed_confidence: float        # [0,1]; is this actually keyed CW?
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    cooldown_until: float = 0.0    # after a max-dwell force-release: can't win
                                   # a channel again until this passes

    @property
    def age_s(self) -> float:
        return time.time() - self.last_seen


@dataclass
class ChannelState:
    """Live state of one of the SDR's RX (capture) channels."""

    ch: int
    mode: ChannelMode = ChannelMode.IDLE
    freq_hz: Optional[float] = None
    since: float = field(default_factory=time.time)
    capture_id: Optional[int] = None   # FK into catalog `captures`
    contaminated: bool = False         # set true while operator is transmitting

    @property
    def dwell_s(self) -> float:
        return time.time() - self.since


@dataclass
class TxEvent:
    start_ts: float
    stop_ts: Optional[float] = None
    reason: str = "ptt"


@dataclass
class Nudge:
    """A bounded write from the agent via MCP. The supervisor stays authoritative."""

    kind: str          # "prioritize_band" | "pin_frequency" | "pause_channel" | ...
    payload: dict
    ts: float = field(default_factory=time.time)
