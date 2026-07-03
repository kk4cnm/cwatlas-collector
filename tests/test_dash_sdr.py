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
