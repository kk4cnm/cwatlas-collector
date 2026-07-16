"""Provenance health signals: silent regressions made visible."""
from __future__ import annotations

import sqlite3

from cwatlas_dash import sources
from cwatlas_mcp.catalog import Catalog

from .conftest import _row

RUN_COLS = ("kind", "started_utc", "ended_utc", "git_commit", "git_dirty",
            "sdr_firmware", "config_sha256")


def _run(db, *, kind="collector", started=1.0, ended=2.0, commit="a" * 40,
         dirty=0, firmware="2026.609", cfg="c" * 64):
    cur = db.execute(
        f"INSERT INTO runs ({','.join(RUN_COLS)}) VALUES (?,?,?,?,?,?,?)",
        (kind, started, ended, commit, dirty, firmware, cfg))
    return cur.lastrowid


def _capture(db, run_id, **kw):
    r = _row(started_ago_s=kw.pop("started_ago_s", 60))
    cur = db.execute(
        "INSERT INTO captures (freq_hz, band, started_utc, ended_utc, n_samples,"
        " srate_hz, path, strength_db, keyed_conf, contaminated, run_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*r, run_id))
    return cur.lastrowid


def _db(tmp_path):
    """A migrated catalog, opened for direct writes."""
    path = tmp_path / "catalog.db"
    Catalog(path).close()
    return path, sqlite3.connect(path)


def test_healthy_corpus_reports_ok(tmp_path):
    path, db = _db(tmp_path)
    run_id = _run(db, ended=None)          # the current run, still going
    _capture(db, run_id)
    db.commit()

    h = sources.provenance_health(db_path=path)
    assert h["ok"] is True
    assert h["unstamped_captures"] == 0
    assert h["unclean_exits"] == 0
    assert h["captures_from_dirty_code"] == 0
    assert h["current_run"] == {"id": run_id, "git_commit": "aaaaaaa",
                                "git_dirty": False, "sdr_firmware": "2026.609",
                                "config_sha256": "cccccccc"}


def test_unstamped_capture_is_an_integration_regression(tmp_path):
    """A capture written without a declared run. Post-backfill there is no
    legitimate NULL in the corpus, so this needs no deployment-time cutoff."""
    path, db = _db(tmp_path)
    _capture(db, None)
    db.commit()

    h = sources.provenance_health(db_path=path)
    assert h["unstamped_captures"] == 1
    assert h["ok"] is False


def test_current_run_being_open_is_not_an_unclean_exit(tmp_path):
    """The newest run having no ended_utc means "running", not "died" — the
    service panel resolves that, so it must not raise an alarm here."""
    path, db = _db(tmp_path)
    _run(db, ended=100.0)          # a previous run, exited cleanly
    _run(db, ended=None)           # the current one, still going
    db.commit()

    assert sources.provenance_health(db_path=path)["unclean_exits"] == 0


def test_an_older_open_run_is_an_unclean_exit(tmp_path):
    """Only one collector runs at a time, so a non-newest run with no ended_utc
    was killed. Legitimate history (SIGKILL, power) — but worth seeing."""
    path, db = _db(tmp_path)
    _run(db, ended=None)           # died without unwinding
    _run(db, ended=None)           # the current one
    db.commit()

    h = sources.provenance_health(db_path=path)
    assert h["unclean_exits"] == 1
    assert h["ok"] is True         # not an error — visible, not alarming


def test_a_dirty_current_run_is_not_ok(tmp_path):
    """IQ being recorded right now from code that exists nowhere in git —
    actionable: commit, restart."""
    path, db = _db(tmp_path)
    clean, dirty = _run(db, dirty=0), _run(db, dirty=1, ended=None)
    _capture(db, clean)
    _capture(db, dirty)
    _capture(db, dirty)
    db.commit()

    h = sources.provenance_health(db_path=path)
    assert h["captures_from_dirty_code"] == 2
    assert h["ok"] is False
    assert h["current_run"]["git_dirty"] is True


def test_past_dirty_captures_do_not_pin_ok_false_forever(tmp_path):
    """The count can never go down — a past dirty run must not leave the panel
    permanently red with nothing anyone can do. An alarm that cannot be cleared
    is one people stop reading; `ok` tracks only what's actionable now.

    (Learned live: run 5 recorded 21 captures from an uncommitted tree, and the
    first cut of this metric would have been red for the rest of the project.)
    """
    path, db = _db(tmp_path)
    old_dirty = _run(db, dirty=1, ended=100.0)      # happened; can't unhappen
    _capture(db, old_dirty)
    now_clean = _run(db, dirty=0, ended=None)       # current run, clean
    _capture(db, now_clean)
    db.commit()

    h = sources.provenance_health(db_path=path)
    assert h["captures_from_dirty_code"] == 1       # still reported, as history
    assert h["current_run"]["git_dirty"] is False
    assert h["ok"] is True                          # nothing to act on


def test_synthetic_run_is_not_mistaken_for_a_collector(tmp_path):
    """The synthetic run has no ended_utc semantics of a process and NULL git
    state; it must not read as an unclean exit or as dirty code."""
    path = tmp_path / "catalog.db"
    raw = sqlite3.connect(path)
    from cwatlas_mcp.catalog import SCHEMA
    raw.executescript(SCHEMA)
    raw.executemany(
        "INSERT INTO captures (freq_hz, band, started_utc, ended_utc, n_samples,"
        " srate_hz, path, strength_db, keyed_conf, contaminated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", [_row(started_ago_s=600)])
    raw.commit()
    raw.close()
    Catalog(path).close()          # migrates; creates the synthetic run

    h = sources.provenance_health(db_path=path)
    assert h["unstamped_captures"] == 0      # adopted, not left NULL
    assert h["unclean_exits"] == 0           # kind='synthetic' is excluded
    assert h["captures_from_dirty_code"] == 0
    assert h["current_run"] is None          # no collector run has happened yet
    assert h["ok"] is True


def test_empty_catalog_does_not_crash(tmp_path):
    path, db = _db(tmp_path)
    db.close()
    h = sources.provenance_health(db_path=path)
    assert h["ok"] is True
    assert h["current_run"] is None
