"""Site config: file < env < CLI, and no station details baked into source."""
from __future__ import annotations

import pytest

from cwatlas_mcp import config

SAMPLE = """
[sdr]
host = "192.0.2.10"
port = 8073

[station]
lat = 35.0
lon = -97.0
"""


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(SAMPLE)
    monkeypatch.delenv("CWATLAS_CONFIG", raising=False)
    monkeypatch.setattr(config, "_REPO_ROOT", tmp_path)   # no stray real config
    return p


# ---- precedence -------------------------------------------------------------

def test_file_supplies_values(cfg_file, monkeypatch):
    monkeypatch.delenv("CWATLAS_SDR_HOST", raising=False)
    cfg = config.load(cfg_file)
    assert config.pick(cfg, "sdr.host", "CWATLAS_SDR_HOST") == "192.0.2.10"
    assert config.pick(cfg, "station.lat", "CWATLAS_LAT", cast=float) == 35.0


def test_env_beats_file(cfg_file, monkeypatch):
    """The systemd units are env-driven; env has to win over a stale file."""
    monkeypatch.setenv("CWATLAS_SDR_HOST", "198.51.100.7")
    cfg = config.load(cfg_file)
    assert config.pick(cfg, "sdr.host", "CWATLAS_SDR_HOST") == "198.51.100.7"


def test_default_is_the_last_resort(cfg_file):
    cfg = config.load(cfg_file)
    assert config.pick(cfg, "capture.rotate_s", default=600.0) == 600.0


def test_missing_key_returns_default_not_a_crash(cfg_file):
    cfg = config.load(cfg_file)
    assert config.pick(cfg, "tx.flex_host", "CWATLAS_FLEX_HOST", "") == ""
    assert config.pick(cfg, "nope.nothing.here.at.all") is None


def test_cast_applies_to_both_env_and_file(cfg_file, monkeypatch):
    cfg = config.load(cfg_file)
    assert config.pick(cfg, "sdr.port", default=8073, cast=int) == 8073
    monkeypatch.setenv("CWATLAS_LAT", "12.5")
    assert config.pick(cfg, "station.lat", "CWATLAS_LAT", cast=float) == 12.5


# ---- finding the file -------------------------------------------------------

def test_no_config_anywhere_is_fine(tmp_path, monkeypatch):
    """Env-only setups are normal; a missing file is not an error."""
    monkeypatch.delenv("CWATLAS_CONFIG", raising=False)
    monkeypatch.setattr(config, "_REPO_ROOT", tmp_path)
    assert config.load() == {}


def test_a_named_config_that_does_not_exist_is_an_error(tmp_path, monkeypatch):
    """Silence here means the operator thinks their settings are loaded when
    they are not — which for lat/lon is wrong band weighting all night."""
    monkeypatch.delenv("CWATLAS_CONFIG", raising=False)
    with pytest.raises(FileNotFoundError, match="nope.toml"):
        config.load(tmp_path / "nope.toml")

    monkeypatch.setenv("CWATLAS_CONFIG", str(tmp_path / "also-nope.toml"))
    with pytest.raises(FileNotFoundError, match="also-nope.toml"):
        config.load()


def test_env_config_path_is_honoured(cfg_file, monkeypatch):
    monkeypatch.setenv("CWATLAS_CONFIG", str(cfg_file))
    assert config.load()["sdr"]["host"] == "192.0.2.10"


def test_malformed_config_raises_loudly(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("[sdr\nhost = broken")
    monkeypatch.delenv("CWATLAS_CONFIG", raising=False)
    with pytest.raises(Exception):          # tomllib.TOMLDecodeError
        config.load(bad)


def test_loaded_config_records_its_own_path(cfg_file):
    """Provenance records which file was in force — 'where did that come
    from?' should have an answer."""
    assert config.load(cfg_file)["_path"] == str(cfg_file)


# ---- the point of all this --------------------------------------------------

def test_the_example_config_ships_no_real_station(tmp_path):
    """config.example.toml is published. It must not carry anyone's actual
    LAN address or antenna location."""
    example = config._REPO_ROOT / "config.example.toml"
    text = example.read_text()
    assert "192.168." not in text and "10.0.0." not in text
    assert "33.4" not in text and "82.2" not in text
    # RFC 5737 documentation range, and a null island placeholder
    assert "192.0.2." in text
