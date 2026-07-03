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

import os
import sqlite3
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
    since = (now or time.time()) - WINDOWS[window]
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
        "iq_hours": round(sum(r[2] for r in by_band) / 3600.0, 2),
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
