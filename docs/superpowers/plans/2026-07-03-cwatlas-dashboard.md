# CWAtlas Dashboard (`cwatlas_dash`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read-only Flask dashboard on `0.0.0.0:8828` showing collector status, live captures, collection stats (totals + 1h/12h/24h/7d), and system health — zero collector code changes.

**Architecture:** New sibling package `cwatlas_dash/` with an MCP-shaped data layer (`sources.py`) reading four independent sources: read-only catalog.db, SDR AJAX endpoints (via `cwatlas_mcp.sdr_client`), systemd/journald, and `cwatlas_mcp.solar`. Flask serves one page + `/api/summary`; vanilla-JS frontend polls every 15 s. Deployed as its own systemd unit.

**Tech Stack:** Python ≥3.11, Flask, sqlite3 (stdlib, read-only URI), httpx (already installed), pytest. No frontend build step, no CDN.

**Spec:** `docs/superpowers/specs/2026-07-03-cwatlas-dashboard-design.md` — read it first.

## Global Constraints

- **Zero collector code changes.** Only additive edits to `pyproject.toml` and new files. Never modify `cwatlas_mcp/*.py`.
- **Never open a WebSocket to the SDR** (no `read_config()`, no `waterfall_scan()`, no `open_snd()`) — WS connections occupy one of the device's 12 rx channel slots and steal capture capacity. AJAX GETs only.
- Catalog DB opened **read-only** (`file:...?mode=ro`, `uri=True`) — never read-write.
- Bind `0.0.0.0:8828` (operator accepted the LAN-only stance; no auth).
- Flask is an optional extra (`dash`); collector install must not gain a hard Flask dependency.
- No CDN/network assets in the frontend — inline SVG charts, local static files only.
- `/api/summary` never 500s because one data source is down — per-source `{"error": ...}` degradation.
- Production paths on airig-01: data dir `/mnt/md0/cwatlas/data`, SDR `192.168.2.46:8073`, lat/lon (now `[station]` in config.toml), collector unit `cwatlas-collector`.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Package scaffolding + dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `cwatlas_dash/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_dash_package.py`

**Interfaces:**
- Produces: importable `cwatlas_dash` package; venv with `flask`, `pytest` installed.

- [ ] **Step 1: Edit `pyproject.toml`** — three additive changes:

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.6"]
dash = ["flask>=3"]
```

```toml
[tool.setuptools]
packages = ["cwatlas_mcp", "cwatlas_dash"]

[tool.setuptools.package-data]
cwatlas_dash = ["templates/*", "static/*"]
```

(`dev` line already exists — add the `dash` line below it; replace the existing `packages` line; add the new `package-data` table.)

- [ ] **Step 2: Create the package and test scaffolding**

`cwatlas_dash/__init__.py`:

```python
"""CWAtlas status dashboard — read-only Flask sidecar over the collector's
observable surfaces (catalog.db, SDR AJAX, systemd, solar). See
docs/superpowers/specs/2026-07-03-cwatlas-dashboard-design.md."""
```

`tests/__init__.py`: empty file.

`tests/test_dash_package.py`:

```python
def test_package_imports():
    import cwatlas_dash  # noqa: F401
```

- [ ] **Step 3: Install and run the test**

Run: `cd /home/dnelms/cwatlas/collector && .venv/bin/pip install -e ".[dev,dash]" -q && .venv/bin/pytest tests/test_dash_package.py -v`
Expected: PASS (1 passed)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml cwatlas_dash/__init__.py tests/__init__.py tests/test_dash_package.py
git commit -m "dash: package scaffolding + flask optional extra"
```

---

### Task 2: Catalog source — window stats and totals

**Files:**
- Create: `cwatlas_dash/sources.py`
- Create: `tests/conftest.py`
- Test: `tests/test_dash_catalog.py`

**Interfaces:**
- Consumes: `cwatlas_mcp.catalog.SCHEMA` (the `CREATE TABLE captures ...` script) — for test fixtures only.
- Produces:
  - `sources.WINDOWS: dict[str, int]` — `{"1h": 3600, "12h": 43200, "24h": 86400, "7d": 604800}`
  - `sources.collection_stats(window: str, db_path: Path | None = None, now: float | None = None) -> dict` — shape identical to the MCP `get_collection_stats` tool: `{"window", "captures", "iq_hours", "bytes", "contaminated", "by_band"}`
  - `sources.totals(db_path: Path | None = None) -> dict` — `{"captures", "iq_hours", "bytes", "contaminated", "in_flight"}`
  - `sources.DATA_DIR: Path`, `sources.DB_PATH: Path` (env-derived defaults)

- [ ] **Step 1: Write the test fixture + failing tests**

`tests/conftest.py`:

```python
import sqlite3
import time

import pytest

from cwatlas_mcp.catalog import SCHEMA

NOW = 1_751_500_000.0  # fixed "now" so window edges are deterministic


def _row(*, started_ago_s, dur_s=60.0, band="20m", freq_hz=14_030_000.0,
         srate=12_000, contaminated=0, in_flight=False):
    """One captures row: started `started_ago_s` before NOW, `dur_s` long."""
    started = NOW - started_ago_s
    ended = None if in_flight else started + dur_s
    n_samples = 0 if in_flight else int(dur_s * srate)
    return (freq_hz, band, started, ended, n_samples, srate,
            f"cap_{band}_{int(started)}", 12.0, 0.8, contaminated)


@pytest.fixture
def fixture_db(tmp_path):
    """catalog.db with rows straddling every window edge."""
    db_path = tmp_path / "catalog.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    rows = [
        _row(started_ago_s=600),                          # inside 1h
        _row(started_ago_s=600, band="40m",
             freq_hz=7_030_000.0, contaminated=1),        # inside 1h, contaminated
        _row(started_ago_s=6 * 3600),                     # inside 12h only
        _row(started_ago_s=20 * 3600, band="40m",
             freq_hz=7_040_000.0),                        # inside 24h only
        _row(started_ago_s=3 * 86400),                    # inside 7d only
        _row(started_ago_s=10 * 86400),                   # outside all windows
        _row(started_ago_s=120, in_flight=True),          # in flight now
    ]
    db.executemany(
        "INSERT INTO captures (freq_hz, band, started_utc, ended_utc,"
        " n_samples, srate_hz, path, strength_db, keyed_conf, contaminated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return db_path
```

