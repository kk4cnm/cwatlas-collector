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
           "2026-07-03T10:00:02 airig-01 python[1]: ValueError: boom\n"
           "2026-07-03T10:00:03 airig-01 python[1]: No errors detected in scan\n"
           "2026-07-03T10:00:04 airig-01 python[1]: fail-safe mode disabled\n"
           "2026-07-03T10:00:05 airig-01 systemd[1]: Failed to start"
           " cwatlas-collector.service\n")
    j = sources.journal_tail(run=fake_run_factory(out))
    assert len(j["lines"]) == 6
    assert j["errors"] == 3


def test_journal_tail_permission_denied():
    with pytest.raises(RuntimeError, match="journal"):
        sources.journal_tail(run=fake_run_factory("", returncode=1))
