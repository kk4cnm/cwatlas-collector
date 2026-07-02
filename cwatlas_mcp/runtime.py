"""Wires the pieces together and runs the supervisor + MCP sidecar in one asyncio app.

Model B: the supervisor is the long-lived core and keeps capturing even if the MCP
interface (or the agent) goes away. MCP is attached as a view over shared state.

M2 wiring (hw-validated 2026-07-01): ONE pooled /W/F scanner cycles the CW watering
holes -> detector -> supervisor assigns idle RX channels -> capture workers write
SigMF + catalog rows. Channel budget: the scanner's W/F connection occupies one of
the device's rx channels, so capture capacity = rx_chans - 1.

Usage:
    python -m cwatlas_mcp.runtime                     # collect forever + MCP (stdio)
    python -m cwatlas_mcp.runtime --trial 180         # 3-minute trial, no MCP
    CWATLAS_SDR_HOST=192.168.2.46 overrides the SDR host.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from . import capture
from .capture import channel_worker
from .catalog import Catalog
from .detector import detect_cw
from .models import Detection
from .scheduler import CollectorState, ControlBus, SchedulerConfig, Supervisor
from .sdr_client import SdrClient, SdrConfig

# CW watering holes: (band, center_hz of one 60 kHz zoom-10 view).
SEARCH_PLAN = [
    ("160m", 1_820_000.0),
    ("80m",  3_530_000.0),
    ("40m",  7_030_000.0),
    ("30m", 10_115_000.0),
    ("20m", 14_030_000.0),
    ("17m", 18_081_000.0),
    ("15m", 21_030_000.0),
    ("12m", 24_905_000.0),
    ("10m", 28_030_000.0),
]


async def scan_worker(sdr: SdrClient, sup: Supervisor) -> None:
    """The search plane: one pooled W/F connection, forever."""
    async for band, _cf, frames in sdr.waterfall_scan(SEARCH_PLAN):
        for det in detect_cw(frames, band):
            if det.keyed_confidence > 0:
                sup.observe(det)


async def solar_worker(sup: Supervisor, lat: float, lon: float,
                       every_s: float = 300.0) -> None:
    """Refresh band_priority from solar elevation (HF propagation follows the sun).

    Biases capture assignment toward bands that are actually open (high bands by
    day, low bands after dark, gray-line boost at twilight) — cuts false-positive
    captures on closed bands. NB: writes the same band_priority dict agent nudges
    use; M4 must reconcile (e.g. nudges as multipliers on the solar baseline).
    """
    from .solar import band_weights

    last_phase = None
    while True:
        phase, weights = band_weights(lat, lon)
        sup.state.band_priority.update(weights)
        if phase != last_phase:
            print(f"[solar] phase={phase} at ({lat:.2f},{lon:.2f}); "
                  f"weights: " + " ".join(f"{b}={w:.1f}" for b, w in weights.items()))
            last_phase = phase
        await asyncio.sleep(every_s)


async def ptt_worker(sup: Supervisor) -> None:
    """Operator TX state (Flex amp-key via GPIO/serial or SmartSDR CAT) -> sup.set_tx().

    NB: the hardware antenna-disconnect relay is the actual front-end protection;
    this is data hygiene only (mark capture windows contaminated). TODO[M3]: real
    PTT ingest — until then TX periods rely on the relay muting the captures.
    """
    while True:
        await asyncio.sleep(0.5)


class ChannelPool:
    """One PERSISTENT worker (and /SND connection) per RX channel slot.

    The supervisor drives via spawn/stop callbacks; workers never close their
    connection between captures — they retune in place (the first live trial
    proved per-capture connections starve the device within a minute).
    """

    def __init__(self, sdr: SdrClient, state: CollectorState,
                 catalog: Catalog, data_dir: Path, rotate_s: float):
        self.inboxes: dict[int, asyncio.Queue] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        for ch, cs in state.channels.items():
            inbox: asyncio.Queue = asyncio.Queue()
            self.inboxes[ch] = inbox
            self.tasks[ch] = asyncio.create_task(
                channel_worker(sdr, cs, inbox, catalog, data_dir,
                               rotate_s=rotate_s),
                name=f"capture-ch{ch}")

    def spawn(self, ch: int, det: Detection) -> None:
        print(f"[supervisor] ch{ch} -> capture {det.freq_hz/1e3:.2f} kHz "
              f"({det.band}, {det.strength_db:.0f} dB, keyed={det.keyed_confidence:.2f})")
        self.inboxes[ch].put_nowait(det)

    def stop(self, ch: int) -> None:
        self.inboxes[ch].put_nowait(None)     # release: finalize file, stay connected

    async def shutdown(self) -> None:
        for inbox in self.inboxes.values():
            inbox.put_nowait(capture.SHUTDOWN)
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description="CWAtlas collector")
    ap.add_argument("--host", default=os.environ.get("CWATLAS_SDR_HOST",
                                                     "192.168.2.46"))
    ap.add_argument("--port", type=int, default=8073)
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("CWATLAS_DATA_DIR",
                                                "~/cwatlas/data")).expanduser())
    ap.add_argument("--trial", type=float, default=0.0,
                    help="run N seconds then exit (skips the MCP server)")
    ap.add_argument("--lat", type=float,
                    default=float(os.environ.get("CWATLAS_LAT", "nan")))
    ap.add_argument("--lon", type=float,
                    default=float(os.environ.get("CWATLAS_LON", "nan")))
    ap.add_argument("--rotate-s", type=float, default=600.0,
                    help="max seconds per capture file segment")
    args = ap.parse_args()

    sdr = SdrClient(SdrConfig(host=args.host, port=args.port))

    # authoritative channel count from the device (this unit: 12, not 13)
    dev = await sdr.read_config()
    rx_chans = int(dev.get("rx_chans", 12))
    print(f"[runtime] device: v{dev.get('version_maj','?')}.{dev.get('version_min','?')} "
          f"rx_chans={rx_chans}; capture capacity={rx_chans - 1} (1 held by scanner)")
    await asyncio.sleep(2)  # let the read_config channel hold clear a moment

    state = CollectorState()
    bus = ControlBus()
    catalog = Catalog(args.data_dir / "catalog.db")
    cfg = SchedulerConfig(n_rx_channels=rx_chans - 1)
    # Supervisor must exist before the pool (it populates state.channels), but the
    # pool must exist before ticks assign — construct in this order:
    sup = Supervisor(cfg, state, bus)
    pool = ChannelPool(sdr, state, catalog, args.data_dir, rotate_s=args.rotate_s)
    sup.spawn_capture = pool.spawn
    sup.stop_capture = pool.stop

    tasks = [
        asyncio.create_task(sup.run(), name="supervisor"),
        asyncio.create_task(scan_worker(sdr, sup), name="scanner"),
        asyncio.create_task(ptt_worker(sup), name="ptt"),
    ]
    if args.lat == args.lat and args.lon == args.lon:  # NaN-safe "both set"
        tasks.append(asyncio.create_task(
            solar_worker(sup, args.lat, args.lon), name="solar"))
    else:
        print("[runtime] no --lat/--lon (or CWATLAS_LAT/LON): "
              "solar band weighting disabled, neutral priorities")
    if not args.trial:
        from . import server
        server.attach(state, bus, sdr)
        tasks.append(asyncio.create_task(server.mcp.run_stdio_async(), name="mcp"))

    try:
        if args.trial:
            done, _ = await asyncio.wait(tasks, timeout=args.trial,
                                         return_when=asyncio.FIRST_EXCEPTION)
            for t in done:   # surface a crashed worker instead of a silent trial
                if not t.cancelled() and t.exception():
                    raise t.exception()
        else:
            await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.shutdown()   # workers finalize in-flight files/rows, then exit
        print(f"[runtime] catalog: {catalog.stats()}")
        catalog.close()
        await sdr.aclose()


if __name__ == "__main__":
    asyncio.run(main())