`tests/test_dash_catalog.py`:

```python
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
    assert abs(s["by_band"]["40m"]["iq_hours"] - 60 / 3600) < 1e-6
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
    import pytest, sqlite3
    with pytest.raises(sqlite3.OperationalError):
        db = sources._connect(fixture_db)
        db.execute("INSERT INTO captures (freq_hz, band, started_utc,"
                   " srate_hz, path) VALUES (1,'x',1,1,'x')")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dash_catalog.py -v`
Expected: FAIL — `AttributeError: module 'cwatlas_dash.sources' has no attribute ...` (or ImportError)

- [ ] **Step 3: Implement `cwatlas_dash/sources.py` (catalog section)**

```python
"""MCP-shaped data layer for the dashboard.

Where an MCP observe tool exists, the function here returns the identical
shape (collection_stats == get_collection_stats, sdr_* == get_sdr_status /
get_adc_overload). When the collector grows an MCP-over-HTTP transport,
this module becomes an MCP client and nothing above it changes.

Read-only by construction: sqlite opened with mode=ro, SDR reached with
AJAX GETs only (a WebSocket would occupy one of the device's rx channel
slots — never do that from the dashboard).
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DATA_DIR = Path(os.environ.get("CWATLAS_DATA_DIR", "~/cwatlas/data")).expanduser()
DB_PATH = DATA_DIR / "catalog.db"

WINDOWS = {"1h": 3600, "12h": 43200, "24h": 86400, "7d": 604800}

BYTES_PER_SAMPLE = 4  # ci16 IQ


# ============================ catalog (sqlite, ro) ============================
def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def collection_stats(window: str, db_path: Path | None = None,
                     now: float | None = None) -> dict:
    """Same shape as the MCP get_collection_stats tool / Catalog.window_stats."""
    since = (now or time.time()) - WINDOWS[window]
    with closing(_connect(db_path)) as db:
        tot = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0),"
            " COALESCE(SUM(contaminated),0)"
            " FROM captures WHERE started_utc >= ?", (since,)).fetchone()
        by_band = db.execute(
            "SELECT band, COUNT(*), COALESCE(SUM(n_samples * 1.0 / srate_hz), 0)"
            " FROM captures WHERE started_utc >= ? GROUP BY band"
            " ORDER BY 3 DESC", (since,)).fetchall()
    return {
        "window": window,
        "captures": tot[0],
        "iq_hours": round(sum(r[2] for r in by_band) / 3600.0, 2),
        "bytes": tot[1] * BYTES_PER_SAMPLE,
        "contaminated": tot[2],
        "by_band": {r[0]: {"captures": r[1], "iq_hours": round(r[2] / 3600.0, 2)}
                    for r in by_band},
    }


def totals(db_path: Path | None = None) -> dict:
    """All-time counters (Catalog.stats shape, plus iq_hours/bytes)."""
    with closing(_connect(db_path)) as db:
        row = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n_samples),0),"
            " COALESCE(SUM(CASE WHEN ended_utc IS NULL THEN 1 ELSE 0 END),0),"
            " COALESCE(SUM(contaminated),0),"
            " COALESCE(SUM(n_samples * 1.0 / srate_hz),0)"
            " FROM captures").fetchone()
    return {"captures": row[0], "bytes": row[1] * BYTES_PER_SAMPLE,
            "in_flight": row[2], "contaminated": row[3],
            "iq_hours": round(row[4] / 3600.0, 2)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dash_catalog.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/sources.py tests/conftest.py tests/test_dash_catalog.py
git commit -m "dash: read-only catalog source (window stats + totals)"
```

---

### Task 3: Catalog source — hourly buckets, in-flight, recent captures

**Files:**
- Modify: `cwatlas_dash/sources.py` (append to the catalog section)
- Test: `tests/test_dash_catalog_live.py`

**Interfaces:**
- Consumes: `sources._connect`, fixtures from `tests/conftest.py`.
- Produces:
  - `sources.hourly_buckets(db_path=None, hours: int = 24, now: float | None = None) -> list[dict]` — exactly `hours` entries, oldest first: `{"ago_h": int, "captures": int, "contaminated": int, "iq_hours": float}`
  - `sources.inflight(db_path=None, now: float | None = None, stale_after_s: float = 1200.0) -> list[dict]` — `{"id", "freq_hz", "band", "started_utc", "dwell_s", "strength_db", "keyed_conf", "stale"}`
  - `sources.recent_captures(limit: int = 50, db_path=None) -> list[dict]` — finalized rows, newest first: `{"id", "freq_hz", "band", "started_utc", "duration_s", "iq_hours", "strength_db", "keyed_conf", "contaminated", "smeter_avg"}`

- [ ] **Step 1: Write the failing tests**

`tests/test_dash_catalog_live.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dash_catalog_live.py -v`
Expected: FAIL with AttributeError on `hourly_buckets`

- [ ] **Step 3: Append implementations to `cwatlas_dash/sources.py`**

