"""The migration mechanism, and migration 1 (provenance).

The load-bearing case is a v0 DB with 35k live rows in it — so these tests care
most about two things: that a v0 DB comes out the other side with every capture
adopted, and that a FAILED migration leaves a clean v0 rather than a half-built
schema nothing knows how to finish.
"""
from __future__ import annotations

import sqlite3

import pytest

from cwatlas_mcp import migrations
from cwatlas_mcp.catalog import SCHEMA, Catalog

from .conftest import _row


def _v0_db(path, n_rows=3):
    """A pre-provenance catalog: the frozen v0 baseline, plus captures."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO captures (freq_hz, band, started_utc, ended_utc, n_samples,"
        " srate_hz, path, strength_db, keyed_conf, contaminated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [_row(started_ago_s=600 * (i + 1)) for i in range(n_rows)])
    db.commit()
    db.close()


def _cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def test_v0_migrates_and_adopts_every_capture(tmp_path):
    path = tmp_path / "catalog.db"
    _v0_db(path, n_rows=3)
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0

    Catalog(path).close()

    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "run_id" in _cols(db, "captures")
    # no capture is left ambiguous between "collected before provenance" and
    # "the stamping is broken"
    assert db.execute("SELECT COUNT(*) FROM captures WHERE run_id IS NULL"
                      ).fetchone()[0] == 0
    runs = db.execute("SELECT id, kind, started_utc, ended_utc, config_json,"
                      " note FROM runs").fetchall()
    assert len(runs) == 1
    _id, kind, started, ended, config_json, note = runs[0]
    assert kind == "synthetic"
    assert config_json is None          # never recorded, and not invented
    assert "UNRECORDED" in note
    # the synthetic run's span is the captures it covers, read from the data
    lo, hi = db.execute("SELECT MIN(started_utc), MAX(started_utc)"
                        " FROM captures").fetchone()
    assert (started, ended) == (lo, hi)


def test_fresh_db_migrates_with_no_synthetic_run(tmp_path):
    """The empty-table case: a bare aggregate SELECT still yields one all-NULL
    row, so a SQL-side backfill would insert a NOT NULL violation here."""
    Catalog(tmp_path / "catalog.db").close()

    db = sqlite3.connect(tmp_path / "catalog.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "run_id" in _cols(db, "captures")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "catalog.db"
    _v0_db(path, n_rows=3)
    Catalog(path).close()
    Catalog(path).close()          # re-running SCHEMA + migrate must be a no-op

    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM runs WHERE kind='synthetic'"
                      ).fetchone()[0] == 1


def test_failed_migration_rolls_back_to_clean_v0(tmp_path, monkeypatch):
    """DDL, backfill and the version bump land together or not at all.

    Guards a specific trap: executescript() implicitly COMMITs the pending
    transaction, so building the tables that way would leave them behind after
    this rollback (and make the ROLLBACK itself raise over the real exception).
    """
    path = tmp_path / "catalog.db"
    _v0_db(path, n_rows=3)

    def _boom(db):
        for stmt in migrations._M1_DDL:
            db.execute(stmt)
        db.execute("ALTER TABLE captures ADD COLUMN run_id INTEGER")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(migrations, "MIGRATIONS", [_boom])
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        Catalog(path)

    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "run_id" not in _cols(db, "captures")        # the ALTER rolled back
    assert db.execute("SELECT name FROM sqlite_master WHERE name='runs'"
                      ).fetchone() is None              # so did the CREATEs
    assert db.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 3


def test_concurrent_opens_migrate_once(tmp_path):
    """Two collectors starting at once must not both migrate. BEGIN IMMEDIATE
    takes the write lock before user_version is read, so the loser blocks and
    then sees v1."""
    path = tmp_path / "catalog.db"
    _v0_db(path, n_rows=3)

    a, b = Catalog(path), Catalog(path)
    try:
        db = sqlite3.connect(path)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        a.close()
        b.close()


def test_capture_events_is_append_only(tmp_path):
    """Immutability enforced by the DB, not by everyone remembering."""
    cat = Catalog(tmp_path / "catalog.db")
    try:
        cap_id = cat.start_capture(freq_hz=14_030_000.0, band="20m", srate_hz=1500,
                                   path="x", strength_db=12.0, keyed_conf=0.8)
        cat._db.execute(
            "INSERT INTO capture_events (capture_id, ts, event_type, actor)"
            " VALUES (?,?,?,?)", (cap_id, 1.0, "reviewed", "human:test"))
        cat._db.commit()

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            cat._db.execute("UPDATE capture_events SET actor='human:someone-else'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            cat._db.execute("DELETE FROM capture_events")
    finally:
        cat.close()
