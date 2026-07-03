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
    "LAT": 33.427,
    "LON": -82.208,
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
        limit = request.args.get("limit", type=int)  # None if absent/unparseable
        limit = 50 if limit is None else limit       # default only when missing
        limit = max(1, min(limit, 500))              # clamp 1..500 (0 -> 1)
        db = app.config["DATA_DIR"] / "catalog.db"
        return jsonify({"captures": _guard(sources.recent_captures,
                                           limit=limit, db_path=db)})

    return app