```python
def hourly_buckets(db_path: Path | None = None, hours: int = 24,
                   now: float | None = None) -> list[dict]:
    """Capture-rate buckets for the last `hours` hours, oldest first."""
    t = now or time.time()
    with closing(_connect(db_path)) as db:
        rows = db.execute(
            "SELECT CAST((? - started_utc) / 3600 AS INTEGER) AS ago,"
            " COUNT(*), COALESCE(SUM(contaminated),0),"
            " COALESCE(SUM(n_samples * 1.0 / srate_hz),0)"
            " FROM captures WHERE started_utc >= ? AND started_utc <= ?"
            " GROUP BY ago", (t, t - hours * 3600, t)).fetchall()
    got = {r[0]: r for r in rows}
    return [
        {"ago_h": ago,
         "captures": got[ago][1] if ago in got else 0,
         "contaminated": got[ago][2] if ago in got else 0,
         "iq_hours": round((got[ago][3] if ago in got else 0) / 3600.0, 2)}
        for ago in range(hours - 1, -1, -1)
    ]


def inflight(db_path: Path | None = None, now: float | None = None,
             stale_after_s: float = 1200.0) -> list[dict]:
    """Captures currently being written — the 'live channels' view.

    A row in flight for > stale_after_s (2x the collector's 600 s rotate
    period) is almost certainly an orphan from a crash: flag it."""
    t = now or time.time()
    with closing(_connect(db_path)) as db:
        rows = db.execute(
            "SELECT id, freq_hz, band, started_utc, strength_db, keyed_conf"
            " FROM captures WHERE ended_utc IS NULL"
            " ORDER BY started_utc DESC").fetchall()
    return [
        {"id": r[0], "freq_hz": r[1], "band": r[2], "started_utc": r[3],
         "dwell_s": round(t - r[3], 1), "strength_db": r[4],
         "keyed_conf": r[5], "stale": (t - r[3]) > stale_after_s}
        for r in rows
    ]


def recent_captures(limit: int = 50, db_path: Path | None = None) -> list[dict]:
    """Last `limit` finalized captures, newest first."""
    with closing(_connect(db_path)) as db:
        rows = db.execute(
            "SELECT id, freq_hz, band, started_utc, ended_utc - started_utc,"
            " n_samples * 1.0 / srate_hz, strength_db, keyed_conf,"
            " contaminated, smeter_avg"
            " FROM captures WHERE ended_utc IS NOT NULL"
            " ORDER BY started_utc DESC LIMIT ?", (limit,)).fetchall()
    return [
        {"id": r[0], "freq_hz": r[1], "band": r[2], "started_utc": r[3],
         "duration_s": round(r[4], 1), "iq_hours": round(r[5] / 3600.0, 3),
         "strength_db": r[6], "keyed_conf": r[7],
         "contaminated": bool(r[8]), "smeter_avg": r[9]}
        for r in rows
    ]
```

- [ ] **Step 4: Run all catalog tests**

Run: `.venv/bin/pytest tests/test_dash_catalog.py tests/test_dash_catalog_live.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/sources.py tests/test_dash_catalog_live.py
git commit -m "dash: hourly buckets, in-flight view, recent captures"
```

---

### Task 4: SDR source — cached AJAX snapshot

**Files:**
- Modify: `cwatlas_dash/sources.py` (new SDR section)
- Test: `tests/test_dash_sdr.py`

**Interfaces:**
- Consumes: `cwatlas_mcp.sdr_client.SdrClient` / `SdrConfig` (AJAX methods `get_status()`, `get_adc()` only).
- Produces:
  - `sources.sdr_snapshot(host: str, port: int = 8073, ttl_s: float = 10.0, now=time.time) -> dict` — `{"status": {k: v}, "adc": {k: v}}` (parsed key=value dicts, as the MCP `get_sdr_status`/`get_adc_overload` tools return); cached per host:port for `ttl_s`.
  - `sources._fetch_sdr(host, port) -> dict` (uncached; tests patch this for cache tests).

- [ ] **Step 1: Write the failing tests**

`tests/test_dash_sdr.py`:

```python
import httpx

from cwatlas_dash import sources


def test_fetch_sdr_parses_status_and_adc(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"/status": "status=active\nusers=3\ngps=good\n",
                "/adc": "ov_mask=0\nadc_level=42\n"}[request.url.path]
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "cwatlas_mcp.sdr_client.httpx.AsyncClient",
        lambda **kw: real(transport=transport, **kw))

    snap = sources._fetch_sdr("192.0.2.1", 8073)
    assert snap["status"]["users"] == "3"
    assert snap["adc"]["ov_mask"] == "0"


def test_sdr_snapshot_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_fetch_sdr",
                        lambda h, p: calls.append(1) or {"status": {}, "adc": {}})
    sources._SDR_CACHE.clear()
    clock = [1000.0]
    now = lambda: clock[0]

    sources.sdr_snapshot("h1", ttl_s=10.0, now=now)
    sources.sdr_snapshot("h1", ttl_s=10.0, now=now)      # within ttl: cached
    assert len(calls) == 1
    clock[0] += 11.0
    sources.sdr_snapshot("h1", ttl_s=10.0, now=now)      # expired: refetch
    assert len(calls) == 2


def test_sdr_snapshot_does_not_cache_failures(monkeypatch):
    def boom(h, p):
        raise ConnectionError("sdr down")
    monkeypatch.setattr(sources, "_fetch_sdr", boom)
    sources._SDR_CACHE.clear()
    import pytest
    with pytest.raises(ConnectionError):
        sources.sdr_snapshot("h2")
    assert not sources._SDR_CACHE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dash_sdr.py -v`
Expected: FAIL with AttributeError on `_fetch_sdr`

- [ ] **Step 3: Implement the SDR section in `sources.py`**

Add `import asyncio` at the top, then:

```python
# ========================= SDR (AJAX info plane only) =========================
# Cache so N browser tabs polling every 15 s produce at most one device hit
# per ttl. Failures are NOT cached: a down SDR is re-probed each poll (short
# timeout below bounds the stall).
_SDR_CACHE: dict[str, tuple[float, dict]] = {}


def _fetch_sdr(host: str, port: int) -> dict:
    from cwatlas_mcp.sdr_client import SdrClient, SdrConfig

    async def go() -> dict:
        sdr = SdrClient(SdrConfig(host=host, port=port))
        try:
            return {"status": await sdr.get_status(),
                    "adc": await sdr.get_adc()}
        finally:
            await sdr.aclose()

    return asyncio.run(asyncio.wait_for(go(), timeout=4.0))


def sdr_snapshot(host: str, port: int = 8073, ttl_s: float = 10.0,
                 now=time.time) -> dict:
    """get_sdr_status + get_adc_overload equivalent, cached per host:port."""
    key = f"{host}:{port}"
    t = now()
    hit = _SDR_CACHE.get(key)
    if hit and t - hit[0] < ttl_s:
        return hit[1]
    snap = _fetch_sdr(host, port)
    _SDR_CACHE[key] = (t, snap)
    return snap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dash_sdr.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/sources.py tests/test_dash_sdr.py
git commit -m "dash: cached SDR AJAX snapshot (status + adc, GETs only)"
```

