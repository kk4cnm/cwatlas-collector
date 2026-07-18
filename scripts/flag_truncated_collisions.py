"""Flag catalog rows whose IQ file was overwritten by a same-second collision.

Until 2026-07-18 (commit e2c495f) two captures could land on one channel at one
frequency inside the same second — a max-dwell release that instantly re-won its
own slot, plus a second-granular filename. open(..., "wb") truncated the first
file and the second capture wrote over it. See
docs/sessions/2026-07-18_crash-recovery-and-collision.md.

The earlier row of each pair is a PHANTOM: its `path` resolves to IQ belonging
to its partner. The IQ it actually recorded (a few hundred samples) is gone.

These rows are FLAGGED, NOT DELETED. The corpus is an instrument's record and a
row that was really written really happened; deleting it would make the catalog
tidier and less true. The event log is the honest place to say "this row's file
is not its own" without editing what was recorded.

Nothing in `captures` is modified — this only appends to `capture_events`.
Consumers should exclude flagged rows:

    SELECT * FROM captures c WHERE NOT EXISTS (
      SELECT 1 FROM capture_events e
      WHERE e.capture_id = c.id AND e.event_type = 'truncated_by_collision')

    python -m scripts.flag_truncated_collisions            # report only
    python -m scripts.flag_truncated_collisions --apply
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cwatlas_mcp.catalog import Catalog  # noqa: E402

DATA_DIR = Path(os.environ.get("CWATLAS_DATA_DIR", "/mnt/md0/cwatlas/data"))
DB_PATH = DATA_DIR / "catalog.db"
EVENT_TYPE = "truncated_by_collision"
BYTES_PER_SAMPLE = 4       # ci16_le


def find_victims(db: sqlite3.Connection) -> list[dict]:
    """Rows whose file demonstrably holds another row's samples.

    Deliberately evidence-based rather than trusting the timestamps: a row is
    only a victim if the file size matches the PARTNER's n_samples and not its
    own. A pair that doesn't show that signature is left alone and reported.
    """
    rows = db.execute(
        "SELECT id, path, n_samples, started_utc FROM captures WHERE path IN"
        " (SELECT path FROM captures GROUP BY path HAVING COUNT(*) > 1)"
        " ORDER BY path, started_utc").fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["path"], []).append(r)

    victims, skipped = [], []
    for path, members in groups.items():
        try:
            on_disk = os.path.getsize(f"{path}.sigmf-data") // BYTES_PER_SAMPLE
        except OSError as exc:
            skipped.append((path, f"unreadable: {exc}"))
            continue
        owner = [m for m in members if m["n_samples"] == on_disk]
        orphaned = [m for m in members if m["n_samples"] != on_disk]
        if len(owner) != 1 or not orphaned:
            skipped.append((path, f"ambiguous: file={on_disk} "
                                  f"rows={[m['n_samples'] for m in members]}"))
            continue
        for m in orphaned:
            victims.append({"id": m["id"], "path": path,
                            "claimed": m["n_samples"], "on_disk": on_disk,
                            "owner_id": owner[0]["id"]})
    return victims, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write events (default: report only)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    victims, skipped = find_victims(db)

    already = {r[0] for r in db.execute(
        "SELECT capture_id FROM capture_events WHERE event_type=?",
        (EVENT_TYPE,)).fetchall()}
    fresh = [v for v in victims if v["id"] not in already]

    for v in fresh:
        print(f"id={v['id']:6d} claims {v['claimed']:>7d} samples; file holds "
              f"{v['on_disk']:>7d} (owner id={v['owner_id']}) "
              f"{Path(v['path']).name}")
    for path, why in skipped:
        print(f"SKIP {Path(path).name}: {why}")

    if already:
        print(f"\n{len(already)} row(s) already flagged.")
    if not fresh:
        print("Nothing to flag.")
        return 0
    if not args.apply:
        print(f"\n{len(fresh)} row(s) would be flagged. Re-run with --apply.")
        return 0

    db.close()
    cat = Catalog(args.db)
    n = 0
    for v in fresh:
        # one event per row so details carry that row's own evidence
        n += cat.add_events([v["id"]], EVENT_TYPE, "operator:collision-audit",
                            {"reason": "file overwritten by a same-second "
                                       "filename collision; see "
                                       "docs/sessions/2026-07-18_crash-"
                                       "recovery-and-collision.md",
                             "claimed_n_samples": v["claimed"],
                             "file_n_samples": v["on_disk"],
                             "file_owner_capture_id": v["owner_id"],
                             "fixed_in": "e2c495f"})
    print(f"\nflagged {n} row(s) as {EVENT_TYPE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
