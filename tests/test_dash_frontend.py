import pytest

from cwatlas_dash.app import create_app

PANEL_IDS = ["panel-status", "panel-totals", "panel-windows", "panel-chart",
             "panel-inflight", "panel-recent", "panel-solar", "panel-journal"]


@pytest.fixture
def client(tmp_path):
    return create_app(DATA_DIR=tmp_path).test_client()


def test_index_has_all_panels_and_no_cdn(client):
    html = client.get("/").data.decode()
    for pid in PANEL_IDS:
        assert f'id="{pid}"' in html, f"missing {pid}"
    assert "https://" not in html          # no CDN/external assets
    assert "dash.js" in html and "dash.css" in html


def test_static_assets_served(client):
    assert client.get("/static/dash.js").status_code == 200
    assert client.get("/static/dash.css").status_code == 200