---

### Task 5: System source — service health, journal tail, disk

**Files:**
- Modify: `cwatlas_dash/sources.py` (new system section)
- Test: `tests/test_dash_system.py`

**Interfaces:**
- Consumes: `systemctl`, `journalctl` (injectable `run=` for tests), `shutil.disk_usage`.
- Produces:
  - `sources.system_health(unit: str = "cwatlas-collector", data_dir: Path | None = None, run=subprocess.run) -> dict` — `{"unit", "active_state", "sub_state", "n_restarts", "memory_bytes", "started_at", "uptime_s", "disk": {"path", "total", "used", "free"}}` (`uptime_s`/`memory_bytes` may be `None`)
  - `sources.journal_tail(unit: str = "cwatlas-collector", n: int = 100, run=subprocess.run) -> dict` — `{"lines": [str], "errors": int}`; raises `RuntimeError` if the journal is unreadable (permission), so the app layer renders the panel's error state.

- [ ] **Step 1: Write the failing tests**

`tests/test_dash_system.py`:

```python
from pathlib import Path
from types import SimpleNamespace

import pytest

from cwatlas_dash import sources

SHOW_OK = """ActiveState=active
SubState=running
NRestarts=2
MemoryCurrent=104857600
ExecMainStartTimestamp=Tue 2026-07-01 12:00:00 EDT
ExecMainStartTimestampMonotonic=5000000000
"""


def fake_run_factory(stdout, returncode=0):
    def fake_run(cmd, **kw):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return fake_run


def test_system_health_parses_systemctl(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_monotonic_now_s", lambda: 6000.0)
    h = sources.system_health(data_dir=tmp_path,
                              run=fake_run_factory(SHOW_OK))
    assert h["active_state"] == "active"
    assert h["n_restarts"] == 2
    assert h["memory_bytes"] == 104857600
    assert h["uptime_s"] == 1000.0          # 6000 - 5000000000us/1e6
    assert h["disk"]["total"] > 0


def test_system_health_unit_gone(tmp_path):
    h = sources.system_health(data_dir=tmp_path,
                              run=fake_run_factory("ActiveState=inactive\n"))
    assert h["active_state"] == "inactive"
    assert h["uptime_s"] is None


def test_journal_tail_counts_errors():
    out = ("2026-07-03T10:00:00 airig-01 python[1]: [runtime] ok\n"
           "2026-07-03T10:00:01 airig-01 python[1]: Traceback (most recent...\n"
           "2026-07-03T10:00:02 airig-01 python[1]: ValueError: boom\n")
    j = sources.journal_tail(run=fake_run_factory(out))
    assert len(j["lines"]) == 3
    assert j["errors"] == 2


def test_journal_tail_permission_denied():
    with pytest.raises(RuntimeError, match="journal"):
        sources.journal_tail(run=fake_run_factory("", returncode=1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dash_system.py -v`
Expected: FAIL with AttributeError

- [ ] **Step 3: Implement the system section in `sources.py`**

Add `import re`, `import shutil`, `import subprocess` at the top, then:

```python
# ====================== system (systemd / journal / disk) =====================
_ERROR_PAT = re.compile(r"traceback|error|exception|fail", re.IGNORECASE)


def _monotonic_now_s() -> float:
    """Seconds since boot (CLOCK_MONOTONIC matches systemd's *Monotonic props)."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def system_health(unit: str = "cwatlas-collector",
                  data_dir: Path | None = None,
                  run=subprocess.run) -> dict:
    out = run(["systemctl", "show", unit, "-p",
               "ActiveState,SubState,NRestarts,MemoryCurrent,"
               "ExecMainStartTimestamp,ExecMainStartTimestampMonotonic"],
              capture_output=True, text=True, timeout=5).stdout
    kv = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)

    uptime_s = None
    mono_us = kv.get("ExecMainStartTimestampMonotonic", "0")
    if kv.get("ActiveState") == "active" and mono_us.isdigit() and int(mono_us):
        uptime_s = round(_monotonic_now_s() - int(mono_us) / 1e6, 0)

    mem = kv.get("MemoryCurrent", "")
    du = shutil.disk_usage(data_dir or DATA_DIR)
    return {
        "unit": unit,
        "active_state": kv.get("ActiveState", "unknown"),
        "sub_state": kv.get("SubState", "unknown"),
        "n_restarts": int(kv.get("NRestarts", "0") or 0),
        "memory_bytes": int(mem) if mem.isdigit() else None,
        "started_at": kv.get("ExecMainStartTimestamp") or None,
        "uptime_s": uptime_s,
        "disk": {"path": str(data_dir or DATA_DIR),
                 "total": du.total, "used": du.used, "free": du.free},
    }


def journal_tail(unit: str = "cwatlas-collector", n: int = 100,
                 run=subprocess.run) -> dict:
    proc = run(["journalctl", "-u", unit, "-n", str(n),
                "--no-pager", "-o", "short-iso"],
               capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(
            f"journal unreadable (is {os.environ.get('USER', 'the user')} in"
            f" the systemd-journal group?): {proc.stderr.strip()}")
    lines = proc.stdout.splitlines()
    return {"lines": lines,
            "errors": sum(1 for ln in lines if _ERROR_PAT.search(ln))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dash_system.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/sources.py tests/test_dash_system.py
git commit -m "dash: system health, journal tail, disk usage"
```

---

### Task 6: Solar source

**Files:**
- Modify: `cwatlas_dash/sources.py` (solar section)
- Test: `tests/test_dash_solar.py`

