"""backfill_orphans: recovery, and being honest that it was a recovery.

The script rebuilds ended_utc from file mtime and n_samples from filesize. The
resulting row is identical in shape to an honestly-finalized one — so without a
finalize_recovered event, the corpus cannot tell an observed finalize from a
reconstructed one. These tests pin that, and the ordering that protects it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import backfill_orphans as bo          # noqa: E402

from cwatlas_mcp.catalog import Catalog             # noqa: E402


@pytest.fixture
def orphan(tmp_path, monkeypatch):
    """A catalog with one row stranded in flight, its IQ file on disk."""
    db_path = tmp_path / "catalog.db"
    cat = Catalog(db_path)
    base = tmp_path / "20m_14030.00kHz_orphan_ch1"
    cap_id = cat.start_capture(freq_hz=14_030_000.0, band="20m", srate_hz=1500,
                               path=str(base), strength_db=12.0, keyed_conf=0.8)
    cat.close()

    data = Path(f"{base}.sigmf-data")
    data.write_bytes(b"\x00\x01\x02\x03" * 1500)     # 1500 complex samples
    old = time.time() - 3600                          # quiescent + stale
    os.utime(data, (old, old))
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE captures SET started_utc=? WHERE id=?",
                   (old - 60, cap_id))

    monkeypatch.setattr(sys, "argv", ["backfill_orphans", "--apply",
                                      "--db", str(db_path)])
    return db_path, cap_id, data


def test_recovery_is_recorded_as_a_recovery(orphan, capsys):
    db_path, cap_id, _ = orphan
    assert bo.main() == 0

    db = sqlite3.connect(db_path)
    ended, n_samples = db.execute(
        "SELECT ended_utc, n_samples FROM captures WHERE id=?", (cap_id,)
    ).fetchone()
    assert ended is not None and n_samples == 1500     # row closed

    rows = db.execute(
        "SELECT event_type, actor, details_json FROM capture_events"
        " WHERE capture_id=?", (cap_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "finalize_recovered"
    assert rows[0][1] == "script:backfill_orphans"
    d = json.loads(rows[0][2])
    # the whole point: these values were inferred, and the row now says so
    assert d["inferred"] is True
    assert d["ended_utc_source"] == "file_mtime"
    assert d["n_samples_source"] == "filesize"


def test_dry_run_writes_neither_row_nor_event(orphan, monkeypatch):
    db_path, cap_id, _ = orphan
    monkeypatch.setattr(sys, "argv", ["backfill_orphans", "--db", str(db_path)])
    assert bo.main() == 0

    db = sqlite3.connect(db_path)
    assert db.execute("SELECT ended_utc FROM captures WHERE id=?",
                      (cap_id,)).fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] == 0


def test_rows_are_closed_even_if_the_event_log_is_broken(orphan, capsys):
    """Ordering rule again: the recovery is the point, the note about it isn't.
    A row left in flight is forever; a missing event is an annoyance."""
    db_path, cap_id, _ = orphan
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE capture_events")

    assert bo.main() == 0                              # must not raise

    db = sqlite3.connect(db_path)
    assert db.execute("SELECT ended_utc FROM captures WHERE id=?",
                      (cap_id,)).fetchone()[0] is not None
    out = capsys.readouterr().out
    assert "rows closed, but finalize_recovered events NOT recorded" in out
    assert str(cap_id) in out                          # says which rows lie


def test_a_live_capture_is_never_touched(tmp_path, monkeypatch):
    """The safety property that already existed: a file still growing may be a
    live worker, and must not be closed or logged."""
    db_path = tmp_path / "catalog.db"
    cat = Catalog(db_path)
    base = tmp_path / "live_ch2"
    cap_id = cat.start_capture(freq_hz=14e6, band="20m", srate_hz=1500,
                               path=str(base), strength_db=12.0, keyed_conf=0.8)
    cat.close()
    Path(f"{base}.sigmf-data").write_bytes(b"\x00" * 400)   # mtime = now
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE captures SET started_utc=? WHERE id=?",
                   (time.time() - 3600, cap_id))

    monkeypatch.setattr(sys, "argv", ["backfill_orphans", "--apply",
                                      "--db", str(db_path)])
    assert bo.main() == 0

    db = sqlite3.connect(db_path)
    assert db.execute("SELECT ended_utc FROM captures WHERE id=?",
                      (cap_id,)).fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] == 0
