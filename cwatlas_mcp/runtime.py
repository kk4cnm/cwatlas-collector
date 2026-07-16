"""Wires the pieces together and runs the supervisor + MCP sidecar in one asyncio app.

Model B: the supervisor is the long-lived core and keeps capturing even if the MCP
interface (or the agent) goes away. MCP is attached as a view over shared state.

M2 wiring (hw-validated 2026-07-01): ONE pooled /W/F scanner cycles the CW watering
holes -> detector -> supervisor assigns idle RX channels -> capture workers write
SigMF + catalog rows. Channel budget: the scanner's W/F connection occupies one of
the device's rx channels, so capture capacity = rx_chans - 1.

Site details (SDR address, antenna location, data dir) come from config.toml —
see config.example.toml and config.py. Nothing about one station belongs here.

Usage:
    python -m cwatlas_mcp.runtime                     # collect forever + MCP (stdio)
    python -m cwatlas_mcp.runtime --trial 180         # 3-minute trial, no MCP
    python -m cwatlas_mcp.runtime --config /etc/cwatlas/config.toml
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sqlite3
from pathlib import Path

from . import capture, config
from .capture import channel_worker
from .catalog import Catalog
from .detector import detect_cw
from .models import Detection
from .provenance import build_run_info
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
    captures on closed bands. Agent nudges live in the separate state.band_nudge
    dict and multiply this baseline (see Supervisor._nudge_mult), so neither
    writer clobbers the other.
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


# PTT ingest (M3) lives in ptt.py: Flex SmartSDR interlock status -> sup.set_tx().
# Data hygiene only — the hardware antenna-disconnect relay is the front-end
# protection. With no --flex-host the collector runs relay-only (TX periods
# record muted antenna instead of being flagged in the catalog).


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
    # Two-stage parse: --config has to be known before the other flags can take
    # their defaults from it. Site details (LAN addresses, antenna location)
    # live in config.toml, not in source — see config.py for the precedence.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None,
                     help="site config TOML (default: $CWATLAS_CONFIG, else "
                          "config.toml beside the package)")
    pre_args, _ = pre.parse_known_args()
    cfg_file = config.load(pre_args.config)

    ap = argparse.ArgumentParser(description="CWAtlas collector", parents=[pre])
    ap.add_argument("--host",
                    default=config.pick(cfg_file, "sdr.host", "CWATLAS_SDR_HOST"))
    ap.add_argument("--port", type=int,
                    default=config.pick(cfg_file, "sdr.port", default=8073,
                                        cast=int))
    ap.add_argument("--data-dir", type=Path,
                    default=config.pick(cfg_file, "paths.data_dir",
                                        "CWATLAS_DATA_DIR", "~/cwatlas/data"))
    ap.add_argument("--trial", type=float, default=0.0,
                    help="run N seconds then exit (skips the MCP server)")
    ap.add_argument("--lat", type=float,
                    default=config.pick(cfg_file, "station.lat", "CWATLAS_LAT",
                                        float("nan"), cast=float))
    ap.add_argument("--lon", type=float,
                    default=config.pick(cfg_file, "station.lon", "CWATLAS_LON",
                                        float("nan"), cast=float))
    ap.add_argument("--rotate-s", type=float,
                    default=config.pick(cfg_file, "capture.rotate_s",
                                        default=600.0, cast=float))
    ap.add_argument("--flex-host",
                    default=config.pick(cfg_file, "tx.flex_host",
                                        "CWATLAS_FLEX_HOST", ""),
                    help="Flex radio IP for PTT ingest; 'auto' = UDP discovery; "
                         "empty = TX hygiene disabled (hardware relay only)")
    ap.add_argument("--no-mcp", action="store_true",
                    help="collect without the MCP sidecar (headless service: "
                         "stdio transport needs a client on stdin; under "
                         "systemd that's /dev/null and MCP would exit at once)")
    args = ap.parse_args()
    args.config = cfg_file.get("_path")      # resolved path, for provenance

    # No default for the SDR: guessing an address someone else owns is worse
    # than saying so. (It used to default to the author's own LAN.)
    if not args.host:
        ap.error("no SDR host: set [sdr] host in config.toml (see "
                 "config.example.toml), or CWATLAS_SDR_HOST, or --host")
    args.data_dir = Path(args.data_dir).expanduser()

    if not args.trial and not args.no_mcp:
        # MCP-on-stdio owns stdout for JSON-RPC; every collector print() —
        # including the startup lines below — must go to stderr or it corrupts
        # the protocol stream. Rebinding print (rather than sys.stdout, which
        # FastMCP reads at startup) leaves the real stdout to the transport.
        import builtins
        import functools
        import sys
        builtins.print = functools.partial(print, file=sys.stderr, flush=True)

    sdr = SdrClient(SdrConfig(host=args.host, port=args.port))

    # authoritative channel count from the device (this unit: 12, not 13)
    dev = await sdr.read_config()
    rx_chans = int(dev.get("rx_chans", 12))
    print(f"[runtime] device: v{dev.get('version_maj','?')}.{dev.get('version_min','?')} "
          f"rx_chans={rx_chans}; capture capacity={rx_chans - 1} (1 held by scanner)")
    await asyncio.sleep(2)  # let the read_config channel hold clear a moment

    state = CollectorState()
    bus = ControlBus()
    # cfg before the catalog: begin_run snapshots it (it carries the hw-derived
    # channel count), and every capture this process makes points at that run.
    cfg = SchedulerConfig(n_rx_channels=rx_chans - 1)
    catalog = Catalog(args.data_dir / "catalog.db")
    run_id = catalog.begin_run(build_run_info(args, dev, cfg, SEARCH_PLAN))
    print(f"[runtime] run {run_id}: recorded firmware, git state and effective "
          f"config; captures from here carry run_id={run_id}")
    # Supervisor must exist before the pool (it populates state.channels), but the
    # pool must exist before ticks assign — construct in this order:
    sup = Supervisor(cfg, state, bus, catalog=catalog)
    pool = ChannelPool(sdr, state, catalog, args.data_dir, rotate_s=args.rotate_s)
    sup.spawn_capture = pool.spawn
    sup.stop_capture = pool.stop

    tasks = [
        asyncio.create_task(sup.run(), name="supervisor"),
        asyncio.create_task(scan_worker(sdr, sup), name="scanner"),
    ]
    if args.flex_host:
        from .ptt import discover_flex, flex_ptt_worker
        flex = args.flex_host
        if flex == "auto":
            flex = await discover_flex()
            print(f"[runtime] flex discovery: {flex or 'nothing heard'}")
        if flex:
            tasks.append(asyncio.create_task(
                flex_ptt_worker(sup, flex), name="ptt"))
    else:
        print("[runtime] no --flex-host (or CWATLAS_FLEX_HOST): TX hygiene "
              "disabled, hardware relay only")
    if args.lat == args.lat and args.lon == args.lon:  # NaN-safe "both set"
        tasks.append(asyncio.create_task(
            solar_worker(sup, args.lat, args.lon), name="solar"))
    else:
        print("[runtime] no --lat/--lon (or CWATLAS_LAT/LON): "
              "solar band weighting disabled, neutral priorities")
    if not args.trial and not args.no_mcp:
        from . import server
        server.attach(state, bus, sdr, catalog)
        tasks.append(asyncio.create_task(server.mcp.run_stdio_async(), name="mcp"))

    # graceful shutdown on SIGTERM/SIGINT (systemctl stop, ^C, plain kill):
    # fall through to the finally block so workers finalize in-flight
    # files/rows instead of orphaning them (the old way to make catalog junk)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        if args.trial:
            done, _ = await asyncio.wait(tasks, timeout=args.trial,
                                         return_when=asyncio.FIRST_EXCEPTION)
            for t in done:   # surface a crashed worker instead of a silent trial
                if not t.cancelled() and t.exception():
                    raise t.exception()
        else:
            waiter = asyncio.create_task(stop.wait(), name="stop-signal")
            done, _ = await asyncio.wait([*tasks, waiter],
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:   # a worker crashing also lands here — surface it
                if t is not waiter and not t.cancelled() and t.exception():
                    raise t.exception()
            if stop.is_set():
                print("[runtime] stop signal: shutting down cleanly")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.shutdown()   # workers finalize in-flight files/rows, then exit
        # Guarded: an end_run failure must not skip catalog.close()/sdr.aclose()
        # below. A run left open reads as "did not exit cleanly", which is a
        # smaller lie than a leaked SDR connection (whose channel is then held
        # ~1 min server-side).
        try:
            catalog.end_run()
        except sqlite3.Error as exc:
            print(f"[runtime] end_run failed ({exc!r}); run left open")
        print(f"[runtime] catalog: {catalog.stats()}")
        catalog.close()
        await sdr.aclose()


if __name__ == "__main__":
    asyncio.run(main())