**Interfaces:**
- Consumes: `cwatlas_mcp.solar.band_weights(lat, lon) -> (phase: str, weights: dict[str, float])`
- Produces: `sources.solar_priorities(lat: float, lon: float) -> dict` — `{"phase": str, "weights": {band: float}, "nudges": None, "note": str}`

- [ ] **Step 1: Write the failing test**

`tests/test_dash_solar.py`:

```python
from cwatlas_dash import sources


def test_solar_priorities_shape():
    p = sources.solar_priorities(35.0, -97.0)
    assert isinstance(p["phase"], str) and p["phase"]
    assert p["weights"] and all(isinstance(w, float) for w in p["weights"].values())
    assert "20m" in p["weights"]
    assert p["nudges"] is None          # live nudges need MCP; explicit in UI
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_dash_solar.py -v`
Expected: FAIL with AttributeError

- [ ] **Step 3: Implement**

```python
# ================================ solar =======================================
def solar_priorities(lat: float, lon: float) -> dict:
    """Recomputed solar baseline (same math the collector's solar_worker runs).

    Live agent nudges are supervisor in-process state — unreachable without
    MCP — so nudges is always None here; the UI says so rather than showing 1.0."""
    from cwatlas_mcp.solar import band_weights

    phase, weights = band_weights(lat, lon)
    return {"phase": phase, "weights": weights, "nudges": None,
            "note": "solar baseline only; live nudges require MCP"}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_dash_solar.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/sources.py tests/test_dash_solar.py
git commit -m "dash: solar band-priority baseline"
```

---

### Task 7: Flask app — `/api/summary`, `/api/captures`, `/`

**Files:**
- Create: `cwatlas_dash/app.py`
- Create: `cwatlas_dash/templates/index.html` (minimal placeholder — Task 8 fills it)
- Test: `tests/test_dash_app.py`

**Interfaces:**
- Consumes: every `sources.*` function from Tasks 2–6 (called as `sources.<name>` so tests can monkeypatch the module attributes).
- Produces:
  - `create_app(**overrides) -> Flask` with config keys `DATA_DIR: Path`, `SDR_HOST: str`, `SDR_PORT: int`, `LAT: float`, `LON: float`, `UNIT: str`
  - `GET /api/summary` → `{"generated_at", "service", "sdr", "adc", "totals", "windows": {"1h","12h","24h","7d"}, "hourly", "inflight", "solar", "journal"}`; any failing source yields `{"error": "<Type>: <msg>"}` under its key, response stays 200
  - `GET /api/captures?limit=N` → `{"captures": [...]}` (limit clamped to 1..500, default 50)
  - `GET /` → rendered `index.html`

- [ ] **Step 1: Write the failing tests**

`tests/test_dash_app.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_dash_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cwatlas_dash.app'`

- [ ] **Step 3: Implement `cwatlas_dash/app.py`**

```python
"""Flask app: one page + JSON endpoints over the sources layer.

Every source is guarded independently — a dead SDR or stopped collector
degrades its panel to {"error": ...}; /api/summary itself never 500s
because a source is down. That is the point of a status page."""
from __future__ import annotations

import time

from flask import Flask, jsonify, render_template, request

from . import sources

DEFAULTS = {
    "DATA_DIR": sources.DATA_DIR,
    "SDR_HOST": "192.168.2.46",
    "SDR_PORT": 8073,
    "LAT": 35.0,
    "LON": -97.0,
    "UNIT": "cwatlas-collector",
}


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — panel-level degradation by design
        return {"error": f"{type(e).__name__}: {e}"}


def create_app(**overrides) -> Flask:
    app = Flask(__name__)
    app.config.update({**DEFAULTS, **overrides})

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/summary")
    def summary():
        c = app.config
        db = c["DATA_DIR"] / "catalog.db"
        sdr = _guard(sources.sdr_snapshot, c["SDR_HOST"], c["SDR_PORT"])
        return jsonify({
            "generated_at": time.time(),
            "service": _guard(sources.system_health, c["UNIT"], c["DATA_DIR"]),
            "sdr": sdr.get("status", sdr),   # {"error":...} passes through whole
            "adc": sdr.get("adc", sdr),
            "totals": _guard(sources.totals, db_path=db),
            "windows": {w: _guard(sources.collection_stats, w, db_path=db)
                        for w in sources.WINDOWS},
            "hourly": _guard(sources.hourly_buckets, db_path=db),
            "inflight": _guard(sources.inflight, db_path=db),
            "solar": _guard(sources.solar_priorities, c["LAT"], c["LON"]),
            "journal": _guard(sources.journal_tail, c["UNIT"]),
        })

    @app.get("/api/captures")
    def captures():
        limit = max(1, min(request.args.get("limit", 50, type=int) or 50, 500))
        db = app.config["DATA_DIR"] / "catalog.db"
        return jsonify({"captures": _guard(sources.recent_captures,
                                           limit=limit, db_path=db)})

    return app
```

Note: `hourly` and `inflight` return lists on success; `_guard` returns a dict on failure — the frontend treats a non-array as the error state.

`cwatlas_dash/templates/index.html` (placeholder; Task 8 replaces it):

```html
<!doctype html>
<title>CWAtlas Collector</title>
<h1>CWAtlas Collector — dashboard loading…</h1>
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: PASS (22 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/app.py cwatlas_dash/templates/index.html tests/test_dash_app.py
git commit -m "dash: flask app — /api/summary with per-source degradation"
```

---

### Task 8: Frontend — single page, vanilla JS, inline SVG

**Files:**
- Modify: `cwatlas_dash/templates/index.html` (replace placeholder)
- Create: `cwatlas_dash/static/dash.css`
- Create: `cwatlas_dash/static/dash.js`
- Test: `tests/test_dash_frontend.py`

**Interfaces:**
- Consumes: `GET /api/summary` and `GET /api/captures?limit=50` JSON shapes from Task 7 (exact key names above).
- Produces: the rendered dashboard. Panel container ids (tests assert these): `panel-status`, `panel-totals`, `panel-windows`, `panel-chart`, `panel-inflight`, `panel-recent`, `panel-solar`, `panel-journal`.

