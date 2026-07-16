"""What gets recorded about a collector process, and what deliberately doesn't."""
from __future__ import annotations

import argparse
import json

from cwatlas_mcp import provenance
from cwatlas_mcp.catalog import Catalog
from cwatlas_mcp.scheduler import SchedulerConfig

SEARCH_PLAN = [("20m", 14_030_000.0), ("40m", 7_030_000.0)]

DEV = {"rx_chans": 12, "version_maj": "2026", "version_min": "609"}


def _args(**over):
    base = dict(host="192.168.2.46", port=8073, rotate_s=600.0, lat=35.0, lon=-97.0,
                trial=0.0, no_mcp=True, flex_host="")
    base.update(over)
    return argparse.Namespace(**base)


def _cfg():
    return SchedulerConfig(n_rx_channels=11)


# ---- config snapshot --------------------------------------------------------

def test_config_sha256_is_stable_for_identical_config():
    a = provenance.effective_config(_args(), _cfg(), SEARCH_PLAN)
    b = provenance.effective_config(_args(), _cfg(), SEARCH_PLAN)
    assert provenance.config_sha256(a) == provenance.config_sha256(b)


def test_config_sha256_changes_when_band_weights_change(monkeypatch):
    """The point of the hash: retuning BAND_WEIGHTS silently changes what gets
    collected, so captures from before and after must be tellable apart."""
    before = provenance.effective_config(_args(), _cfg(), SEARCH_PLAN)
    tweaked = dict(provenance.BAND_WEIGHTS, **{"20m": (9.9, 9.9, 9.9)})
    monkeypatch.setattr(provenance, "BAND_WEIGHTS", tweaked)
    after = provenance.effective_config(_args(), _cfg(), SEARCH_PLAN)

    assert provenance.config_sha256(before) != provenance.config_sha256(after)
    # and the old weights stay readable, not just hashed
    assert before["solar"]["band_weights"]["20m"] == [1.2, 1.5, 1.0]


def test_config_captures_hardware_derived_and_cli_values():
    """The part git_commit cannot tell you: these vary independently of source."""
    cfg = provenance.effective_config(_args(rotate_s=300.0), _cfg(), SEARCH_PLAN)
    assert cfg["scheduler"]["n_rx_channels"] == 11      # from the device, not source
    assert cfg["args"]["rotate_s"] == 300.0
    assert cfg["detector"]["min_snr_db"] == 10.0        # read from the signature
    assert cfg["dsp"]["fs_out_hz"] == 1500              # the on-disk sample format
    assert cfg["search_plan"] == [["20m", 14_030_000.0], ["40m", 7_030_000.0]]


def test_flex_host_is_reduced_to_a_bool():
    """A LAN IP is not provenance."""
    cfg = provenance.effective_config(_args(flex_host="192.168.2.99"), _cfg(),
                                      SEARCH_PLAN)
    assert cfg["args"]["flex_ptt"] is True
    assert "192.168.2.99" not in json.dumps(cfg)


def test_unset_lat_lon_is_null_not_nan():
    """NaN isn't JSON; it would serialize to a bare NaN token that strict
    parsers reject. Unset means null."""
    cfg = provenance.effective_config(_args(lat=float("nan"), lon=float("nan")),
                                      _cfg(), SEARCH_PLAN)
    assert cfg["args"]["lat"] is None
    json.loads(json.dumps(cfg))     # must round-trip


# ---- git state --------------------------------------------------------------

def test_git_helper_never_raises_outside_a_checkout(tmp_path, monkeypatch):
    """Not a checkout / no git / held index.lock all mean "unknown", and NULL
    says so. This runs at startup — it must not be able to kill the collector."""
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    assert provenance._git("rev-parse", "HEAD") is None
    assert provenance.git_state() == {"git_commit": None, "git_dirty": None,
                                      "git_diff_sha256": None}


def test_git_state_of_a_real_checkout():
    st = provenance.git_state()
    assert st["git_commit"] is not None and len(st["git_commit"]) == 40
    assert st["git_dirty"] in (0, 1)
    # the fingerprint exists exactly when there's a diff to fingerprint
    assert (st["git_diff_sha256"] is not None) == bool(st["git_dirty"])


# ---- run rows ---------------------------------------------------------------

def test_begin_run_stamps_subsequent_captures(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    try:
        run_id = cat.begin_run(
            provenance.build_run_info(_args(), DEV, _cfg(), SEARCH_PLAN))
        cap_id = cat.start_capture(freq_hz=14_030_000.0, band="20m", srate_hz=1500,
                                   path="x", strength_db=12.0, keyed_conf=0.8)
        assert cat._db.execute("SELECT run_id FROM captures WHERE id=?",
                               (cap_id,)).fetchone()[0] == run_id

        row = cat._db.execute(
            "SELECT sdr_firmware, sdr_rx_chans, config_json, config_sha256,"
            " ended_utc FROM runs WHERE id=?", (run_id,)).fetchone()
        assert row[0] == "2026.609"        # fetched at startup, no longer discarded
        assert row[1] == 12
        assert json.loads(row[2])["scheduler"]["n_rx_channels"] == 11
        assert len(row[3]) == 64
        assert row[4] is None              # still running
    finally:
        cat.close()


def test_capture_without_a_run_records_null(tmp_path):
    """A Catalog opened by a script or a test declares no run; NULL is the
    honest answer, not a bug."""
    cat = Catalog(tmp_path / "catalog.db")
    try:
        cap_id = cat.start_capture(freq_hz=14_030_000.0, band="20m", srate_hz=1500,
                                   path="x", strength_db=12.0, keyed_conf=0.8)
        assert cat._db.execute("SELECT run_id FROM captures WHERE id=?",
                               (cap_id,)).fetchone()[0] is None
    finally:
        cat.close()


def test_end_run_closes_the_run(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    try:
        run_id = cat.begin_run(
            provenance.build_run_info(_args(), DEV, _cfg(), SEARCH_PLAN))
        cat.end_run()
        ended = cat._db.execute("SELECT ended_utc FROM runs WHERE id=?",
                                (run_id,)).fetchone()[0]
        assert ended is not None
    finally:
        cat.close()


def test_end_run_without_begin_run_is_a_noop(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    try:
        cat.end_run()          # must not raise into runtime's finally block
        assert cat._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        cat.close()


def test_missing_firmware_is_null_not_a_guess(tmp_path):
    """An unrecorded fact is NULL. A plausible default that reads like an
    observation is worse than a gap."""
    info = provenance.build_run_info(_args(), {}, _cfg(), SEARCH_PLAN)
    assert info["sdr_firmware"] is None
    assert info["sdr_rx_chans"] is None
