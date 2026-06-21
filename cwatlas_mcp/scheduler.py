"""The collector's deterministic brain: Activity Map + Supervisor.

This is Model B — it runs and keeps capturing with NO LLM in the loop. The MCP sidecar
only reads `CollectorState` and pushes `Nudge`s onto the `ControlBus`; the supervisor
remains authoritative over channel assignment.

Detection (CW signature) and the WS frame decode live behind the search workers and the
SdrClient; this module is policy + bookkeeping, intentionally LLM-free and testable.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import ChannelMode, ChannelState, Detection, Nudge, TxEvent


@dataclass
class SchedulerConfig:
    n_rx_channels: int = 13           # [verify on hw]
    deep_dwell_reserve: int = 2       # channels held for weak/speculative captures
    min_dwell_s: float = 8.0          # don't thrash a channel off a real QSO
    release_timeout_s: float = 12.0   # CW QSOs have gaps; tune so we don't drop mid-QSO
    detection_stale_s: float = 30.0   # decay detections unseen this long
    keyed_conf_threshold: float = 0.5
    tick_s: float = 1.0


class ControlBus:
    """Decoupling boundary between MCP and the supervisor.

    In-memory for the skeleton; production should back this with the catalog DB so the
    two processes are fully isolated (kill one, the other survives).
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[Nudge] = asyncio.Queue()

    async def push(self, nudge: Nudge) -> None:
        await self._q.put(nudge)

    def drain(self) -> list[Nudge]:
        out: list[Nudge] = []
        while not self._q.empty():
            out.append(self._q.get_nowait())
        return out


@dataclass
class CollectorState:
    """Everything the MCP sidecar reads. Owned/written by the supervisor only."""

    channels: dict[int, ChannelState] = field(default_factory=dict)
    activity: dict[float, Detection] = field(default_factory=dict)  # keyed by freq_hz
    band_priority: dict[str, float] = field(default_factory=dict)
    tx_active: bool = False
    tx_events: list[TxEvent] = field(default_factory=list)


class Supervisor:
    def __init__(self, cfg: SchedulerConfig, state: CollectorState, bus: ControlBus):
        self.cfg = cfg
        self.state = state
        self.bus = bus
        for ch in range(cfg.n_rx_channels):
            state.channels[ch] = ChannelState(ch=ch)

    # ---- main loop -------------------------------------------------------
    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # never let one bad tick kill collection
                # TODO: structured logging / metrics
                print(f"[supervisor] tick error: {exc}")
            await asyncio.sleep(self.cfg.tick_s)

    def tick(self) -> None:
        self._decay_activity()
        self._apply_nudges(self.bus.drain())
        if self.state.tx_active:
            self._handle_tx()
            return
        desired = self._score_candidates()
        self._reconcile(desired)
        # TODO: persist channel_state + capture rows to the catalog DB here.

    # ---- ingest from search workers -------------------------------------
    def observe(self, det: Detection) -> None:
        """Called by search workers when a detection appears/updates."""
        existing = self.state.activity.get(det.freq_hz)
        if existing:
            existing.last_seen = det.last_seen
            existing.strength_db = det.strength_db
            existing.keyed_confidence = det.keyed_confidence
        else:
            self.state.activity[det.freq_hz] = det

    def _decay_activity(self) -> None:
        cutoff = self.cfg.detection_stale_s
        for f in [f for f, d in self.state.activity.items() if d.age_s > cutoff]:
            del self.state.activity[f]

    # ---- nudges (bounded agent writes) ----------------------------------
    def _apply_nudges(self, nudges: list[Nudge]) -> None:
        for n in nudges:
            if n.kind == "prioritize_band":
                self.state.band_priority[n.payload["band"]] = n.payload["weight"]
            elif n.kind == "pause_channel":
                self._set_mode(n.payload["ch"], ChannelMode.PAUSED)
            elif n.kind == "resume_channel":
                self._set_mode(n.payload["ch"], ChannelMode.IDLE)
            elif n.kind in ("pin_frequency", "request_deep_dwell"):
                # TODO: force-assign an idle channel to payload["freq_hz"].
                pass
            # unknown kinds ignored on purpose (forward-compatible)

    # ---- scoring & reconciliation ---------------------------------------
    def _score_candidates(self) -> list[Detection]:
        """Rank active detections worth capturing. Higher = more deserving."""
        def score(d: Detection) -> float:
            prio = self.state.band_priority.get(d.band, 1.0)
            # priority > keyed_confidence > strength > (TODO) coverage_debt
            return prio * (d.keyed_confidence * 10 + d.strength_db)

        cands = [
            d for d in self.state.activity.values()
            if d.keyed_confidence >= self.cfg.keyed_conf_threshold
        ]
        return sorted(cands, key=score, reverse=True)

    def _reconcile(self, desired: list[Detection]) -> None:
        # 1. release idle/stale channels (respecting min dwell)
        for ch in self.state.channels.values():
            if ch.mode == ChannelMode.CAPTURING and ch.freq_hz is not None:
                det = self.state.activity.get(ch.freq_hz)
                stale = det is None or det.age_s > self.cfg.release_timeout_s
                if stale and ch.dwell_s > self.cfg.min_dwell_s:
                    self._release(ch.ch)

        # 2. assign idle channels to top unserved candidates
        served = {c.freq_hz for c in self.state.channels.values() if c.freq_hz}
        capacity = self.cfg.n_rx_channels - self.cfg.deep_dwell_reserve
        active = sum(1 for c in self.state.channels.values()
                     if c.mode == ChannelMode.CAPTURING)
        for det in desired:
            if active >= capacity:
                break
            if det.freq_hz in served:
                continue
            ch = self._first_idle()
            if ch is None:
                break
            self._assign(ch, det)
            active += 1

    # ---- channel ops (skeleton: state only; capture worker does the WS I/O) --
    def _first_idle(self) -> Optional[int]:
        for ch in self.state.channels.values():
            if ch.mode == ChannelMode.IDLE:
                return ch.ch
        return None

    def _assign(self, ch: int, det: Detection) -> None:
        cs = self.state.channels[ch]
        cs.mode = ChannelMode.CAPTURING
        cs.freq_hz = det.freq_hz
        cs.since = time.time()
        cs.contaminated = False
        # TODO: spawn a capture worker -> SdrClient.capture_iq(det.freq_hz)

    def _release(self, ch: int) -> None:
        cs = self.state.channels[ch]
        cs.mode = ChannelMode.IDLE
        cs.freq_hz = None
        cs.since = time.time()
        cs.capture_id = None
        # TODO: stop the capture worker, finalize the catalog row.

    def _set_mode(self, ch: int, mode: ChannelMode) -> None:
        cs = self.state.channels[ch]
        cs.mode = mode
        cs.since = time.time()

    # ---- TX hygiene ------------------------------------------------------
    def set_tx(self, active: bool) -> None:
        """Driven by the PTT ingest (Flex amp-key / SmartSDR CAT)."""
        self.state.tx_active = active
        if active:
            self.state.tx_events.append(TxEvent(start_ts=time.time()))
        elif self.state.tx_events and self.state.tx_events[-1].stop_ts is None:
            self.state.tx_events[-1].stop_ts = time.time()

    def _handle_tx(self) -> None:
        for cs in self.state.channels.values():
            if cs.mode == ChannelMode.CAPTURING:
                cs.contaminated = True
        # TODO: mark affected catalog capture windows contaminated; hold new assignments.
