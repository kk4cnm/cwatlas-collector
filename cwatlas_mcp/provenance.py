"""What was running when a capture was made.

The corpus records what was captured in detail; without this module it records
nothing about the conditions of capture. Five years on, "which band weights
biased this assignment?" or "which detector thresholds were in force?" should
have an answer, not a guess.

One `runs` row per collector process (see catalog.begin_run). Everything here is
read ONCE at startup, never per capture — so it can afford to shell out to git.

Two rules this module exists to honour:
  * An unrecorded fact is NULL, never a plausible reconstruction. A guess that
    reads like an observation is worse than a gap.
  * Nothing here may raise into the capture path. Provenance describes the
    collection; it must never be the reason collection stops.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import platform
import socket
import subprocess
import time
from pathlib import Path

from . import __version__
from .detector import detect_cw
from .dsp import CARRIER_OUT_HZ, DECIM, FS_IN, FS_OUT
from .solar import BAND_WEIGHTS

# The package is installed editable (pip install -e .), so the source tree IS the
# checkout and its parent is the repo root. Deploys are `git pull` + restart with
# no build step, which is exactly why a build-time version stamp would lie here.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str, timeout: float = 2.0) -> str | None:
    """Run a git command in the repo. -> stdout, or None if it can't be known.

    Never raises: no git, not a checkout, or a held index.lock all mean "we don't
    know", and NULL says that honestly. cwd is pinned to the source tree rather
    than os.getcwd() so this doesn't depend on systemd's WorkingDirectory.
    """
    try:
        r = subprocess.run(("git",) + args, cwd=_REPO_ROOT, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def git_state() -> dict:
    """-> {git_commit, git_dirty, git_diff_sha256}, any of which may be None.

    Dirty means `git diff HEAD` is non-empty: the tracked source on disk differs
    from the commit, i.e. the code that ran is not the code at git_commit. Note
    this deliberately does NOT use `git status --porcelain`, which also reports
    index state and untracked files — neither of which changes what executed, and
    a flag that trips on an untracked scratch file is a flag you learn to ignore.
    The known gap: an untracked new module that gets imported reads as clean.

    When dirty, git_diff_sha256 fingerprints the actual diff, so two dirty runs
    carrying different uncommitted changes are distinguishable. Without it,
    "dirty" is an unfalsifiable shrug.
    """
    commit = _git("rev-parse", "HEAD")
    if commit is None:
        return {"git_commit": None, "git_dirty": None, "git_diff_sha256": None}
    diff = _git("diff", "HEAD")
    if diff is None:
        return {"git_commit": commit.strip(), "git_dirty": None,
                "git_diff_sha256": None}
    dirty = bool(diff.strip())
    return {
        "git_commit": commit.strip(),
        "git_dirty": int(dirty),
        "git_diff_sha256": (hashlib.sha256(diff.encode()).hexdigest()
                            if dirty else None),
    }


def _defaults_of(fn) -> dict:
    """Keyword defaults of a function, read from the signature.

    Read rather than retyped on purpose: a hand-copied dict of thresholds is
    exactly the "someone tuned it and forgot to update the record" failure this
    module exists to prevent.
    """
    return {k: v.default for k, v in inspect.signature(fn).parameters.items()
            if v.default is not inspect.Parameter.empty}


def effective_config(args, cfg, search_plan) -> dict:
    """The resolved config this process is actually running under.

    Effective, not source-literal: n_rx_channels is hardware-derived and the args
    are CLI/env, so these vary independently of git_commit. That's precisely the
    part a commit hash cannot tell you.
    """
    return {
        "scheduler": dataclasses.asdict(cfg),
        "detector": _defaults_of(detect_cw),
        "solar": {"band_weights": {b: list(w) for b, w in BAND_WEIGHTS.items()}},
        # determines the on-disk sample format — the most consequential thing
        # here, and the one a future reader of the raw IQ most needs
        "dsp": {"fs_in_hz": FS_IN, "fs_out_hz": FS_OUT, "decim": DECIM,
                "carrier_out_hz": CARRIER_OUT_HZ},
        # what was lookable-for; shapes the corpus as much as the detector does
        "search_plan": [[band, cf] for band, cf in search_plan],
        "args": {
            "rotate_s": args.rotate_s,
            "lat": args.lat if args.lat == args.lat else None,   # NaN -> null
            "lon": args.lon if args.lon == args.lon else None,
            "trial": args.trial,
            "no_mcp": args.no_mcp,
            "flex_ptt": bool(args.flex_host),   # a LAN IP isn't provenance
            "sdr": f"{args.host}:{args.port}",
        },
    }


def config_sha256(config: dict) -> str:
    """Grouping key over effective_config: "which captures ran under the old
    band weights?" is then one GROUP BY, with the weights still readable."""
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_run_info(args, dev: dict, cfg, search_plan) -> dict:
    """Everything known about this process at startup -> a runs row.

    `dev` is the Web-888's MSG config from SdrClient.read_config().
    """
    config = effective_config(args, cfg, search_plan)
    maj, min_ = dev.get("version_maj"), dev.get("version_min")
    return {
        "kind": "collector",
        "started_utc": time.time(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "collector_version": __version__,
        **git_state(),
        "python_version": platform.python_version(),
        "sdr_host": f"{args.host}:{args.port}",
        "sdr_firmware": f"{maj}.{min_}" if maj is not None else None,
        "sdr_rx_chans": (int(dev["rx_chans"]) if dev.get("rx_chans") is not None
                         else None),
        "config_json": json.dumps(config, sort_keys=True, separators=(",", ":")),
        "config_sha256": config_sha256(config),
        "note": None,
    }
