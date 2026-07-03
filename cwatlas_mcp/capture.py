"""Capture plane: one persistent worker per RX channel slot.

THE cardinal rule (learned twice on hardware): never churn connections. Each
worker opens its /SND session once (lazily, on first assignment) and RETUNES IN
PLACE for every subsequent capture — a closed connection's channel is held ~1 min
server-side and rapid open/close starves the whole device.

Worker protocol (via its inbox queue):
    Detection  -> start capturing it (retune; finalize any current file first)
    None       -> release: finalize current file, go idle (connection stays open,
                  incoming IQ is drained and discarded)
    SHUTDOWN   -> finalize and exit

Each capture writes a SigMF pair (<name>.sigmf-data ci16_le + .sigmf-meta JSON)
and a catalog row. TX hygiene: if ChannelState.contaminated is set mid-file (the
supervisor marks it on PTT), the row/meta are flagged; the IQ is kept but stays
out of the clean training set.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .catalog import Catalog
from .dsp import CARRIER_OUT_HZ, FS_OUT, CwDecimator
from .models import ChannelMode, ChannelState, Detection
from .sdr_client import IqSession, SdrClient

SHUTDOWN = object()          # inbox sentinel: exit the worker
KEEPALIVE_EVERY_S = 10.0     # app-level liveness (firmware ignores WS pings)
ROTATE_S = 600.0             # cap file segments at ~10 min; capture continues


def _sigmf_meta(det: Detection, srate_hz: int, started_utc: float,
                gpssec: int | None, contaminated: bool, n_samples: int) -> dict:
    return {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": srate_hz,
            "core:version": "1.0.0",
            "core:description": "CWAtlas CW capture (Web-888, AGC off; device "
                                "passband 750-1250 Hz shifted -750 Hz and "
                                "decimated 12k->1.5k on capture; CW carrier at "
                                f"~+{CARRIER_OUT_HZ:.0f} Hz baseband)",
            "core:recorder": "cwatlas-collector",
            "cwatlas:band": det.band,
            "cwatlas:strength_db": det.strength_db,
            "cwatlas:keyed_confidence": det.keyed_confidence,
            "cwatlas:contaminated": contaminated,
            "cwatlas:gps_start_sec": gpssec,
            "cwatlas:carrier_offset_hz": CARRIER_OUT_HZ,
            "cwatlas:decimated_from_hz": 12000,
        },
        "captures": [{
            "core:sample_start": 0,
            # baseband 0 Hz corresponds to this RF; carrier at +CARRIER_OUT_HZ
            "core:frequency": det.freq_hz - CARRIER_OUT_HZ,
            "core:datetime": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_utc)),
        }],
        "annotations": [{
            "core:sample_start": 0,
            "core:sample_count": n_samples,
            "core:freq_lower_edge": det.freq_hz - 250.0,
            "core:freq_upper_edge": det.freq_hz + 250.0,
            "core:label": "CW candidate",
        }],
    }


async def _write_capture(session: IqSession, cs: ChannelState,
                         det: Detection, catalog: Catalog, data_dir: Path,
                         inbox: asyncio.Queue, stall_s: float,
                         rotate_s: float = ROTATE_S):
    """Write one capture file until interrupted, stalled, or rotate_s elapses.

    Returns (interrupting command, reason) with reason in "inbox" | "stall" |
    "rotate". Command is Detection | None | SHUTDOWN (only for "inbox").
    Always finalizes its file and catalog row. ConnectionClosed propagates to
    the worker (which reopens the session). IQ is decimated 12k -> 1.5k inline
    (see dsp.py) — the wire is 12 kHz but the disk doesn't have to be.
    """
    started = time.time()
    name = (f"{det.band}_{det.freq_hz/1e3:.2f}kHz_"
            f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(started))}_ch{cs.ch}")
    day_dir = data_dir / time.strftime("%Y-%m-%d", time.gmtime(started))
    day_dir.mkdir(parents=True, exist_ok=True)
    base = day_dir / name

    cap_id = catalog.start_capture(
        freq_hz=det.freq_hz, band=det.band, srate_hz=FS_OUT,
        path=str(base), strength_db=det.strength_db,
        keyed_conf=det.keyed_confidence)
    cs.capture_id = cap_id

    dec = CwDecimator()                # fresh filter state per file
    n_samples, smeter_sum, smeter_n = 0, 0, 0
    gps_first: tuple[int, int] | None = None
    contaminated = False
    nxt = None
    reason = "inbox"
    last_ka = time.time()
    try:
        with open(f"{base}.sigmf-data", "wb") as fd:
            while True:
                try:
                    nxt = inbox.get_nowait()   # reassignment/release/shutdown?
                    break
                except asyncio.QueueEmpty:
                    pass
                if time.time() - started >= rotate_s:
                    reason = "rotate"          # cap segment length; keep capturing
                    break
                try:
                    chunk = await asyncio.wait_for(session.next_chunk(),
                                                   timeout=stall_s)
                except asyncio.TimeoutError:
                    reason = "stall"           # no IQ for stall_s -> finalize
                    break
                out = dec.process(chunk.data)
                fd.write(out)
                n_samples += len(out) // 4
                smeter_sum += chunk.smeter
                smeter_n += 1
                if gps_first is None and chunk.gps_solution:
                    gps_first = (chunk.gpssec, chunk.gpsnsec)
                    catalog.set_gps_start(cap_id, *gps_first)
                if cs.contaminated and not contaminated:
                    contaminated = True        # operator TX overlapped this file
                    catalog.mark_contaminated(cap_id)
                if time.time() - last_ka > KEEPALIVE_EVERY_S:
                    await session.ws.send("SET keepalive")
                    last_ka = time.time()
    finally:
        meta = _sigmf_meta(det, FS_OUT, started,
                           gps_first[0] if gps_first else None,
                           contaminated, n_samples)
        with open(f"{base}.sigmf-meta", "w") as fm:
            json.dump(meta, fm, indent=1)
        catalog.finalize_capture(
            cap_id, n_samples=n_samples, contaminated=contaminated,
            smeter_avg=(smeter_sum / smeter_n) if smeter_n else None)
        cs.capture_id = None
        print(f"[capture ch{cs.ch}] {name}: {n_samples} samples "
              f"({n_samples / FS_OUT:.1f}s @{FS_OUT}Hz)"
              f"{' STALLED' if reason == 'stall' else ''}"
              f"{' ->rotate' if reason == 'rotate' else ''}"
              f"{' CONTAMINATED' if contaminated else ''}")
    return nxt, reason


async def channel_worker(sdr: SdrClient, cs: ChannelState, inbox: asyncio.Queue,
                         catalog: Catalog, data_dir: Path,
                         stall_s: float = 20.0, rotate_s: float = ROTATE_S) -> None:
    """Own one RX channel slot for the process lifetime."""
    session: IqSession | None = None
    cmd: object = None
    last_ka = time.time()
    try:
        while True:
            # ---- idle: wait for an assignment ----------------------------
            if cmd is None:
                if session is None:
                    cmd = await inbox.get()    # no connection yet: just block
                else:
                    # keep draining IQ so the server doesn't back up on us
                    try:
                        cmd = inbox.get_nowait()
                    except asyncio.QueueEmpty:
                        try:
                            await asyncio.wait_for(session.next_chunk(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                        except Exception:      # socket died while idle
                            session = None
                        if session and time.time() - last_ka > KEEPALIVE_EVERY_S:
                            await session.ws.send("SET keepalive")
                            last_ka = time.time()
                        continue
            if cmd is SHUTDOWN:
                return
            if cmd is None:
                # release arrived while already idle (stall self-release raced
                # the supervisor's stop): nothing to do, keep the session
                continue
            det: Detection = cmd  # type: ignore[assignment]

            # ---- (re)acquire the session, retune in place ----------------
            try:
                if session is None:
                    session = await sdr.capture_iq(det.freq_hz, half_bw_hz=250.0)
                else:
                    await session.retune(det.freq_hz)
            except Exception as exc:
                backoff = 65 + cs.ch * 7       # decorrelate retries across workers
                print(f"[capture ch{cs.ch}] session error ({exc!r}); "
                      f"backing off {backoff}s (channel-hold window)")
                session = None
                self_release(cs, det)
                cmd = None
                await asyncio.sleep(backoff)
                continue

            # ---- capture until interrupted -------------------------------
            try:
                cmd, reason = await _write_capture(session, cs, det, catalog,
                                                   data_dir, inbox, stall_s,
                                                   rotate_s)
            except Exception as exc:           # ConnectionClosed mid-capture etc.
                backoff = 65 + cs.ch * 7       # decorrelate retries across workers
                print(f"[capture ch{cs.ch}] stream error ({exc!r}); "
                      f"reconnecting after {backoff}s")
                session = None
                self_release(cs, det)
                cmd = None
                await asyncio.sleep(backoff)
                continue
            if reason == "rotate":
                cmd = det       # same assignment, next file segment (no retune)
            elif cmd is None and reason == "stall":
                # worker-initiated stop: free the slot ONLY if the supervisor
                # hasn't already retasked it (never clobber a fresh assignment —
                # doing so caused double-assign churn in trial 2)
                self_release(cs, det)
            # cmd is None from a supervisor release: it already set IDLE; hands off.
    finally:
        if session is not None:
            await session.close()


def self_release(cs: ChannelState, det: Detection) -> None:
    """Mark the slot idle, but only if it's still ours (see clobber note above)."""
    if (cs.mode in (ChannelMode.CAPTURING, ChannelMode.DEEP_DWELL)
            and cs.freq_hz == det.freq_hz):
        cs.mode = ChannelMode.IDLE
        cs.freq_hz = None
