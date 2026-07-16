"""MCP-shaped data layer for the dashboard.

Where an MCP observe tool exists, the function here returns the identical
shape (collection_stats == get_collection_stats, sdr_* == get_sdr_status /
get_adc_overload). When the collector grows an MCP-over-HTTP transport,
this module becomes an MCP client and nothing above it changes.

Read-only by construction: sqlite opened with mode=ro, SDR reached with
AJAX GETs only (a WebSocket would occupy one of the device's rx channel
slots — never do that from the dashboard).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path

DATA_DIR = Path(os.environ.get("CWATLAS_DATA_DIR", "~/cwatlas/data")).expanduser()
DB_PATH = DATA_DIR / "catalog.db"

WINDOWS = {"1h": 3600, "12h": 43200, "24h": 86400, "7d": 604800}

BYTES_PER_SAMPLE = 4  # ci16 IQ


# ============================ catalog (sqlite, ro) ============================
def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def collection_stats(window: str, db_path: Path | None = None,
                     now: float | None = None) -> dict:
    """Same shape as the MCP get_collection_stats tool / Catalog.window_stats."""
    since = (time.time() if now is None else now) - WINDOWS[window]
    with closing(_connect(db_path)) as db:
        tot = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0),"
            " COALESCE(SUM(contaminated),0)"
            " FROM captures WHERE started_utc >= ?", (since,)).fetchone()
        by_band = db.execute(
            "SELECT band, COUNT(*), COALESCE(SUM(n_samples * 1.0 / srate_hz), 0)"
            " FROM captures WHERE started_utc >= ? GROUP BY band"
            " ORDER BY 3 DESC", (since,)).fetchall()
    return {
        "window": window,
        "captures": tot[0],
        "iq_hours": round(sum(r[2] for r in by_band) / 3600.0, 1),
        "bytes": tot[1] * BYTES_PER_SAMPLE,
        "contaminated": tot[2],
        "by_band": {r[0]: {"captures": r[1], "iq_hours": round(r[2] / 3600.0, 2)}
                    for r in by_band},
    }


def totals(db_path: Path | None = None) -> dict:
    """All-time counters (Catalog.stats shape, plus iq_hours/bytes)."""
    with closing(_connect(db_path)) as db:
        row = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0),"
            " COALESCE(SUM(CASE WHEN ended_utc IS NULL THEN 1 ELSE 0 END),0),"
            " COALESCE(SUM(contaminated),0),"
            " COALESCE(SUM(n_samples * 1.0 / srate_hz),0)"
            " FROM captures").fetchone()
    return {"captures": row[0], "bytes": row[1] * BYTES_PER_SAMPLE,
            "in_flight": row[2], "contaminated": row[3],
            "iq_hours": round(row[4] / 3600.0, 2)}


def provenance_health(db_path: Path | None = None) -> dict:
    """Instrument-health signals derived from the provenance tables.

    These turn otherwise-silent regressions into visible ones. Each is a count
    that should be 0 in a healthy production corpus, plus the context needed to
    read it — see docs/provenance.md.
    """
    with closing(_connect(db_path)) as db:
        # An INTEGRATION REGRESSION. The m1 backfill adopted every pre-existing
        # capture into the synthetic run, and begin_run stamps every new one, so
        # there is no legitimate NULL anywhere in the corpus — past or future.
        # Nothing in production inserts captures except start_capture
        # (backfill_orphans only UPDATEs). A NULL here means the collector wrote
        # a capture without declaring a run: provenance is silently broken.
        # NB this needs no "since deployment" cutoff precisely because the
        # backfill refused to leave NULLs behind.
        unstamped = db.execute(
            "SELECT COUNT(*) FROM captures WHERE run_id IS NULL").fetchone()[0]

        # NOT AN ERROR, but operationally interesting: a run with no ended_utc
        # died without unwinding (SIGKILL, OOM, power). Only one collector runs
        # at a time, so any run that isn't the newest is unambiguously dead —
        # the newest one is either running right now or died, and `service`
        # in the same payload already says which.
        unclean = db.execute(
            "SELECT COUNT(*) FROM runs WHERE kind='collector'"
            " AND ended_utc IS NULL"
            " AND id < (SELECT MAX(id) FROM runs WHERE kind='collector')"
        ).fetchone()[0]

        # HISTORY, NOT AN ALARM. Non-zero means IQ in the corpus came from code
        # that exists nowhere in git — unreproducible by construction. Note the
        # "unfixable after the fact" part: this count can never go down, so it
        # must NOT gate `ok`. A single dirty run would otherwise leave the
        # dashboard permanently red with nothing anyone can do about it, which
        # is how a signal becomes wallpaper. What's actionable is whether the
        # run happening NOW is dirty; that's in `ok` via current_run below.
        from_dirty = db.execute(
            "SELECT COUNT(*) FROM captures c JOIN runs r ON c.run_id = r.id"
            " WHERE r.git_dirty = 1").fetchone()[0]

        current = db.execute(
            "SELECT id, git_commit, git_dirty, sdr_firmware, config_sha256"
            " FROM runs WHERE kind='collector' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return {
        "unstamped_captures": unstamped,
        "unclean_exits": unclean,
        "captures_from_dirty_code": from_dirty,
        # Only conditions someone can act on right now: provenance is broken, or
        # the collector is currently recording IQ from uncommitted code.
        "ok": unstamped == 0 and not (current and current[2]),
        "current_run": ({"id": current[0],
                         "git_commit": (current[1] or "")[:7] or None,
                         "git_dirty": bool(current[2]),
                         "sdr_firmware": current[3],
                         "config_sha256": (current[4] or "")[:8] or None}
                        if current else None),
    }


def hourly_buckets(db_path: Path | None = None, hours: int = 24,
                   now: float | None = None) -> list[dict]:
    """Capture-rate buckets for the last `hours` hours, oldest first."""
    t = now if now is not None else time.time()
    with closing(_connect(db_path)) as db:
        rows = db.execute(
            "SELECT CAST((? - started_utc) / 3600 AS INTEGER) AS ago,"
            " COUNT(*), COALESCE(SUM(contaminated),0),"
            " COALESCE(SUM(n_samples * 1.0 / srate_hz),0)"
            " FROM captures WHERE started_utc >= ? AND started_utc <= ?"
            " GROUP BY ago", (t, t - hours * 3600, t)).fetchall()
    got = {r[0]: r for r in rows}
    return [
        {"ago_h": ago,
         "captures": got[ago][1] if ago in got else 0,
         "contaminated": got[ago][2] if ago in got else 0,
         "iq_hours": round((got[ago][3] if ago in got else 0) / 3600.0, 2)}
        for ago in range(hours - 1, -1, -1)
    ]


def inflight(db_path: Path | None = None, now: float | None = None,
             stale_after_s: float = 1200.0) -> list[dict]:
    """Captures currently being written — the 'live channels' view.

    A row in flight for > stale_after_s (2x the collector's 600 s rotate
    period) is almost certainly an orphan from a crash: flag it."""
    t = now if now is not None else time.time()
    with closing(_connect(db_path)) as db:
        rows = db.execute(
            "SELECT id, freq_hz, band, started_utc, strength_db, keyed_conf"
            " FROM captures WHERE ended_utc IS NULL"
            " ORDER BY started_utc DESC").fetchall()
    return [
        {"id": r[0], "freq_hz": r[1], "band": r[2], "started_utc": r[3],
         "dwell_s": round(t - r[3], 1), "strength_db": r[4],
         "keyed_conf": r[5], "stale": (t - r[3]) > stale_after_s}
        for r in rows
    ]


def recent_captures(limit: int = 50, db_path: Path | None = None) -> list[dict]:
    """Last `limit` finalized captures, newest first."""
    with closing(_connect(db_path)) as db:
        rows = db.execute(
            "SELECT id, freq_hz, band, started_utc, ended_utc - started_utc,"
            " n_samples * 1.0 / srate_hz, strength_db, keyed_conf,"
            " contaminated, smeter_avg"
            " FROM captures WHERE ended_utc IS NOT NULL"
            " ORDER BY started_utc DESC LIMIT ?", (limit,)).fetchall()
    return [
        {"id": r[0], "freq_hz": r[1], "band": r[2], "started_utc": r[3],
         "duration_s": round(r[4], 1), "iq_hours": round(r[5] / 3600.0, 3),
         "strength_db": r[6], "keyed_conf": r[7],
         "contaminated": bool(r[8]), "smeter_avg": r[9]}
        for r in rows
    ]


# ========================= SDR (AJAX info plane only) =========================
# Cache so N browser tabs polling every 15 s produce at most one device hit
# per ttl. Failures are NOT cached: a down SDR is re-probed each poll (short
# timeout below bounds the stall).
_SDR_CACHE: dict[str, tuple[float, dict]] = {}
_SDR_LOCK = threading.Lock()  # single-flight under Flask threaded serving


def _fetch_sdr(host: str, port: int) -> dict:
    from cwatlas_mcp.sdr_client import SdrClient, SdrConfig

    async def go() -> dict:
        sdr = SdrClient(SdrConfig(host=host, port=port))
        try:
            return {"status": await sdr.get_status(),
                    "adc": await sdr.get_adc()}
        finally:
            await sdr.aclose()

    return asyncio.run(asyncio.wait_for(go(), timeout=4.0))


def sdr_snapshot(host: str, port: int = 8073, ttl_s: float = 10.0,
                 now=time.time) -> dict:
    """get_sdr_status + get_adc_overload equivalent, cached per host:port."""
    key = f"{host}:{port}"
    hit = _SDR_CACHE.get(key)                 # fast path: no lock
    if hit and now() - hit[0] < ttl_s:
        return hit[1]
    with _SDR_LOCK:                           # double-checked single-flight
        t = now()
        hit = _SDR_CACHE.get(key)
        if hit and t - hit[0] < ttl_s:
            return hit[1]
        snap = _fetch_sdr(host, port)
        _SDR_CACHE[key] = (t, snap)
        return snap


# ====================== system (systemd / journal / disk) =====================
# Error *signatures*, not substrings: Python stack dumps ("Traceback"),
# CamelCase exception class names (ValueError, RuntimeError — case-sensitive so
# prose like "no errors" doesn't hit), uppercase ERROR level tags, and systemd
# failure verbs ("Failed to start", "failure"). Deliberately does NOT match
# negations like "No errors detected" or "fail-safe".
_ERROR_PAT = re.compile(
    r"Traceback|\w*(?:Error|Exception)\b|\bERROR\b|(?i:\bfail(?:ed|ure)\b)")


def _monotonic_now_s() -> float:
    """Seconds since boot (CLOCK_MONOTONIC matches systemd's *Monotonic props)."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def system_health(unit: str = "cwatlas-collector",
                  data_dir: Path | None = None,
                  run=subprocess.run) -> dict:
    out = run(["systemctl", "show", unit, "-p",
               "ActiveState,SubState,NRestarts,MemoryCurrent,"
               "ExecMainStartTimestamp,ExecMainStartTimestampMonotonic"],
              capture_output=True, text=True, timeout=5).stdout
    kv = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)

    uptime_s = None
    mono_us = kv.get("ExecMainStartTimestampMonotonic", "0")
    if kv.get("ActiveState") == "active" and mono_us.isdigit() and int(mono_us):
        uptime_s = round(_monotonic_now_s() - int(mono_us) / 1e6, 0)

    mem = kv.get("MemoryCurrent", "")
    du = shutil.disk_usage(data_dir or DATA_DIR)
    return {
        "unit": unit,
        "active_state": kv.get("ActiveState", "unknown"),
        "sub_state": kv.get("SubState", "unknown"),
        "n_restarts": int(kv.get("NRestarts", "0") or 0),
        "memory_bytes": int(mem) if mem.isdigit() else None,
        "started_at": kv.get("ExecMainStartTimestamp") or None,
        "uptime_s": uptime_s,
        "disk": {"path": str(data_dir or DATA_DIR),
                 "total": du.total, "used": du.used, "free": du.free},
    }


def journal_tail(unit: str = "cwatlas-collector", n: int = 100,
                 run=subprocess.run) -> dict:
    proc = run(["journalctl", "-u", unit, "-n", str(n),
                "--no-pager", "-o", "short-iso"],
               capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(
            f"journal unreadable (is {os.environ.get('USER', 'the user')} in"
            f" the systemd-journal group?): {proc.stderr.strip()}")
    lines = proc.stdout.splitlines()
    return {"lines": lines,
            "errors": sum(1 for ln in lines if _ERROR_PAT.search(ln))}


# ================================ solar =======================================
def solar_priorities(lat: float, lon: float) -> dict:
    """Recomputed solar baseline (same math the collector's solar_worker runs).

    Live agent nudges are supervisor in-process state — unreachable without
    MCP — so nudges is always None here; the UI says so rather than showing 1.0."""
    from cwatlas_mcp.solar import band_weights

    phase, weights = band_weights(lat, lon)
    return {"phase": phase, "weights": weights, "nudges": None,
            "note": "solar baseline only; live nudges require MCP"}
