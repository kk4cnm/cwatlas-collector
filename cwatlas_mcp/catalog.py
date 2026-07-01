"""SQLite catalog of captures — the index into the MorseBase raw-IQ corpus.

One row per capture session (one channel dwell on one frequency). The IQ itself
lives in SigMF file pairs on disk; the catalog is how anything finds it again.
sqlite3 + WAL is plenty at collection rates (a handful of rows/minute, tops).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id            INTEGER PRIMARY KEY,
    freq_hz       REAL NOT NULL,          -- RF of the detected signal (bin center)
    band          TEXT NOT NULL,          -- "20m", ...
    started_utc   REAL NOT NULL,          -- unix ts (host clock)
    ended_utc     REAL,                   -- NULL while in flight
    gps_start_sec INTEGER,                -- gpssec of first IQ chunk (GPS-disciplined)
    gps_start_nsec INTEGER,
    n_samples     INTEGER DEFAULT 0,      -- complex samples written
    srate_hz      INTEGER NOT NULL,
    path          TEXT NOT NULL,          -- SigMF basename (no extension)
    strength_db   REAL,                   -- detection SNR that triggered capture
    keyed_conf    REAL,                   -- detector confidence at trigger time
    contaminated  INTEGER DEFAULT 0,      -- operator TX overlapped this window
    smeter_avg    REAL
);
CREATE INDEX IF NOT EXISTS idx_captures_band_time ON captures(band, started_utc);
"""


class Catalog:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def start_capture(self, *, freq_hz: float, band: str, srate_hz: int,
                      path: str, strength_db: float, keyed_conf: float) -> int:
        cur = self._db.execute(
            "INSERT INTO captures (freq_hz, band, started_utc, srate_hz, path,"
            " strength_db, keyed_conf) VALUES (?,?,?,?,?,?,?)",
            (freq_hz, band, time.time(), srate_hz, path, strength_db, keyed_conf))
        self._db.commit()
        return cur.lastrowid

    def set_gps_start(self, cap_id: int, gpssec: int, gpsnsec: int) -> None:
        self._db.execute(
            "UPDATE captures SET gps_start_sec=?, gps_start_nsec=? WHERE id=?",
            (gpssec, gpsnsec, cap_id))
        self._db.commit()

    def finalize_capture(self, cap_id: int, *, n_samples: int,
                         contaminated: bool, smeter_avg: float | None) -> None:
        self._db.execute(
            "UPDATE captures SET ended_utc=?, n_samples=?, contaminated=?,"
            " smeter_avg=? WHERE id=?",
            (time.time(), n_samples, int(contaminated), smeter_avg, cap_id))
        self._db.commit()

    def mark_contaminated(self, cap_id: int) -> None:
        self._db.execute(
            "UPDATE captures SET contaminated=1 WHERE id=?", (cap_id,))
        self._db.commit()

    def stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0),"
            " SUM(CASE WHEN ended_utc IS NULL THEN 1 ELSE 0 END),"
            " SUM(contaminated) FROM captures").fetchone()
        return {"captures": row[0], "total_samples": row[1],
                "in_flight": row[2] or 0, "contaminated": row[3] or 0}

    def close(self) -> None:
        self._db.close()
