"""The capture finally-block contract: the catalog row always gets closed.

Regression for the 2026-07-15 orphans — a disk mounted over /mnt shadowed the
data dir mid-capture, the sidecar write raised out of the finally, and
finalize_capture never ran. 7 rows read as "capturing" for 18 h.
"""
import asyncio
import sqlite3

import pytest

from cwatlas_mcp.capture import _write_capture
from cwatlas_mcp.catalog import Catalog
from cwatlas_mcp.models import ChannelState, Detection


class _FakeSession:
    """Yields one chunk, then stalls so _write_capture finalizes and returns."""

    def __init__(self):
        self.sent = 0

    async def next_chunk(self):
        self.sent += 1
        if self.sent > 1:
            await asyncio.sleep(3600)      # -> wait_for timeout -> reason="stall"
        return _FakeChunk()


class _FakeChunk:
    data = b"\x00\x01" * 2048              # 12k in; decimator handles the rest
    smeter = -80.0
    gps_solution = False
    gpssec = 0
    gpsnsec = 0


@pytest.fixture
def rig(tmp_path):
    catalog = Catalog(tmp_path / "catalog.db")
    det = Detection(freq_hz=14_030_000.0, band="20m", strength_db=12.0,
                    keyed_confidence=0.8)
    cs = ChannelState(ch=3)
    return catalog, det, cs, tmp_path


def _run(catalog, det, cs, data_dir, stall_s=0.01):
    return asyncio.run(_write_capture(
        _FakeSession(), cs, det, catalog, data_dir,
        asyncio.Queue(), stall_s=stall_s, rotate_s=3600.0))


def _inflight(db_path):
    db = sqlite3.connect(db_path)
    return db.execute(
        "SELECT COUNT(*) FROM captures WHERE ended_utc IS NULL").fetchone()[0]


def test_row_finalized_on_normal_path(rig):
    catalog, det, cs, data_dir = rig
    _, reason = _run(catalog, det, cs, data_dir)
    assert reason == "stall"
    assert _inflight(data_dir / "catalog.db") == 0
    assert cs.capture_id is None


def test_row_finalized_when_sidecar_write_fails(rig, monkeypatch):
    """The 07-15 bug: sidecar open() raising must not skip the catalog."""
    catalog, det, cs, data_dir = rig
    real_open = open

    def exploding_open(path, mode="r", *a, **kw):
        if str(path).endswith(".sigmf-meta"):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", exploding_open)
    _, reason = _run(catalog, det, cs, data_dir)

    assert reason == "stall"                       # error did not escape
    assert _inflight(data_dir / "catalog.db") == 0  # row closed anyway
    assert cs.capture_id is None
    assert not (data_dir / "catalog.db").with_suffix(".sigmf-meta").exists()


def test_samples_recorded_despite_sidecar_failure(rig, monkeypatch):
    """A closed row still carries the sample count, so meta is regenerable."""
    catalog, det, cs, data_dir = rig
    real_open = open

    def exploding_open(path, mode="r", *a, **kw):
        if str(path).endswith(".sigmf-meta"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", exploding_open)
    _run(catalog, det, cs, data_dir)

    db = sqlite3.connect(data_dir / "catalog.db")
    n_samples, ended = db.execute(
        "SELECT n_samples, ended_utc FROM captures").fetchone()
    assert ended is not None
    assert n_samples > 0
