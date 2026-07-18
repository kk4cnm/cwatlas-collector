"""Two captures on one channel+frequency in the same second must not collide.

Regression for the 2026-07-18 truncation: the max-dwell hog guard released a
channel and the SAME tick reassigned the same signal to it, because `desired`
was scored before the cooldown was applied. The worker then wrote a second file
whose second-granular name matched the first — and open(..., "wb") silently
truncated it, leaving the first catalog row pointing at another capture's IQ.
171 path groups corpus-wide, 2026-07-02 .. 2026-07-18.

Two independent defects, so two independent guards:
  - scheduler: a signal serving a cooldown must not win a slot (root cause)
  - capture:   a capture must never overwrite another's file (safety net)
"""
import asyncio
import sqlite3
import time

import pytest

from cwatlas_mcp.capture import _write_capture
from cwatlas_mcp.catalog import Catalog
from cwatlas_mcp.models import ChannelMode, ChannelState, Detection
from cwatlas_mcp.scheduler import CollectorState, SchedulerConfig, Supervisor


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
    data = b"\x00\x01" * 2048
    smeter = -80.0
    gps_solution = False
    gpssec = 0
    gpsnsec = 0


# ---- capture plane: never overwrite -------------------------------------

@pytest.fixture
def rig(tmp_path):
    catalog = Catalog(tmp_path / "catalog.db")
    det = Detection(freq_hz=14_030_000.0, band="20m", strength_db=12.0,
                    keyed_confidence=0.8)
    cs = ChannelState(ch=3)
    return catalog, det, cs, tmp_path


def _run(catalog, det, cs, data_dir):
    return asyncio.run(_write_capture(
        _FakeSession(), cs, det, catalog, data_dir,
        asyncio.Queue(), stall_s=0.01, rotate_s=3600.0))


def _rows(db_path):
    db = sqlite3.connect(db_path)
    return db.execute(
        "SELECT id, path, n_samples FROM captures ORDER BY id").fetchall()


def test_same_second_recapture_does_not_truncate(rig):
    """The exact 07-18 shape: same channel, same freq, same second, twice."""
    catalog, det, cs, data_dir = rig
    _run(catalog, det, cs, data_dir)
    _run(catalog, det, cs, data_dir)        # same second, same name

    rows = _rows(data_dir / "catalog.db")
    assert len(rows) == 2
    paths = [r[1] for r in rows]
    assert paths[0] != paths[1], "second capture reused the first's path"

    # every row's file must hold exactly the samples that row claims
    for _id, path, n_samples in rows:
        on_disk = (data_dir / f"{path}.sigmf-data").stat().st_size // 4
        assert on_disk == n_samples, f"row {_id}: {n_samples} claimed, {on_disk} on disk"


def test_distinct_seconds_keep_the_plain_name(rig, monkeypatch):
    """The uniquifier is collision-only: normal captures keep their name."""
    catalog, det, cs, data_dir = rig
    _run(catalog, det, cs, data_dir)
    rows = _rows(data_dir / "catalog.db")
    assert not rows[0][1].endswith("_2"), "uniquified a name that did not collide"


# ---- scheduler: a cooled-down signal must not re-win its own slot --------

def _sched(**over):
    cfg = SchedulerConfig(n_rx_channels=2, deep_dwell_reserve=0,
                          min_dwell_s=0.0, max_dwell_s=1.0,
                          capture_cooldown_s=180.0, min_capture_score=0.0,
                          **over)
    state = CollectorState()
    spawned = []
    sup = Supervisor(cfg, state, _Bus(),
                     spawn_capture=lambda ch, d: spawned.append((ch, d)),
                     stop_capture=lambda ch: None)
    return sup, state, spawned


class _Bus:
    def drain(self):
        return []


def test_max_dwell_release_does_not_instantly_reassign():
    """The hog guard's whole point: the evicted signal serves its cooldown."""
    sched, state, spawned = _sched()
    det = Detection(freq_hz=7_047_487.79, band="40m", strength_db=40.0,
                    keyed_confidence=1.0)
    state.activity[det.freq_hz] = det

    cs = state.channels[0]
    cs.mode, cs.freq_hz = ChannelMode.CAPTURING, det.freq_hz
    cs.since = time.time() - 3600          # well past max_dwell_s

    sched.tick()

    assert not any(d.freq_hz == det.freq_hz for _ch, d in spawned), \
        "released hog instantly re-won a slot, defeating its cooldown"
    assert det.cooldown_until > time.time(), "cooldown never applied"
    assert cs.mode == ChannelMode.IDLE, "slot did not come free"
