"""SQLite catalog of captures — the index into the MorseBase raw-IQ corpus.

One row per capture session (one channel dwell on one frequency). The IQ itself
lives in SigMF file pairs on disk; the catalog is how anything finds it again.
sqlite3 + WAL is plenty at collection rates (a handful of rows/minute, tops).

Also holds provenance: a `runs` row per collector process describing what was
running (see provenance.py), which every capture points at via run_id. See
docs/provenance.md.

SCHEMA below is the FROZEN v0 baseline — never edit it. Schema changes go in
migrations.py; see the note there.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .migrations import migrate

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
    # NB foreign_keys is left OFF (sqlite's default). The REFERENCES clauses in
    # the schema are documentation. Turning enforcement on would mean a bad
    # run_id fails every start_capture INSERT — a provenance bug taking
    # collection down with it, which is exactly backwards.

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)      # v0 baseline; frozen, see migrations.py
        self._db.commit()
        migrate(self._db)
        # Set by begin_run(). Stays None for a Catalog opened without one (tests,
        # scripts, the dash): captures then record run_id NULL, which is correct —
        # no run was declared, so there is nothing to point at.
        self._run_id: int | None = None

    # ---- provenance ---------------------------------------------------------

    def begin_run(self, info: dict) -> int:
        """Record what's running (see provenance.build_run_info) and stamp every
        capture from here on with it.

        Deliberately unguarded: this writes to the same DB start_capture needs,
        so if it can't INSERT the collector is not going to collect anything
        either. Failing loudly at startup beats discovering it 18 hours later.
        """
        cols = ", ".join(info)
        cur = self._db.execute(
            f"INSERT INTO runs ({cols}) VALUES ({', '.join('?' * len(info))})",
            tuple(info.values()))
        self._db.commit()
        self._run_id = cur.lastrowid
        return self._run_id

    def end_run(self) -> None:
        """Close the current run. Leaving ended_utc NULL is meaningful — it says
        the process died without unwinding (SIGKILL, OOM, power) — so this is
        only ever called from a clean shutdown."""
        if self._run_id is None:
            return
        self._db.execute("UPDATE runs SET ended_utc=? WHERE id=?",
                         (time.time(), self._run_id))
        self._db.commit()

    # ---- captures -----------------------------------------------------------

    def start_capture(self, *, freq_hz: float, band: str, srate_hz: int,
                      path: str, strength_db: float, keyed_conf: float) -> int:
        cur = self._db.execute(
            "INSERT INTO captures (freq_hz, band, started_utc, srate_hz, path,"
            " strength_db, keyed_conf, run_id) VALUES (?,?,?,?,?,?,?,?)",
            (freq_hz, band, time.time(), srate_hz, path, strength_db, keyed_conf,
             self._run_id))
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

    def mark_contaminated(self, cap_id: int, *, actor: str = "collector",
                          details: dict | None = None) -> None:
        """Latch the contamination flag, then record who said so and why.

        ORDERING IS LOAD-BEARING — see add_events(). The flag is committed on its
        own before the event is attempted; the two must never share a
        transaction.
        """
        self._db.execute(
            "UPDATE captures SET contaminated=1 WHERE id=?", (cap_id,))
        self._db.commit()          # <-- the boundary: the flag is durable HERE
        self.add_events([cap_id], "contaminated", actor, details)

    def mark_window(self, start_ts: float, end_ts: float, *,
                    actor: str = "agent", reason: str | None = None) -> int:
        """Flag every capture whose recording overlaps [start_ts, end_ts]
        (agent-reported contamination, e.g. a TX the PTT ingest missed).

        -> the number of captures NEWLY flagged.
        """
        # `AND contaminated=0` so re-running a window doesn't re-emit an event
        # for rows already flagged (and so the count means "newly flagged"
        # rather than "matched"). RETURNING needs sqlite >= 3.35; this host is
        # 3.37. rowcount is unreliable with RETURNING — count the rows instead.
        rows = self._db.execute(
            "UPDATE captures SET contaminated=1 WHERE started_utc <= ?"
            " AND COALESCE(ended_utc, strftime('%s','now')) >= ?"
            " AND contaminated=0 RETURNING id", (end_ts, start_ts)).fetchall()
        self._db.commit()          # <-- flags durable before any event is tried
        cap_ids = [r[0] for r in rows]
        self.add_events(cap_ids, "contaminated", actor,
                        {"reason": reason, "window": [start_ts, end_ts]})
        return len(cap_ids)

    def add_events(self, cap_ids: list[int], event_type: str, actor: str,
                   details: dict | None = None) -> int:
        """Append to the immutable log, best-effort. -> events written.

        BEST-EFFORT ON PURPOSE. Provenance must never stop collection, and it
        must never cost us a fact the catalog itself is carrying. Two rules
        follow, and both are easy to "clean up" into bugs:

        1. This can only ever print. mark_contaminated is called from inside
           capture.py's write loop; raising here would surface as a stream error
           and buy a 65 s reconnect backoff — trading dead radio time for a
           provenance hiccup.

        2. Callers MUST commit the state change BEFORE calling this, in a
           separate transaction. Never write `UPDATE captures ...; INSERT INTO
           capture_events ...; commit()` — the source reads flag-first but it is
           ONE implicit transaction, so an event failure rolls the flag back
           with it. The flag is what keeps contaminated IQ out of the training
           set; the event is a note about the flag. Losing the note is an
           annoyance, losing the flag is a poisoned corpus. HYGIENE BEATS
           PROVENANCE: a crash between the two commits is the correct outcome.
        """
        if not cap_ids:
            return 0
        blob = json.dumps(details, sort_keys=True) if details else None
        ts = time.time()
        try:
            self._db.executemany(
                "INSERT INTO capture_events (capture_id, ts, event_type, actor,"
                " run_id, details_json) VALUES (?,?,?,?,?,?)",
                [(c, ts, event_type, actor, self._run_id, blob) for c in cap_ids])
            self._db.commit()
        except sqlite3.Error as exc:
            # rollback() the METHOD, not execute("ROLLBACK"): if the INSERT died
            # before its implicit BEGIN there is no transaction, and the
            # statement form raises "cannot rollback - no transaction is active"
            # right over the top of the real error. The method is a no-op.
            self._db.rollback()
            print(f"[catalog] {event_type} event for {len(cap_ids)} capture(s) "
                  f"NOT recorded ({exc!r}); the capture rows are unaffected")
            return 0
        return len(cap_ids)

    def window_stats(self, since_ts: float) -> dict:
        """Coverage/throughput summary for the MCP get_collection_stats tool."""
        totals = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0), SUM(contaminated)"
            " FROM captures WHERE started_utc >= ?", (since_ts,)).fetchone()
        by_band = self._db.execute(
            "SELECT band, COUNT(*), COALESCE(SUM(n_samples),0),"
            " COALESCE(SUM(n_samples * 1.0 / srate_hz), 0)"
            " FROM captures WHERE started_utc >= ? GROUP BY band"
            " ORDER BY 3 DESC", (since_ts,)).fetchall()
        return {
            "captures": totals[0],
            "iq_hours": round(sum(r[3] for r in by_band) / 3600.0, 1),
            "bytes": totals[1] * 4,          # ci16: 4 bytes per complex sample
            "contaminated": totals[2] or 0,
            "by_band": {r[0]: {"captures": r[1],
                               "iq_hours": round(r[3] / 3600.0, 2)}
                        for r in by_band},
        }

    def stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0),"
            " SUM(CASE WHEN ended_utc IS NULL THEN 1 ELSE 0 END),"
            " SUM(contaminated) FROM captures").fetchone()
        return {"captures": row[0], "total_samples": row[1],
                "in_flight": row[2] or 0, "contaminated": row[3] or 0}

    def close(self) -> None:
        self._db.close()