Design notes (from the frontend-design/dataviz guidance): dark theme suited to a shack/ops page; one accent color for good states, a distinct alarm color reserved exclusively for errors/contamination; tabular numerals for stats; the 24 h chart is an inline SVG bar chart where the contaminated fraction of each bar is overlaid in the alarm color; every panel has a `.error` rendering state.

- [ ] **Step 1: Write the failing smoke test**

`tests/test_dash_frontend.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_dash_frontend.py -v`
Expected: FAIL (placeholder page has no panel ids)

- [ ] **Step 3: Write the three frontend files**

`cwatlas_dash/templates/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CWAtlas Collector</title>
<link rel="stylesheet" href="{{ url_for('static', filename='dash.css') }}">
</head>
<body>
<header>
  <h1>CWAtlas Collector <span class="sub">airig-01 &middot; Web-888 &rarr; MorseBase</span></h1>
  <div id="stale-banner" class="hidden">data stale — polling failed</div>
</header>
<main>
  <section id="panel-status" class="strip"></section>
  <section id="panel-totals" class="cards"></section>
  <section id="panel-windows" class="cards"></section>
  <section id="panel-chart" class="wide"><h2>Captures — last 24 h</h2><div class="body"></div></section>
  <section id="panel-inflight" class="wide"><h2>Capturing now</h2><div class="body"></div></section>
  <section id="panel-recent" class="wide"><h2>Recent captures</h2><div class="body"></div></section>
  <section id="panel-solar" class="wide"><h2>Band priorities (solar baseline)</h2><div class="body"></div></section>
  <section id="panel-journal" class="wide"><h2>Collector journal</h2><div class="body"></div></section>
</main>
<script src="{{ url_for('static', filename='dash.js') }}"></script>
</body>
</html>
```

`cwatlas_dash/static/dash.css`:

```css
:root {
  --bg: #101418; --panel: #1a2027; --line: #2a323c;
  --fg: #d7dee6; --dim: #8494a6;
  --ok: #3fb68b; --warn: #d9a13f; --bad: #d95f4f; --accent: #4f9dd9;
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--bg); color: var(--fg);
       font: 14px/1.45 system-ui, sans-serif; padding: 0 1rem 3rem; }
header { display: flex; align-items: baseline; gap: 1rem; padding: 1rem 0; }
h1 { font-size: 1.15rem; } h2 { font-size: .8rem; text-transform: uppercase;
     letter-spacing: .08em; color: var(--dim); margin-bottom: .5rem; }
.sub { color: var(--dim); font-size: .8rem; font-weight: normal; }
main { display: grid; gap: .8rem; max-width: 1100px; margin: auto; }
section { background: var(--panel); border: 1px solid var(--line);
          border-radius: 8px; padding: .8rem 1rem; }
.strip { display: flex; flex-wrap: wrap; gap: 1.5rem; }
.strip .item b { display: block; font-size: .7rem; color: var(--dim);
                 text-transform: uppercase; letter-spacing: .06em; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr));
         gap: .8rem; background: none; border: none; padding: 0; }
.card { background: var(--panel); border: 1px solid var(--line);
        border-radius: 8px; padding: .8rem 1rem; }
.card .big { font-size: 1.5rem; font-variant-numeric: tabular-nums; }
.card h3 { font-size: .7rem; color: var(--dim); text-transform: uppercase; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .25rem .5rem; border-bottom: 1px solid var(--line); }
th { color: var(--dim); font-size: .7rem; text-transform: uppercase; }
td.num, th.num { text-align: right; }
pre { overflow-x: auto; font-size: .75rem; color: var(--dim); max-height: 16rem; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
.error { color: var(--bad); font-style: italic; }
.hidden { display: none; }
#stale-banner { background: var(--bad); color: #fff; padding: .2rem .6rem;
                border-radius: 4px; font-size: .8rem; }
svg .bar { fill: var(--accent); } svg .contam { fill: var(--bad); }
svg text { fill: var(--dim); font-size: 9px; }
.wide .body { overflow-x: auto; }
```

`cwatlas_dash/static/dash.js`:

