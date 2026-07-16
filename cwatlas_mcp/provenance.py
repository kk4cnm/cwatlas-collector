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
import re
import socket
import sqlite3
import subprocess
import time
from importlib.metadata import PackageNotFoundError, requires, version
from pathlib import Path

from . import __version__
from .detector import detect_cw
from .dsp import CARRIER_OUT_HZ, DECIM, FS_IN, FS_OUT
from .solar import BAND_WEIGHTS

DIST = "cwatlas-collector"

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


def _declared_runtime_packages(dist: str = DIST) -> list[str]:
    """The distribution's own runtime requirements, by name.

    Derived, not curated: pyproject decides WHICH packages are relevant, so
    adding a dependency puts it in provenance without anyone remembering to.
    A hand-listed set is maintainable state that goes stale — the same failure
    as retyping detector thresholds instead of reading the signature.

    Extras are excluded: dev (pytest/ruff) and dash (flask/otel) are not the
    collector's runtime and none of them can touch the corpus.
    """
    try:
        reqs = requires(dist) or []
    except PackageNotFoundError:
        return []
    names = []
    for req in reqs:
        spec, _, marker = req.partition(";")
        if "extra" in marker:
            continue
        m = re.match(r"[A-Za-z0-9._-]+", spec.strip())
        if m:
            names.append(m.group(0))
    return sorted(names)


def dependency_versions() -> dict:
    """What's actually installed, as opposed to what pyproject asked for.

    Declared requirements describe intent ("numpy>=1.26"); this describes
    reality ("2.5.0"). The gap matters: `pip install -U numpy` changes the
    decimator's arithmetic — and therefore the IQ on disk — while git_commit and
    config_sha256 both stay put. Direct requirements only: numpy and websockets,
    the two that shape the corpus, have no runtime deps of their own, while mcp
    drags 17 and is dormant in production under --no-mcp. Hashing that tree would
    churn on code that never runs.
    """
    packages: dict[str, str | None] = {}
    for name in _declared_runtime_packages():
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None       # declared but absent: an honest gap
    return {
        "packages": packages,
        # The linked C library, NOT a pip distribution — no freeze would ever
        # show it, and an OS upgrade moves it silently. It's a real dependency:
        # mark_window's RETURNING needs >= 3.35. Nested under its own key rather
        # than mixed in with `packages`, which would be a small lie about what
        # kind of thing it is.
        "sqlite3": sqlite3.sqlite_version,
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
            # WHICH config file was in force. The values it produced are already
            # resolved into this snapshot, so this is for the human asking
            # "where did that come from?" — the archived Phase-1 schema had
            # sessions.config_path for the same reason.
            "config_path": getattr(args, "config", None),
        },
    }


def _canonical(obj) -> str:
    """Stable JSON: same content -> same bytes -> same hash, whatever the dict
    insertion order was."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def config_sha256(config: dict) -> str:
    """Grouping key over effective_config: "which captures ran under the old
    band weights?" is then one GROUP BY, with the weights still readable."""
    return hashlib.sha256(_canonical(config).encode()).hexdigest()


def dependencies_sha256(deps: dict) -> str:
    """Same trick for the environment: "which captures ran under numpy 1.x?"."""
    return hashlib.sha256(_canonical(deps).encode()).hexdigest()


def build_run_info(args, dev: dict, cfg, search_plan) -> dict:
    """Everything known about this process at startup -> a runs row.

    `dev` is the Web-888's MSG config from SdrClient.read_config().
    """
    config = effective_config(args, cfg, search_plan)
    deps = dependency_versions()
    maj, min_ = dev.get("version_maj"), dev.get("version_min")
    return {
        "kind": "collector",
        "started_utc": time.time(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "collector_version": __version__,
        **git_state(),
        "python_version": platform.python_version(),
        "dependencies_json": _canonical(deps),
        "dependencies_sha256": dependencies_sha256(deps),
        "sdr_host": f"{args.host}:{args.port}",
        "sdr_firmware": f"{maj}.{min_}" if maj is not None else None,
        "sdr_rx_chans": (int(dev["rx_chans"]) if dev.get("rx_chans") is not None
                         else None),
        "config_json": json.dumps(config, sort_keys=True, separators=(",", ":")),
        "config_sha256": config_sha256(config),
        "note": None,
    }
