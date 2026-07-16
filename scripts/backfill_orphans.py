"""Close catalog rows orphaned by a mid-capture disk failure, and rebuild meta.

An orphan is a row whose worker died between start_capture() and
finalize_capture() — ended_utc stays NULL forever, so the dash shows it
"capturing" indefinitely and mark_window's COALESCE keeps sweeping it. The IQ
itself is fine: .sigmf-data was written through an already-open fd. Everything
finalize would have written is recoverable from the row plus the file:

    n_samples <- filesize // 4      (ci16_le = 4 bytes per complex sample)
    ended_utc <- data file mtime    (when writing actually stopped)

Not recoverable: smeter_avg and gps_start_sec (accumulated in worker memory).
Those stay NULL — an honest gap beats a fabricated average.

Safety: only rows in flight longer than STALE_AFTER_S (2x the 600 s rotate
period, same threshold the dash uses) whose data file has stopped growing are
touched, so a live capture can never be clobbered. Dry-run unless --apply.

    python -m scripts.backfill_orphans            # report only
    python -m scripts.backfill_orphans --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cwatlas_mcp.dsp import CARRIER_OUT_HZ  # noqa: E402

DATA_DIR = Path(os.environ.get("CWATLAS_DATA_DIR", "/mnt/md0/cwatlas/data"))
DB_PATH = DATA_DIR / "catalog.db"
STALE_AFTER_S = 1200.0     # matches cwatlas_dash.sources.inflight
QUIESCENT_S = 120.0        # data file must not have grown this recently
BYTES_PER_SAMPLE = 4       # ci16_le


def _meta(row: sqlite3.Row, n_samples: int) -> dict:
    """Same shape as capture._sigmf_meta, rebuilt from the catalog row."""
    return {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": row["srate_hz"],
            "core:version": "1.0.0",
            "core:description": "CWAtlas CW capture (Web-888, AGC off; device "
                                "passband 750-1250 Hz shifted -750 Hz and "
                                "decimated 12k->1.5k on capture; CW carrier at "
                                f"~+{CARRIER_OUT_HZ:.0f} Hz baseband)",
            "core:recorder": "cwatlas-collector",
            "cwatlas:band": row["band"],
            "cwatlas:strength_db": row["strength_db"],
            "cwatlas:keyed_confidence": row["keyed_conf"],
            "cwatlas:contaminated": bool(row["contaminated"]),
            "cwatlas:gps_start_sec": row["gps_start_sec"],
            "cwatlas:carrier_offset_hz": CARRIER_OUT_HZ,
            "cwatlas:decimated_from_hz": 12000,
            # provenance: this pair was reconstructed, not written live
            "cwatlas:recovered": True,
        },
        "captures": [{
            "core:sample_start": 0,
            "core:frequency": row["freq_hz"] - CARRIER_OUT_HZ,
            "core:datetime": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["started_utc"])),
        }],
        "annotations": [{
            "core:sample_start": 0,
            "core:sample_count": n_samples,
            "core:freq_lower_edge": row["freq_hz"] - 250.0,
            "core:freq_upper_edge": row["freq_hz"] + 250.0,
            "core:label": "CW candidate",
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: report only)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    now = time.time()
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM captures WHERE ended_utc IS NULL AND started_utc < ?"
        " ORDER BY started_utc", (now - STALE_AFTER_S,)).fetchall()

    if not rows:
        print("no orphaned rows")
        return 0

    plan, skipped = [], []
    for r in rows:
        data = Path(f"{r['path']}.sigmf-data")
        if not data.exists():
            skipped.append((r["id"], "no .sigmf-data on disk"))
            continue
        st = data.stat()
        if now - st.st_mtime < QUIESCENT_S:
            skipped.append((r["id"], "file still growing — may be live"))
            continue
        if st.st_size == 0:
            skipped.append((r["id"], "empty capture"))
            continue
        plan.append((r, st.st_size // BYTES_PER_SAMPLE, st.st_mtime))

    for r, n_samples, ended in plan:
        meta_path = Path(f"{r['path']}.sigmf-meta")
        print(f"id={r['id']:6d} {r['band']:4s} "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r['started_utc']))} "
              f"-> ended={time.strftime('%H:%M:%S', time.gmtime(ended))} "
              f"({ended - r['started_utc']:6.1f}s) n_samples={n_samples} "
              f"meta={'write' if not meta_path.exists() else 'exists, skip'}")
    for cap_id, why in skipped:
        print(f"id={cap_id:6d} SKIP: {why}")

    if not args.apply:
        print(f"\n{len(plan)} row(s) would be closed. Re-run with --apply.")
        return 0

    for r, n_samples, ended in plan:
        meta_path = Path(f"{r['path']}.sigmf-meta")
        if not meta_path.exists():
            tmp = meta_path.with_suffix(".sigmf-meta.tmp")
            with open(tmp, "w") as fm:      # atomic: never a half-written sidecar
                json.dump(_meta(r, n_samples), fm, indent=1)
            os.replace(tmp, meta_path)
        db.execute(
            "UPDATE captures SET ended_utc=?, n_samples=? WHERE id=?"
            " AND ended_utc IS NULL",       # re-check: don't race a live worker
            (ended, n_samples, r["id"]))
    db.commit()
    print(f"\nclosed {len(plan)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
