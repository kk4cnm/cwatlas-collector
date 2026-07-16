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
from typing import Callable, Optional

from .models import ChannelMode, ChannelState, Detection, Nudge, TxEvent

# Amateur band edges (Hz) for classifying agent-pinned frequencies.
BAND_EDGES = [
    ("160m", 1_800e3, 2_000e3), ("80m", 3_500e3, 4_000e3),
    ("60m", 5_330e3, 5_407e3), ("40m", 7_000e3, 7_300e3),
    ("30m", 10_100e3, 10_150e3), ("20m", 14_000e3, 14_350e3),
    ("17m", 18_068e3, 18_168e3), ("15m", 21_000e3, 21_450e3),
    ("12m", 24_890e3, 24_990e3), ("10m", 28_000e3, 29_700e3),
    ("6m", 50_000e3, 54_000e3),
]


def band_for_freq(freq_hz: float) -> str:
    for band, lo, hi in BAND_EDGES:
        if lo <= freq_hz <= hi:
            return band
    return "oob"


@dataclass
class SchedulerConfig:
    """NB on time constants: detections only refresh when the single pooled
    scanner revisits that band — a full cycle over 9 watering holes is ~90-120 s.
    Timeouts SHORTER than the revisit period cause release/reassign ping-pong
    (observed in the first live trial: 154 rows in 3 min, most 0-sample)."""

    n_rx_channels: int = 12           # authoritative count read from hw at startup
    deep_dwell_reserve: int = 2       # channels held for weak/speculative captures
    min_dwell_s: float = 20.0         # don't thrash a channel off a real QSO
    release_timeout_s: float = 150.0  # > scan revisit period, or captures ping-pong
    detection_stale_s: float = 240.0  # decay detections unseen this long
    keyed_conf_threshold: float = 0.5
    # minimum weighted score to be assigned a channel AT ALL. With neutral
    # band_priority (1.0) almost anything eligible passes (keyed 0.5 + 3 dB = 8);
    # with solar weighting, closed-band junk is excluded even when channels are
    # idle (day 160m at 0.2: needs keyed*10+SNR >= 40) instead of squatting on
    # them for release_timeout_s. Signals must EARN a channel, not just exist.
    min_capture_score: float = 8.0
    # channel-hog guard: busy frequencies never go stale (fresh detections every
    # scan cycle), so without a cap one 14 dB signal can hold a channel for
    # hours (observed: 265 min in the first overnight soak). Force the slot back
    # into competition; the cooldown stops the same signal instantly re-winning.
    max_dwell_s: float = 1800.0
    capture_cooldown_s: float = 180.0
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
    # solar baseline, written by solar_worker (or left empty = neutral 1.0)
    band_priority: dict[str, float] = field(default_factory=dict)
    # agent-nudge multipliers ON TOP of the solar baseline: band -> (mult, expires_at).
    # Separate dict so solar refreshes never clobber a nudge and vice versa;
    # the TTL keeps a forgotten nudge from steering collection forever.
    band_nudge: dict[str, tuple[float, float]] = field(default_factory=dict)
    tx_active: bool = False
    tx_events: list[TxEvent] = field(default_factory=list)


