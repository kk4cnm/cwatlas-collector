import pytest

import cwatlas_dash.sources as sources
from cwatlas_dash.app import create_app

SUMMARY_KEYS = {"generated_at", "service", "sdr", "adc", "totals",
                "windows", "hourly", "inflight", "solar", "journal"}


@pytest.fixture
def client(fixture_db, monkeypatch):
    monkeypatch.setattr(sources, "sdr_snapshot",
                        lambda *a, **k: {"status": {"gps": "good"},
                                         "adc": {"ov_mask": "0"}})
    monkeypatch.setattr(sources, "system_health",
                        lambda *a, **k: {"active_state": "active"})
    monkeypatch.setattr(sources, "journal_tail",
                        lambda *a, **k: {"lines": [], "errors": 0})
    app = create_app(DATA_DIR=fixture_db.parent)
    return app.test_client()


def test_summary_has_all_panels(client):
    r = client.get("/api/summary")
    assert r.status_code == 200
    data = r.get_json()
    assert set(data) == SUMMARY_KEYS
    assert set(data["windows"]) == {"1h", "12h", "24h", "7d"}
    assert len(data["hourly"]) == 24
    assert data["sdr"] == {"gps": "good"}
    assert data["adc"] == {"ov_mask": "0"}


def test_summary_degrades_per_source(client, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("sdr down")
    monkeypatch.setattr(sources, "sdr_snapshot", boom)
    r = client.get("/api/summary")
    assert r.status_code == 200
    data = r.get_json()
    assert "error" in data["sdr"] and "sdr down" in data["sdr"]["error"]
    assert data["totals"]["captures"] == 7      # catalog panel unaffected


def test_captures_endpoint_clamps_limit(client):
    r = client.get("/api/captures?limit=99999")
    assert r.status_code == 200
    assert isinstance(r.get_json()["captures"], list)
    r = client.get("/api/captures?limit=-3")
    assert r.status_code == 200


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"CWAtlas" in r.data
