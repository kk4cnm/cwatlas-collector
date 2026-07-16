import sqlite3
import time

import pytest

from cwatlas_mcp.catalog import SCHEMA, Catalog

NOW = 1_751_500_000.0  # fixed "now" so window edges are deterministic


def _row(*, started_ago_s, dur_s=60.0, band="20m", freq_hz=14_030_000.0,
         srate=12_000, contaminated=0, in_flight=False):
    """One captures row: started `started_ago_s` before NOW, `dur_s` long."""
    started = NOW - started_ago_s
    ended = None if in_flight else started + dur_s
    n_samples = 0 if in_flight else int(dur_s * srate)
    return (freq_hz, band, started, ended, n_samples, srate,
            f"cap_{band}_{int(started)}", 12.0, 0.8, contaminated)


@pytest.fixture
def fixture_db(tmp_path):
    """catalog.db with rows straddling every window edge.

    Built at the v0 baseline and then migrated, which is what the dash always
    reads in production: the collector migrates catalog.db at startup, before
    anything else opens it. (Left unmigrated, the provenance panel degrades to
    {"error": no such column: run_id} and the dash tests assert nothing.)
    """
    db_path = tmp_path / "catalog.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    rows = [
        _row(started_ago_s=600),                          # inside 1h
        _row(started_ago_s=600, band="40m",
             freq_hz=7_030_000.0, contaminated=1),        # inside 1h, contaminated
        _row(started_ago_s=6 * 3600),                     # inside 12h only
        _row(started_ago_s=20 * 3600, band="40m",
             freq_hz=7_040_000.0),                        # inside 24h only
        _row(started_ago_s=3 * 86400),                    # inside 7d only
        _row(started_ago_s=10 * 86400),                   # outside all windows
        _row(started_ago_s=120, in_flight=True),          # in flight now
    ]
    db.executemany(
        "INSERT INTO captures (freq_hz, band, started_utc, ended_utc,"
        " n_samples, srate_hz, path, strength_db, keyed_conf, contaminated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    Catalog(db_path).close()      # v0 -> current; adopts the rows as pre-provenance
    return db_path
