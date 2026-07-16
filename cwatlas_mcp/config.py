"""Site configuration: where this particular station's details live.

Nothing about one operator's rig — LAN addresses, antenna location, data paths —
belongs in source. They live in a config.toml that is NOT tracked; see
config.example.toml for the shape.

Precedence, highest first:

    1. CLI flag          --host 10.0.0.5
    2. environment       CWATLAS_SDR_HOST=10.0.0.5   (what the systemd unit uses)
    3. config.toml       [sdr] host = "10.0.0.5"
    4. built-in default   — and there deliberately isn't one for the SDR host or
                            the station location: a wrong guess is worse than a
                            clear error, and a wrong LAT/LON silently mis-weights
                            every band.

A missing config file is fine (env-only setups are normal, and the unit is
env-driven). A malformed one raises at startup, loudly, like everything else
that means the collector cannot know what it's doing.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV_CONFIG = "CWATLAS_CONFIG"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def find_path(explicit: str | Path | None = None) -> Path | None:
    """-> the config file to use, or None if there isn't one.

    A file named explicitly (--config or $CWATLAS_CONFIG) MUST exist. It does
    not fall back to the default search: the operator who typed a path thinks
    their settings are loaded, and quietly loading a different station's file
    instead is worse than any error — for lat/lon it's wrong band weighting all
    night, looking like nothing is wrong.
    """
    named = explicit or os.environ.get(ENV_CONFIG)
    if named:
        path = Path(named)
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {named}")
        return path
    default = _REPO_ROOT / "config.toml"
    return default if default.is_file() else None


def load(explicit: str | Path | None = None) -> dict:
    path = find_path(explicit)
    if path is None:
        return {}
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg["_path"] = str(path)      # recorded in provenance; see effective_config
    return cfg


def _dig(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def pick(cfg: dict, dotted: str, env: str | None = None, default=None,
         cast=None):
    """One setting, resolved by precedence. CLI is argparse's job — this is the
    `default=` it falls back to."""
    if env and os.environ.get(env):
        raw = os.environ[env]
        return cast(raw) if cast else raw
    found = _dig(cfg, dotted)
    if found is not None:
        return cast(found) if cast else found
    return default
