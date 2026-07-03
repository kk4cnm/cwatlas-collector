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
                    default=float(os.environ.get("CWATLAS_LAT", "33.427")))
    ap.add_argument("--lon", type=float,
                    default=float(os.environ.get("CWATLAS_LON", "-82.208")))
    args = ap.parse_args()

    app = create_app(DATA_DIR=args.data_dir, SDR_HOST=args.sdr_host,
                     LAT=args.lat, LON=args.lon)
    # Built-in server, threaded: single-operator LAN dashboard. Waitress is
    # the drop-in if this ever needs hardening.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
