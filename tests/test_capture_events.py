"""The event log's writers, and the ordering rule that protects the corpus.

The load-bearing test here is test_flag_is_durable_before_the_event_is_tried:
contamination is data hygiene, the event is a note about it. If an event write
can take the flag down with it, contaminated IQ silently reaches the training
set. That has to be impossible by construction, not by convention.
"""
from __future__ import annotations

import sqlite3

import pytest

from cwatlas_mcp.catalog import Catalog


@pytest.fixture
def cat(tmp_path):
    c = Catalog(tmp_path / "catalog.db")
    yield c
    c.close()


def _cap(cat, **kw):
    return cat.start_capture(freq_hz=kw.get("freq_hz", 14_030_000.0), band="20m",
                             srate_hz=1500, path=kw.get("path", "x"),
                             strength_db=12.0, keyed_conf=0.8)


def _events(db, cap_id=None):
    q = ("SELECT capture_id, event_type, actor, details_json FROM capture_events"
         + (" WHERE capture_id=?" if cap_id else "") + " ORDER BY id")
    return db.execute(q, (cap_id,) if cap_id else ()).fetchall()


# sqlite3.Connection's methods are read-only and can't be monkeypatched, which
# is just as well — these break the log the way it could really break, and the
# two modes exercise different halves of the except path.

def _break_log_mid_statement(cat):
    """INSERTs abort while executing -> a transaction is left OPEN."""
    cat._db.execute("CREATE TRIGGER _boom BEFORE INSERT ON capture_events "
                    "BEGIN SELECT RAISE(ABORT,'disk I/O error'); END")
    cat._db.commit()


def _break_log_at_prepare(cat):
    """INSERTs fail to compile -> NO transaction is ever opened. (Triggers
    guard the rows; they don't stop DROP.)"""
    cat._db.execute("DROP TABLE capture_events")
    cat._db.commit()


# ---- the ordering rule ------------------------------------------------------

def test_flag_is_durable_before_the_event_is_tried(tmp_path):
    """THE regression this whole design turns on.

    Asserting contaminated==1 on the *writing* connection would prove nothing —
    an uncommitted UPDATE is perfectly visible to the connection that made it.
    So read through a SECOND connection, opened while the first is still open:
    it can only see committed data. If the flag and the event ever share a
    transaction, the event's failure rolls the flag back and this fails.
    """
    path = tmp_path / "catalog.db"
    cat = Catalog(path)
    try:
        cap_id = _cap(cat)
        _break_log_mid_statement(cat)

        cat.mark_contaminated(cap_id)      # must not raise

        # while the first connection is STILL OPEN and unclosed: closing cat
        # first would resolve its transaction either way and destroy the very
        # evidence we're after
        other = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert other.execute("SELECT contaminated FROM captures WHERE id=?",
                                 (cap_id,)).fetchone()[0] == 1
            assert other.execute("SELECT COUNT(*) FROM capture_events"
                                 ).fetchone()[0] == 0
        finally:
            other.close()
    finally:
        cat.close()


def test_window_flags_are_durable_before_events_are_tried(tmp_path):
    """Same rule on the bulk path: an agent flagging 200 captures must not lose
    all 200 flags because the log hiccuped."""
    path = tmp_path / "catalog.db"
    cat = Catalog(path)
    try:
        ids = [_cap(cat, path=f"c{i}") for i in range(3)]
        for cap_id in ids:
            cat.finalize_capture(cap_id, n_samples=1000, contaminated=False,
                                 smeter_avg=-90.0)
        _break_log_mid_statement(cat)

        assert cat.mark_window(0, 2 ** 31, reason="whatever") == 3

        other = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert other.execute(
                "SELECT COUNT(*) FROM captures WHERE contaminated=1"
            ).fetchone()[0] == 3
            assert other.execute("SELECT COUNT(*) FROM capture_events"
                                 ).fetchone()[0] == 0
        finally:
            other.close()
    finally:
        cat.close()


def test_event_failure_never_raises_into_the_capture_path(cat, capsys):
    """mark_contaminated is called from capture.py's write loop. Raising would
    surface as a stream error and buy a 65 s reconnect backoff — dead radio
    time traded for a provenance hiccup."""
    cap_id = _cap(cat)
    _break_log_mid_statement(cat)

    cat.mark_contaminated(cap_id)          # no exception

    out = capsys.readouterr().out
    assert "NOT recorded" in out
    assert "capture rows are unaffected" in out