```javascript
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const kHz = (hz) => (hz / 1e3).toFixed(2) + " kHz";
const gb = (b) => (b / 1e9).toFixed(2) + " GB";
const ts = (t) => new Date(t * 1000).toLocaleString();
const dur = (s) => s == null ? "—" :
  s < 3600 ? Math.round(s / 60) + " m" : (s / 3600).toFixed(1) + " h";

function errCard(el, err) {
  el.innerHTML = `<div class="error">unavailable — ${esc(err)}</div>`;
}
const failed = (d) => d && !Array.isArray(d) && d.error !== undefined;

function renderStatus(el, svc, sdr, adc, totals) {
  if (failed(svc)) return errCard(el, svc.error);
  const stateCls = svc.active_state === "active" ? "ok" : "bad";
  const sdrHtml = failed(sdr)
    ? `<span class="bad">unreachable</span>`
    : `<span class="ok">ok</span> <span class="sub">gps ${esc(sdr.gps ?? "?")}</span>`;
  const ov = failed(adc) ? "?" : adc.ov_mask;
  const ovCls = ov === "0" ? "ok" : "bad";
  const d = svc.disk || {};
  const freePct = d.total ? (100 * d.free / d.total).toFixed(0) : "?";
  el.innerHTML = `
    <div class="item"><b>collector</b>
      <span class="${stateCls}">${esc(svc.active_state)}</span>
      <span class="sub">up ${dur(svc.uptime_s)} · ${svc.n_restarts ?? 0} restarts</span></div>
    <div class="item"><b>sdr</b> ${sdrHtml}</div>
    <div class="item"><b>adc overload</b> <span class="${ovCls}">${esc(ov)}</span></div>
    <div class="item"><b>in flight</b> ${failed(totals) ? "?" : totals.in_flight}</div>
    <div class="item"><b>disk (${esc(d.path ?? "?")})</b>
      ${d.free ? gb(d.free) : "?"} free (${freePct}%)</div>`;
}

const card = (title, big, sub = "") =>
  `<div class="card"><h3>${esc(title)}</h3><div class="big">${big}</div>
   <div class="sub">${sub}</div></div>`;

function renderTotals(el, t) {
  if (failed(t)) { el.innerHTML = ""; return errCard(el, t.error); }
  el.innerHTML =
    card("captures (all time)", t.captures) +
    card("IQ hours", t.iq_hours) +
    card("corpus size", gb(t.bytes)) +
    card("contaminated", t.contaminated,
         t.captures ? (100 * t.contaminated / t.captures).toFixed(1) + " %" : "");
}

function renderWindows(el, windows) {
  el.innerHTML = Object.entries(windows).map(([w, s]) => {
    if (failed(s)) return card(w, `<span class="bad">err</span>`, esc(s.error));
    const bands = Object.entries(s.by_band)
      .map(([b, v]) => `${esc(b)} ${v.captures}`).join(" · ") || "—";
    return card(`last ${w}`, s.captures,
      `${s.iq_hours} IQ h · ${s.contaminated} contam.<br>${bands}`);
  }).join("");
}

function renderChart(el, hourly) {
  if (failed(hourly)) return errCard(el, hourly.error);
  const W = 960, H = 140, PAD = 20, bw = (W - PAD) / hourly.length;
  const max = Math.max(1, ...hourly.map((b) => b.captures));
  const bars = hourly.map((b, i) => {
    const h = (H - 30) * b.captures / max;
    const hc = (H - 30) * b.contaminated / max;
    const x = PAD + i * bw, y = H - 20 - h;
    const label = (b.ago_h % 6 === 0)
      ? `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">-${b.ago_h}h</text>` : "";
    return `<rect class="bar" x="${x}" y="${y}" width="${bw - 2}" height="${h}">
      <title>${b.ago_h}h ago: ${b.captures} captures, ${b.contaminated} contaminated, ${b.iq_hours} IQ h</title></rect>
      <rect class="contam" x="${x}" y="${H - 20 - hc}" width="${bw - 2}" height="${hc}"/>${label}`;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img"
    aria-label="captures per hour, last 24 hours">
    <text x="${PAD}" y="10">${max}</text>${bars}</svg>`;
}

const table = (heads, rows) => `<table><tr>${heads.map(([h, cls]) =>
  `<th class="${cls || ""}">${esc(h)}</th>`).join("")}</tr>${rows}</table>`;

function renderInflight(el, rows) {
  if (failed(rows)) return errCard(el, rows.error);
  if (!rows.length) { el.innerHTML = `<div class="sub">idle — no captures in flight</div>`; return; }
  el.innerHTML = table(
    [["freq"], ["band"], ["dwell", "num"], ["snr dB", "num"], ["keyed", "num"], [""]],
    rows.map((r) => `<tr><td>${kHz(r.freq_hz)}</td><td>${esc(r.band)}</td>
      <td class="num">${dur(r.dwell_s)}</td>
      <td class="num">${r.strength_db?.toFixed(0) ?? "—"}</td>
      <td class="num">${r.keyed_conf?.toFixed(2) ?? "—"}</td>
      <td>${r.stale ? '<span class="warn">stale?</span>' : ""}</td></tr>`).join(""));
}

function renderRecent(el, rows) {
  if (failed(rows)) return errCard(el, rows.error);
  el.innerHTML = table(
    [["started"], ["freq"], ["band"], ["dur", "num"], ["snr dB", "num"],
     ["keyed", "num"], [""]],
    rows.map((r) => `<tr><td>${ts(r.started_utc)}</td><td>${kHz(r.freq_hz)}</td>
      <td>${esc(r.band)}</td><td class="num">${dur(r.duration_s)}</td>
      <td class="num">${r.strength_db?.toFixed(0) ?? "—"}</td>
      <td class="num">${r.keyed_conf?.toFixed(2) ?? "—"}</td>
      <td>${r.contaminated ? '<span class="bad">contam.</span>' : ""}</td></tr>`).join(""));
}

function renderSolar(el, s) {
  if (failed(s)) return errCard(el, s.error);
  const rows = Object.entries(s.weights).map(([b, w]) =>
    `<tr><td>${esc(b)}</td><td class="num">${w.toFixed(1)}</td></tr>`).join("");
  el.innerHTML = `<div class="sub">phase: <b>${esc(s.phase)}</b> · nudges: n/a (MCP offline)</div>
    ${table([["band"], ["weight", "num"]], rows)}`;
}

function renderJournal(el, j) {
  if (failed(j)) return errCard(el, j.error);
  const cls = j.errors ? "bad" : "ok";
  el.innerHTML = `<div class="sub"><span class="${cls}">${j.errors} error lines</span>
    in last ${j.lines.length}</div><pre>${esc(j.lines.slice(-30).join("\n"))}</pre>`;
}

let missedPolls = 0;
async function poll() {
  try {
    const [sumR, capR] = await Promise.all([
      fetch("/api/summary"), fetch("/api/captures?limit=50")]);
    const d = await sumR.json(), caps = (await capR.json()).captures;
    missedPolls = 0;
    renderStatus($("panel-status"), d.service, d.sdr, d.adc, d.totals);
    renderTotals($("panel-totals"), d.totals);
    renderWindows($("panel-windows"), d.windows);
    renderChart($("panel-chart").querySelector(".body"), d.hourly);
    renderInflight($("panel-inflight").querySelector(".body"), d.inflight);
    renderRecent($("panel-recent").querySelector(".body"), caps);
    renderSolar($("panel-solar").querySelector(".body"), d.solar);
    renderJournal($("panel-journal").querySelector(".body"), d.journal);
  } catch (e) {
    missedPolls += 1;
  }
  $("stale-banner").classList.toggle("hidden", missedPolls < 2);
}
poll();
setInterval(poll, 15000);
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: PASS (24 passed)

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/templates/index.html cwatlas_dash/static/dash.css cwatlas_dash/static/dash.js tests/test_dash_frontend.py
git commit -m "dash: single-page frontend — panels, 24h SVG chart, 15s poll"
```

