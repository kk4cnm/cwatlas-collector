"""flag_truncated_collisions: say the row is wrong without editing the row.

The 2026-07-18 collision left 172 rows pointing at a partner's IQ. The decision
was to FLAG, not delete — an instrument's record keeps what really happened, and
a catalog that quietly loses rows is tidier and less true. These tests pin the
two properties that decision depends on: `captures` is never mutated, and the
victim is chosen from file evidence rather than from timestamp order.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import flag_truncated_collisions as ftc   # noqa: E402

from cwatlas_mcp.catalog import Catalog                # noqa: E402

FULL, TRUNCATED = 900_032, 192


@pytest.fixture
def collided(tmp_path):
    """Two rows on one path; the file holds only the second row's samples."""
    db_path = tmp_path / "catalog.db"
    cat = Catalog(db_path)
    base = tmp_path / "40m_7047.49kHz_20260718T030122Z_ch5"
    kw = dict(freq_hz=7_047_487.79, band="40m", srate_hz=1500,
              path=str(base), strength_db=40.0, keyed_conf=1.0)
    victim = cat.start_capture(**kw)      # earlier: truncated
    owner = cat.start_capture(**kw)       # later: wrote the file that survives
    for cap_id, n in ((victim, TRUNCATED), (owner, FULL)):
        cat.finalize_capture(cap_id, n_samples=n, contaminated=False,
                             smeter_avg=None)
    cat.close()
    Path(f"{base}.sigmf-data").write_bytes(b"\x00\x01\x02\x03" * FULL)
    return db_path, victim, owner


def _flag(db_path, apply=True):
    argv = ["flag_truncated_collisions", "--db", str(db_path)]
    if apply:
        argv.append("--apply")
    sys.argv = argv
    return ftc.main()


def _events(db_path, cap_id=None):
    db = sqlite3.connect(db_path)
    q = ("SELECT capture_id, event_type FROM capture_events"
         " WHERE event_type='truncated_by_collision'")
    if cap_id is not None:
        q += f" AND capture_id={cap_id}"
    return db.execute(q).fetchall()


def test_flags_only_the_row_whose_file_was_overwritten(collided):
    db_path, victim, owner = collided
    _flag(db_path)
    flagged = {r[0] for r in _events(db_path)}
    assert flagged == {victim}, "flagged the file's rightful owner"


def test_captures_table_is_never_mutated(collided):
    """Flag, don't edit: the row keeps saying exactly what it always said."""
    db_path, victim, _owner = collided
    before = sqlite3.connect(db_path).execute(
        "SELECT id, path, n_samples, ended_utc FROM captures ORDER BY id"
    ).fetchall()
    _flag(db_path)
    after = sqlite3.connect(db_path).execute(
        "SELECT id, path, n_samples, ended_utc FROM captures ORDER BY id"
    ).fetchall()
    assert before == after


def test_rerun_does_not_duplicate_events(collided):
    db_path, victim, _owner = collided
    _flag(db_path)
    _flag(db_path)
    assert len(_events(db_path, victim)) == 1


def test_victim_chosen_by_file_evidence_not_row_order(collided):
    """If the EARLIER row owns the file, the LATER one is the victim.

    The real corpus never showed this shape, but picking by timestamp instead
    of by evidence would flag the wrong row if it ever did.
    """
    db_path, victim, owner = collided
    with sqlite3.connect(db_path) as db:      # swap which row matches the file
        db.execute("UPDATE captures SET n_samples=? WHERE id=?", (FULL, victim))
        db.execute("UPDATE captures SET n_samples=? WHERE id=?", (TRUNCATED, owner))
    _flag(db_path)
    flagged = {r[0] for r in _events(db_path)}
    assert flagged == {owner}, "chose the victim by row order, not by the file"


def test_ambiguous_group_is_skipped_not_guessed(collided, capsys):
    """Neither row matching the file means we don't know — say so, touch nothing."""
    db_path, victim, owner = collided
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE captures SET n_samples=12345 WHERE id=?", (victim,))
        db.execute("UPDATE captures SET n_samples=54321 WHERE id=?", (owner,))
    _flag(db_path)
    assert _events(db_path) == []
    assert "SKIP" in capsys.readouterr().out
