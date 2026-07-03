from tests.conftest import NOW

from cwatlas_dash import sources


def test_hourly_buckets_shape_and_counts(fixture_db):
    buckets = sources.hourly_buckets(db_path=fixture_db, now=NOW)
    assert len(buckets) == 24
    assert [b["ago_h"] for b in buckets] == list(range(23, -1, -1))
    newest = buckets[-1]                     # ago_h == 0: last hour
    assert newest["captures"] == 3           # 600s x2 + in-flight 120s
    assert newest["contaminated"] == 1
    six_h = next(b for b in buckets if b["ago_h"] == 6)
    assert six_h["captures"] == 1
    assert sum(b["captures"] for b in buckets) == 5   # 24h count incl. in-flight


def test_inflight(fixture_db):
    rows = sources.inflight(db_path=fixture_db, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["band"] == "20m" and r["stale"] is False
    assert abs(r["dwell_s"] - 120) < 1.0


def test_inflight_stale_flag(fixture_db):
    rows = sources.inflight(db_path=fixture_db, now=NOW + 2000)
    assert rows[0]["stale"] is True          # dwell 2120s > 1200s


def test_recent_captures(fixture_db):
    rows = sources.recent_captures(limit=3, db_path=fixture_db)
    assert len(rows) == 3
    assert rows[0]["started_utc"] >= rows[1]["started_utc"]   # newest first
    assert all(r["duration_s"] is not None for r in rows)     # finalized only
