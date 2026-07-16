"""python -m cwatlas_dash — serve the dashboard.

Reads the same site config as the collector (config.toml / env / CLI, in
reverse precedence — see cwatlas_mcp.config), so one file describes the station
and both services agree about where it is.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from cwatlas_mcp import config

from .app import create_app


def main() -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None,
                     help="site config TOML (default: $CWATLAS_CONFIG, else "
                          "config.toml beside the package)")
    pre_args, _ = pre.parse_known_args()
    cfg_file = config.load(pre_args.config)

    ap = argparse.ArgumentParser(description="CWAtlas status dashboard",
                                 parents=[pre])
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=8828)
    ap.add_argument("--data-dir", type=Path,
                    default=config.pick(cfg_file, "paths.data_dir",
                                        "CWATLAS_DATA_DIR", "~/cwatlas/data"))
    ap.add_argument("--sdr-host",
                    default=config.pick(cfg_file, "sdr.host", "CWATLAS_SDR_HOST"))
    ap.add_argument("--sdr-port", type=int,
                    default=config.pick(cfg_file, "sdr.port", default=8073,
                                        cast=int))
    # NaN when unset: the solar panel then reports "not configured" rather than
    # confidently weighting bands for somewhere nobody lives.
    ap.add_argument("--lat", type=float,
                    default=config.pick(cfg_file, "station.lat", "CWATLAS_LAT",
                                        float("nan"), cast=float))
    ap.add_argument("--lon", type=float,
                    default=config.pick(cfg_file, "station.lon", "CWATLAS_LON",
                                        float("nan"), cast=float))
    args = ap.parse_args()

    app = create_app(DATA_DIR=Path(args.data_dir).expanduser(),
                     SDR_HOST=args.sdr_host, SDR_PORT=args.sdr_port,
                     LAT=args.lat, LON=args.lon)
    # Built-in server, threaded: single-operator LAN dashboard. Waitress is
    # the drop-in if this ever needs hardening.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
