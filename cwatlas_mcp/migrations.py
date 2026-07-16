"""Schema evolution for catalog.db, keyed on PRAGMA user_version.

catalog.SCHEMA is the v0 baseline and is FROZEN — it must never change again.
It builds `captures` with CREATE TABLE IF NOT EXISTS, which can create a table
but can never alter one, so every change from here is a migration. A fresh DB
runs SCHEMA (-> v0) then every migration in turn; a live DB runs only the
migrations it's missing. One code path, no drift between the two.

Each migration runs inside one BEGIN IMMEDIATE with the user_version bump, so
DDL + backfill + version land together or not at all: a crash mid-migration
leaves a clean v0 rather than a half-migrated DB with no way to tell. Sqlite is
happy to roll back DDL, so this works — but see _M1_DDL on why the statements
are a tuple and not one executescript() blob.
"""
from __future__ import annotations

import sqlite3

# Provenance: one `runs` row per collector process, `captures.run_id` pointing at
# it, and an append-only per-capture event log. See docs/provenance.md.
#
# run_id is deliberately nullable with no DEFAULT. `DEFAULT 1 REFERENCES runs(id)`
# would make the backfill O(1), but it is rejected outright once foreign_keys is
# ON ("Cannot add a REFERENCES column with non-NULL default value") and would
# silently stamp any future INSERT that omits run_id as the legacy run. The
# honest backfill costs ~10 ms.
#
# REFERENCES is documentation: foreign_keys stays OFF (see catalog.py), because
# with it ON a bad run_id would make every start_capture INSERT fail — turning a
# provenance bug into a capture outage. Provenance never stops collection.
#
# A tuple of statements, NOT one executescript() string: executescript implicitly
# COMMITs any pending transaction before it runs, which would end the migration's
# BEGIN IMMEDIATE early — leaving these tables committed even when the migration
# later fails and rolls back, releasing the write lock that serializes racing
# starts, and making the ROLLBACK in migrate() raise "no transaction is active"
# over the top of the real exception. execute() does none of that. (Splitting on
# ';' is not an option either — the triggers contain their own.)
_M1_DDL = (
    """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL DEFAULT 'collector',  -- 'collector' | 'synthetic'
    started_utc       REAL NOT NULL,
    ended_utc         REAL,          -- NULL = the process did not exit cleanly
    host              TEXT,
    pid               INTEGER,
    collector_version TEXT,
    git_commit        TEXT,          -- NULL = not a git checkout / git unavailable
    git_dirty         INTEGER,       -- 1 = tracked source differed from git_commit
    git_diff_sha256   TEXT,          -- NULL unless dirty; tells two dirty runs apart
    python_version    TEXT,
    sdr_host          TEXT,
    sdr_firmware      TEXT,
    sdr_rx_chans      INTEGER,       -- authoritative device count (this unit: 12)
    config_json       TEXT,          -- effective resolved config, verbatim
    config_sha256     TEXT,          -- grouping key over config_json
    note              TEXT
)
""",
    """
CREATE TABLE IF NOT EXISTS capture_events (
    id           INTEGER PRIMARY KEY,
    capture_id   INTEGER NOT NULL REFERENCES captures(id),
    ts           REAL NOT NULL,
    event_type   TEXT NOT NULL,   -- 'contaminated' | 'finalize_recovered' | 'reviewed'
                                  -- | 'dataset_added' | 'dataset_removed' | 'published'
    actor        TEXT NOT NULL,   -- 'collector' | 'agent:<tool>' | 'human:<who>'
                                  -- | 'script:<name>'
    run_id       INTEGER REFERENCES runs(id),  -- which process wrote the event
    details_json TEXT
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_capture_events_capture
    ON capture_events(capture_id, ts)
""",
    """
CREATE INDEX IF NOT EXISTS idx_capture_events_type_ts
    ON capture_events(event_type, ts)
""",
    # Append-only, enforced rather than merely intended: a log whose whole point
    # is immutability cannot rest on everyone remembering not to UPDATE it. This
    # stops buggy code, not a determined operator — DROP TABLE still works.
    """
CREATE TRIGGER IF NOT EXISTS capture_events_no_update
BEFORE UPDATE ON capture_events BEGIN
    SELECT RAISE(ABORT, 'capture_events is append-only');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS capture_events_no_delete
BEFORE DELETE ON capture_events BEGIN
    SELECT RAISE(ABORT, 'capture_events is append-only');
END
""",
)