---

### Task 9: Entrypoint + live verification on airig-01

**Files:**
- Create: `cwatlas_dash/__main__.py`
- Test: manual verification against the live catalog/SDR (this host IS production)

**Interfaces:**
- Consumes: `create_app(**overrides)` from Task 7.
- Produces: `python -m cwatlas_dash --host 0.0.0.0 --port 8828` runs the server; env fallbacks `CWATLAS_DATA_DIR`, `CWATLAS_SDR_HOST`, `CWATLAS_LAT`, `CWATLAS_LON` match the collector's conventions.

- [ ] **Step 1: Implement `cwatlas_dash/__main__.py`**

```python
"""python -m cwatlas_dash — serve the dashboard.

Env fallbacks mirror the collector's (CWATLAS_SDR_HOST, CWATLAS_DATA_DIR,
CWATLAS_LAT/LON) so the systemd unit can share its Environment= lines."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="CWAtlas status dashboard")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8828)
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("CWATLAS_DATA_DIR",
                                                "~/cwatlas/data")).expanduser())
    ap.add_argument("--sdr-host",
                    default=os.environ.get("CWATLAS_SDR_HOST", "192.168.2.46"))
    ap.add_argument("--lat", type=float,
                    default=float(os.environ.get("CWATLAS_LAT", "35.0")))
    ap.add_argument("--lon", type=float,
                    default=float(os.environ.get("CWATLAS_LON", "-97.0")))
    args = ap.parse_args()

    app = create_app(DATA_DIR=args.data_dir, SDR_HOST=args.sdr_host,
                     LAT=args.lat, LON=args.lon)
    # Built-in server, threaded: single-operator LAN dashboard. Waitress is
    # the drop-in if this ever needs hardening.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run against the live sources** (read-only — safe next to the running collector)

```bash
cd /home/dnelms/cwatlas/collector
CWATLAS_DATA_DIR=/mnt/md0/cwatlas/data .venv/bin/python -m cwatlas_dash --port 8828 &
sleep 2
curl -s http://localhost:8828/api/summary | .venv/bin/python -m json.tool | head -60
curl -s "http://localhost:8828/api/captures?limit=3" | .venv/bin/python -m json.tool
```

Expected: `service.active_state == "active"`, `sdr` has real status keys (or a clear error if the SDR is briefly unreachable), `totals.captures` > 0, all four windows present, 24 hourly buckets, captures list non-empty.

**Check specifically:** `journal` — if it shows the permission error, note it in the task report; the fix (outside this plan's file changes) is `sudo usermod -aG systemd-journal dnelms` + re-login, or accept the degraded panel.

- [ ] **Step 3: Verify the page renders** — `curl -s http://localhost:8828/ | grep -c panel-` → expect `8`. If a browser/Playwright is available, load `http://localhost:8828/`, confirm panels populate and no console errors; otherwise curl checks suffice.

- [ ] **Step 4: Stop the ad-hoc server**

```bash
pkill -f "python -m cwatlas_dash"    # job-control %1 is unreliable across separate shell invocations
```

- [ ] **Step 5: Commit**

```bash
git add cwatlas_dash/__main__.py
git commit -m "dash: __main__ entrypoint (0.0.0.0:8828, collector-style env)"
```

---

### Task 10: systemd unit — install, enable, verify

**Files:**
- Create: `deploy/cwatlas-dash.service`

**Interfaces:**
- Consumes: `python -m cwatlas_dash` entrypoint from Task 9.
- Produces: dashboard running at boot on airig-01, `http://airig-01:8828/`.

- [ ] **Step 1: Write `deploy/cwatlas-dash.service`**

```ini
[Unit]
Description=CWAtlas status dashboard (read-only Flask sidecar, port 8828)
# Independent of cwatlas-collector by design (Model B): the dashboard must
# render (and show RED) precisely when the collector is down.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dnelms
WorkingDirectory=/home/dnelms/cwatlas/collector
Environment=PYTHONUNBUFFERED=1
Environment=CWATLAS_SDR_HOST=192.168.2.46
Environment=CWATLAS_DATA_DIR=/mnt/md0/cwatlas/data
Environment=CWATLAS_LAT=35.0
Environment=CWATLAS_LON=-97.0
ExecStart=/home/dnelms/cwatlas/collector/.venv/bin/python -m cwatlas_dash --host 0.0.0.0 --port 8828
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Install and enable** (requires sudo — if unavailable non-interactively, hand the exact commands to the operator and stop here)

```bash
sudo cp deploy/cwatlas-dash.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cwatlas-dash
```

- [ ] **Step 3: Verify**

```bash
systemctl is-enabled cwatlas-dash    # expect: enabled
systemctl is-active cwatlas-dash     # expect: active
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8828/          # 200
curl -s http://localhost:8828/api/summary | grep -o 'active_state[^,]*'   # expect ..."active"
systemctl is-active cwatlas-collector   # still: active (we changed nothing)
```

- [ ] **Step 4: Commit**

```bash
git add deploy/cwatlas-dash.service
git commit -m "deploy: cwatlas-dash systemd unit (enable --now on airig-01)"
```

---

## Final acceptance (maps to spec)

- [ ] `http://airig-01:8828/` reachable from another LAN machine; all 8 panels render
- [ ] Totals + 1h/12h/24h/7d windows populated from the live catalog
- [ ] SDR status/ADC shown; **no new WS connections to the SDR** (`curl -s http://192.168.2.46:8073/users` count unchanged while the dashboard runs)
- [ ] Stop test acceptable to operator only if they ask — otherwise verify degradation by pointing a dev instance at a bogus `--sdr-host` and confirming the SDR panel shows the error while catalog panels still render
- [ ] `systemctl is-enabled cwatlas-dash` → `enabled` (survives reboot)
- [ ] Zero modifications under `cwatlas_mcp/` (`git diff 5cb0743..HEAD --stat -- cwatlas_mcp/` is empty — 5cb0743 is the approved-spec commit this plan starts from)