def test_rollback_after_a_failed_event_does_not_mask_the_error(cat, capsys):
    """The except path must use rollback() the method, not execute("ROLLBACK").

    Dropping the table makes the INSERT fail at PREPARE time, so no transaction
    is ever opened — and that is exactly when execute("ROLLBACK") raises
    "cannot rollback - no transaction is active" over the top of the real error.
    """
    cap_id = _cap(cat)
    _break_log_at_prepare(cat)

    cat.mark_contaminated(cap_id)

    out = capsys.readouterr().out
    assert "no such table: capture_events" in out    # the REAL error surfaced
    assert "cannot rollback" not in out


# ---- mark_contaminated ------------------------------------------------------

def test_mark_contaminated_records_who_and_why(cat):
    cap_id = _cap(cat)
    cat.mark_contaminated(cap_id, actor="collector",
                          details={"source": "ptt", "ch": 3})

    rows = _events(cat._db, cap_id)
    assert len(rows) == 1
    assert rows[0][1:3] == ("contaminated", "collector")
    assert '"source": "ptt"' in rows[0][3]
    assert cat._db.execute("SELECT contaminated FROM captures WHERE id=?",
                           (cap_id,)).fetchone()[0] == 1


def test_events_carry_the_run_that_wrote_them(cat):
    """The log gets provenance too."""
    from cwatlas_mcp import provenance
    from cwatlas_mcp.scheduler import SchedulerConfig
    import argparse
    args = argparse.Namespace(host="h", port=1, rotate_s=600.0, lat=0.0, lon=0.0,
                              trial=0.0, no_mcp=True, flex_host="")
    run_id = cat.begin_run(provenance.build_run_info(
        args, {"rx_chans": 12}, SchedulerConfig(), [("20m", 14e6)]))
    cap_id = _cap(cat)
    cat.mark_contaminated(cap_id)

    assert cat._db.execute("SELECT run_id FROM capture_events WHERE capture_id=?",
                           (cap_id,)).fetchone()[0] == run_id


# ---- mark_window ------------------------------------------------------------

def test_mark_window_emits_one_event_per_capture_with_the_reason(cat):
    """server.py accepts a `reason` for retroactively flagging an unbounded
    number of captures; it used to be dropped on the floor."""
    a, b = _cap(cat, path="a"), _cap(cat, path="b")
    for cap_id in (a, b):
        cat.finalize_capture(cap_id, n_samples=1000, contaminated=False,
                             smeter_avg=-90.0)

    n = cat.mark_window(0, 2 ** 31, actor="agent:mark_window_contaminated",
                        reason="unlogged TX from the neighbour's amp")

    assert n == 2
    rows = _events(cat._db)
    assert len(rows) == 2
    assert {r[0] for r in rows} == {a, b}
    assert all(r[2] == "agent:mark_window_contaminated" for r in rows)
    assert "neighbour" in rows[0][3]


def test_mark_window_is_idempotent_and_does_not_re_emit(cat):
    """Re-running a window must not stack duplicate events on rows already
    flagged — the `AND contaminated=0` guard."""
    cap_id = _cap(cat)
    cat.finalize_capture(cap_id, n_samples=1000, contaminated=False,
                         smeter_avg=-90.0)

    assert cat.mark_window(0, 2 ** 31, reason="first") == 1
    assert cat.mark_window(0, 2 ** 31, reason="second") == 0     # nothing new

    rows = _events(cat._db, cap_id)
    assert len(rows) == 1
    assert "first" in rows[0][3]


def test_mark_window_with_no_matches_writes_nothing(cat):
    assert cat.mark_window(0, 1) == 0
    assert _events(cat._db) == []


# ---- the log stays immutable ------------------------------------------------

def test_written_events_cannot_be_altered(cat):
    cap_id = _cap(cat)
    cat.mark_contaminated(cap_id, details={"source": "ptt"})

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        cat._db.execute("UPDATE capture_events SET actor='someone else'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        cat._db.execute("DELETE FROM capture_events")