_LEGACY_NOTE = (
    "Synthetic run covering every capture made before provenance existed. "
    "Collector/detector versions, receiver firmware and config were never "
    "recorded for these captures and cannot be recovered: the window spans many "
    "commits with no way to attribute a row to one. Every NULL in this row means "
    "UNRECORDED, not failed-to-record. Do not backfill it with a guess."
)

# m2 replaces _LEGACY_NOTE. For kind='collector', started_utc/ended_utc are one
# process's lifetime; for kind='synthetic' they are an envelope over captures
# many processes made. Same columns, two meanings keyed on kind — so the row has
# to say which out loud.
_LEGACY_NOTE_V2 = _LEGACY_NOTE + (
    " SEMANTICS: started_utc is the earliest observed capture start and "
    "ended_utc the latest observed capture end; together they bound observed "
    "capture ACTIVITY. Unlike a kind='collector' row, they do NOT represent the "
    "lifetime of a single historical collector process — many processes started "
    "and exited inside this window, and their boundaries are unrecorded."
)


def _m1_provenance(db: sqlite3.Connection) -> None:
    for stmt in _M1_DDL:
        db.execute(stmt)
    db.execute("ALTER TABLE captures ADD COLUMN run_id INTEGER REFERENCES runs(id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_captures_run ON captures(run_id)")

    # Adopt pre-provenance rows into an explicit synthetic run. Leaving them at
    # run_id IS NULL would be ambiguous between "collected before we recorded
    # this" and "the stamping is broken"; kind='synthetic' says which, and can be
    # JOINed so the answer is a sentence rather than a NULL.
    #
    # In Python because both natural SQL spellings break on sqlite 3.37: a bare
    # aggregate SELECT still returns one all-NULL row on an empty table (-> NOT
    # NULL constraint failed on a fresh install), and HAVING without GROUP BY is
    # rejected.
    #
    # NB the MAX(started_utc) below is WRONG as an envelope — it's the last
    # capture's START, so captures that were still recording end after it (9 of
    # them did, by up to 40 s, on the live corpus). _m2_synthetic_envelope fixes
    # it. Left as it ran on purpose: a migration is history, and rewriting this
    # one would make the code lie about what production actually executed. A
    # fresh DB runs m1 then m2 and lands in the same place.
    n, lo, hi = db.execute(
        "SELECT COUNT(*), MIN(started_utc), MAX(started_utc) FROM captures"
    ).fetchone()
    if n:
        cur = db.execute(
            "INSERT INTO runs (kind, started_utc, ended_utc, note)"
            " VALUES ('synthetic',?,?,?)", (lo, hi, _LEGACY_NOTE))
        db.execute("UPDATE captures SET run_id=? WHERE run_id IS NULL",
                   (cur.lastrowid,))


def _m2_synthetic_envelope(db: sqlite3.Connection) -> None:
    """Correct the synthetic run's ended_utc, and say what the bounds mean.

    m1 set ended_utc = MAX(started_utc), the latest capture START. A capture
    runs for up to rotate_s after it starts, so real captures end AFTER that
    bound: `WHERE t BETWEEN started_utc AND ended_utc` silently dropped them.
    The envelope is MAX(COALESCE(ended_utc, started_utc)) — COALESCE because a
    row orphaned in flight has no end, and its start is then the best honest
    bound we have for it.
    """
    for run_id, in db.execute("SELECT id FROM runs WHERE kind='synthetic'"):
        row = db.execute(
            "SELECT MIN(started_utc), MAX(COALESCE(ended_utc, started_utc))"
            " FROM captures WHERE run_id=?", (run_id,)).fetchone()
        if row[0] is None:      # a synthetic run adopting nothing shouldn't
            continue            # exist, but don't NULL its NOT NULL column
        db.execute("UPDATE runs SET started_utc=?, ended_utc=?, note=? WHERE id=?",
                   (row[0], row[1], _LEGACY_NOTE_V2, run_id))


MIGRATIONS = [_m1_provenance, _m2_synthetic_envelope]   # index i -> user_version i+1


def migrate(db: sqlite3.Connection) -> int:
    """Bring db up to len(MIGRATIONS). -> the version it's now at.

    BEGIN IMMEDIATE takes the write lock BEFORE user_version is read, so two
    collectors starting at once can't both decide they need to migrate.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            MIGRATIONS[i](db)
            # pragmas take no bind parameters; i is a loop index, not user input
            db.execute(f"PRAGMA user_version={i + 1}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return len(MIGRATIONS)
