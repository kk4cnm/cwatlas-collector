"""Flask app: one page + JSON endpoints over the sources layer.

Every source is guarded independently — a dead SDR or stopped collector
degrades its panel to {"error": ...}; /api/summary itself never 500s
because a source is down. That is the point of a status page."""
from __future__ import annotations

import time

from flask import Flask, g, jsonify, render_template, request

from . import sources
from .telemetry import get_telemetry, request_start_time

# Site details come from config.toml / env / CLI (see cwatlas_mcp.config and
# config.example.toml) — never from source. A dashboard that ships one
# operator's LAN address and antenna location as its defaults is a dashboard
# that quietly points at the wrong station for everyone else.
DEFAULTS = {
    "DATA_DIR": sources.DATA_DIR,
    "SDR_HOST": None,
    "SDR_PORT": 8073,
    "LAT": float("nan"),      # NaN -> solar panel reports "not configured"
    "LON": float("nan"),
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
    telemetry = get_telemetry()

    @app.before_request
    def _otel_before_request():
        g._otel_started_s = request_start_time()

    @app.after_request
    def _otel_after_request(response):
        started = getattr(g, "_otel_started_s", None)
        if started is not None:
            endpoint = request.url_rule.rule if request.url_rule else request.endpoint or "unknown"
            telemetry.record_request(endpoint, response.status_code,
                                     time.perf_counter() - started)
        return response

    @app.get("/")
    def index():
        with telemetry.span("cwatlas_dash.index", route="/"):
            return render_template("index.html")

    @app.get("/api/summary")
    def summary():
        c = app.config
        db = c["DATA_DIR"] / "catalog.db"
        started = time.perf_counter()
        with telemetry.span("cwatlas_dash.summary", route="/api/summary"):
            sdr = _guard(sources.sdr_snapshot, c["SDR_HOST"], c["SDR_PORT"])
            payload = {
                "generated_at": time.time(),
                "service": _guard(sources.system_health, c["UNIT"], c["DATA_DIR"]),
                "sdr": sdr.get("status", sdr),   # {"error":...} passes through whole
                "adc": sdr.get("adc", sdr),
                "totals": _guard(sources.totals, db_path=db),
                "windows": {w: _guard(sources.collection_stats, w, db_path=db)
                            for w in sources.WINDOWS},
                "hourly": _guard(sources.hourly_buckets, db_path=db),
                "inflight": _guard(sources.inflight, db_path=db),
                "provenance": _guard(sources.provenance_health, db_path=db),
                "solar": _guard(sources.solar_priorities, c["LAT"], c["LON"]),
                "journal": _guard(sources.journal_tail, c["UNIT"]),
            }
            telemetry.record_summary(payload, time.perf_counter() - started)
            return jsonify(payload)

    @app.get("/api/captures")
    def captures():
        limit = request.args.get("limit", type=int)  # None if absent/unparseable
        limit = 50 if limit is None else limit       # default only when missing
        limit = max(1, min(limit, 500))              # clamp 1..500 (0 -> 1)
        db = app.config["DATA_DIR"] / "catalog.db"
        return jsonify({"captures": _guard(sources.recent_captures,
                                           limit=limit, db_path=db)})

    return app
