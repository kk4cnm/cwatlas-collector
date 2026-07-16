from tests.conftest import NOW

from cwatlas_dash import sources


def test_collection_stats_1h(fixture_db):
    s = sources.collection_stats("1h", db_path=fixture_db, now=NOW)
    assert s["window"] == "1h"
    assert s["captures"] == 3          # two finalized + one in-flight
    assert s["contaminated"] == 1
    assert set(s["by_band"]) == {"20m", "40m"}
    assert s["by_band"]["20m"]["captures"] == 2  # finalized + in-flight
    # one finalized 60 s 20m capture; the in-flight row has n_samples=0
    assert s["by_band"]["40m"]["iq_hours"] == round(60 / 3600, 2)
    # top-level iq_hours rounds to 1 decimal (Catalog.window_stats parity)
    assert s["iq_hours"] == round(120 / 3600, 1)
    assert s["bytes"] == 2 * 60 * 12_000 * 4     # ci16: 4 bytes/sample


def test_windows_nest(fixture_db):
    counts = {w: sources.collection_stats(w, db_path=fixture_db, now=NOW)["captures"]
              for w in sources.WINDOWS}
    assert counts == {"1h": 3, "12h": 4, "24h": 5, "7d": 6}


def test_collection_stats_rejects_unknown_window(fixture_db):
    import pytest
    with pytest.raises(KeyError):
        sources.collection_stats("3w", db_path=fixture_db)


def test_totals(fixture_db):
    t = sources.totals(db_path=fixture_db)
    assert t["captures"] == 7
    assert t["in_flight"] == 1
    assert t["contaminated"] == 1
    assert t["bytes"] == 6 * 60 * 12_000 * 4
    assert abs(t["iq_hours"] - 6 * 60 / 3600) < 1e-6


def test_db_is_opened_read_only(fixture_db):
    import contextlib
    import sqlite3

    import pytest
    with contextlib.closing(sources._connect(fixture_db)) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO captures (freq_hz, band, started_utc,"
                       " srate_hz, path) VALUES (1,'x',1,1,'x')")