class Supervisor:
    NUDGE_TTL_S = 900.0   # agent band nudges decay after 15 min unless renewed

    def __init__(self, cfg: SchedulerConfig, state: CollectorState, bus: ControlBus,
                 spawn_capture: Optional[Callable[[int, Detection], None]] = None,
                 stop_capture: Optional[Callable[[int], None]] = None,
                 catalog=None):
        self.cfg = cfg
        self.state = state
        self.bus = bus
        # runtime wires these to real capture-worker task management; None in tests
        self.spawn_capture = spawn_capture
        self.stop_capture = stop_capture
        self.catalog = catalog        # for mark_contaminated nudges; None in tests
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

    _n_ticks = 0

    def tick(self) -> None:
        self._decay_activity()
        self._apply_nudges(self.bus.drain())
        if self.state.tx_active:
            self._handle_tx()
            return
        desired = self._score_candidates()
        self._reconcile(desired)
        self._n_ticks += 1
        if self._n_ticks % 30 == 0:   # periodic health line (~every 30s)
            modes = [c.mode.value[:3] for c in self.state.channels.values()]
            print(f"[supervisor] tick={self._n_ticks} ch=[{' '.join(modes)}] "
                  f"activity={len(self.state.activity)} eligible={len(desired)}")

    # ---- ingest from search workers -------------------------------------
    MERGE_TOLERANCE_HZ = 120.0  # ~2 waterfall bins: same station, slight drift

    def observe(self, det: Detection) -> None:
        """Called by search workers when a detection appears/updates."""
        existing = None
        for f, d in self.state.activity.items():
            if abs(f - det.freq_hz) <= self.MERGE_TOLERANCE_HZ:
                existing = d
                break
        if existing:
            existing.last_seen = det.last_seen
            existing.strength_db = det.strength_db
            # keep the best keying evidence seen recently (one quiet dwell
            # shouldn't demote a station mid-QSO; staleness decay handles exits)
            existing.keyed_confidence = max(existing.keyed_confidence,
                                            det.keyed_confidence)
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
                # multiplier on the solar baseline, not an overwrite — solar
                # keeps refreshing band_priority underneath and both survive
                ttl = float(n.payload.get("ttl_s", self.NUDGE_TTL_S))
                self.state.band_nudge[n.payload["band"]] = (
                    n.payload["weight"], time.time() + ttl)
            elif n.kind == "pause_channel":
                self._set_mode(n.payload["ch"], ChannelMode.PAUSED)
            elif n.kind == "resume_channel":
                self._set_mode(n.payload["ch"], ChannelMode.IDLE)
            elif n.kind in ("pin_frequency", "request_deep_dwell"):
                self._pin(n)
            elif n.kind == "notify_tx":
                self.set_tx(bool(n.payload["active"]))
            elif n.kind == "mark_contaminated" and self.catalog is not None:
                # `reason` is the agent's justification for retroactively
                # flagging an unbounded number of captures. It used to stop
                # here; now it lands in the event log with the flag.
                n_hit = self.catalog.mark_window(
                    n.payload["start_ts"], n.payload["end_ts"],
                    actor="agent:mark_window_contaminated",
                    reason=n.payload.get("reason"))
                print(f"[supervisor] contamination window "
                      f"{n.payload['start_ts']:.0f}..{n.payload['end_ts']:.0f}: "
                      f"{n_hit} capture(s) flagged "
                      f"(reason: {n.payload.get('reason') or 'unstated'})")
            # unknown kinds ignored on purpose (forward-compatible)

    def _pin(self, n: Nudge) -> None:
        """Force-assign an idle channel to an agent-chosen frequency.

        The pin rides the normal lifecycle: a synthetic high-confidence
        Detection goes on the activity map and an idle channel is assigned
        directly. If the scanner doesn't confirm the frequency it goes stale
        and releases after release_timeout_s (~150 s), so dwell_s requests
        beyond that need the agent to re-pin.
        """
        freq = float(n.payload["freq_hz"])
        det = Detection(freq_hz=freq, band=band_for_freq(freq),
                        strength_db=99.0, keyed_confidence=1.0)
        self.state.activity[freq] = det
        ch = self._first_idle()
        if ch is None:
            print(f"[supervisor] pin {freq/1e3:.2f} kHz: no idle channel")
            return
        self._assign(ch, det)
        if n.kind == "request_deep_dwell":
            self.state.channels[ch].mode = ChannelMode.DEEP_DWELL
        print(f"[supervisor] ch{ch} pinned to {freq/1e3:.2f} kHz ({n.kind})")

    def _nudge_mult(self, band: str) -> float:
        entry = self.state.band_nudge.get(band)
        if entry is None:
            return 1.0
        mult, expires = entry
        if time.time() >= expires:
            del self.state.band_nudge[band]
            return 1.0
        return mult

    # ---- scoring & reconciliation ---------------------------------------
    def _score_candidates(self) -> list[Detection]:
        """Rank active detections worth capturing. Higher = more deserving."""
        def score(d: Detection) -> float:
            prio = (self.state.band_priority.get(d.band, 1.0)
                    * self._nudge_mult(d.band))
            # priority > keyed_confidence > strength > (TODO) coverage_debt
            return prio * (d.keyed_confidence * 10 + d.strength_db)

        cands = [
            d for d in self.state.activity.values()
            if d.keyed_confidence >= self.cfg.keyed_conf_threshold
            # a detection too stale to keep a channel must not win a new one,
            # or release->reassign ping-pongs the same station across channels
            and d.age_s <= self.cfg.release_timeout_s
            # and it must EARN the channel under current band weighting
            and score(d) >= self.cfg.min_capture_score
            # and not be serving a max-dwell cooldown
            and time.time() >= d.cooldown_until
        ]
        return sorted(cands, key=score, reverse=True)

    def _reconcile(self, desired: list[Detection]) -> None:
        # 1. release stale channels (respecting min dwell) and max-dwell hogs
        for ch in self.state.channels.values():
            if (ch.mode in (ChannelMode.CAPTURING, ChannelMode.DEEP_DWELL)
                    and ch.freq_hz is not None):
                det = self.state.activity.get(ch.freq_hz)
                stale = det is None or det.age_s > self.cfg.release_timeout_s
                if stale and ch.dwell_s > self.cfg.min_dwell_s:
                    self._release(ch.ch)
                elif ch.dwell_s > self.cfg.max_dwell_s:
                    # hog guard: back into competition + cooldown so the same
                    # signal doesn't instantly re-win the freed slot
                    if det is not None:
                        det.cooldown_until = (time.time()
                                              + self.cfg.capture_cooldown_s)
                    print(f"[supervisor] ch{ch.ch} max-dwell release "
                          f"{ch.freq_hz/1e3:.2f} kHz after {ch.dwell_s/60:.0f} min")
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
        if self.spawn_capture:
            self.spawn_capture(ch, det)   # runtime starts a capture_worker task

    def _release(self, ch: int) -> None:
        if self.stop_capture:
            self.stop_capture(ch)         # cancels the worker; it finalizes files/row
        cs = self.state.channels[ch]
        cs.mode = ChannelMode.IDLE
        cs.freq_hz = None
        cs.since = time.time()
        cs.capture_id = None

    def _set_mode(self, ch: int, mode: ChannelMode) -> None:
        cs = self.state.channels[ch]
        cs.mode = mode
        cs.since = time.time()

    # ---- TX hygiene ------------------------------------------------------
    def set_tx(self, active: bool) -> None:
        """Driven by the PTT ingest (see ptt.py: Flex interlock status)."""
        if active == self.state.tx_active:
            return
        self.state.tx_active = active
        if active:
            self.state.tx_events.append(TxEvent(start_ts=time.time()))
            print("[supervisor] TX active: holding assignments, flagging captures")
        else:
            if self.state.tx_events and self.state.tx_events[-1].stop_ts is None:
                self.state.tx_events[-1].stop_ts = time.time()
            # files that overlapped TX latched their own contaminated flag in
            # the capture worker; clear the channel flags so the NEXT file
            # segment on each channel starts clean
            for cs in self.state.channels.values():
                cs.contaminated = False
            print("[supervisor] TX ended: resuming assignments")

    def _handle_tx(self) -> None:
        """Each tick while transmitting. New assignments are held (tick returns
        early); in-flight captures keep running but their files get flagged."""
        for cs in self.state.channels.values():
            if cs.mode in (ChannelMode.CAPTURING, ChannelMode.DEEP_DWELL):
                cs.contaminated = True
