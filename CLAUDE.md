# Working on CWAtlas

Autonomous CW collector feeding **MorseBase**, a raw-IQ corpus. It runs 24/7 on
`airig-01` and has been collecting since 2026-07-01. Read `README.md` for what
it does, `DESIGN.md` for why, `docs/provenance.md` for how it records what it
was doing.

**This is a live instrument, not a project skeleton.** A bug here doesn't fail a
build; it silently costs IQ-hours that cannot be re-collected, or writes a
corpus nobody can interpret later. The design invariants in README.md
("Design invariants (don't break these)") are incident reports, not preferences.

## Verify by trying to break it, not by confirming it

**A verification that cannot fail proves nothing.** This has bitten this project
more than once, and it is the single easiest way to do real damage here while
reporting success.

Before claiming anything works:

1. **Name the falsifier first.** Say out loud what you would observe if it were
   broken. If you can't name that observation, you don't have a test — you have
   a ritual. Then confirm that observation was actually reachable.
2. **Query the end state independently; never trust the operation's exit code.**
   `git rm --cached` exits 0 and can still leave a file tracked. `git ls-files`
   is the answer. The command telling you it ran is not the system telling you
   it worked.
3. **Never arrange the world and then measure it.** If you regenerate an
   artifact and *then* commit it, a later "no diff" is a tautology. Test the
   path a real user takes, from the state a real user is in.
4. **Isolate the variable.** An unrelated dirty file, a stale env var, or your
   own uncommitted work will confound the signal and hand you a green light that
   means nothing.
5. **Prefer a test that has failed at least once.** A new test that passes on
   the first run has told you nothing yet — break the code, watch it go red,
   then fix it.

Both times this went wrong here, the evidence was one command away
(`git ls-files`, `git status`) and the failure was reported as a success. When
in doubt, spend the extra command.

## Traps this repo has already paid for

- **`executescript()` implicitly COMMITs** the pending transaction. Never use it
  inside a migration — DDL survives the rollback, the write lock releases early,
  and `ROLLBACK` then raises *"no transaction is active"* over the real
  exception. `migrations._M1_DDL` is a tuple of `execute()` calls for this reason.
- **`git commit -- <paths>` commits the WORKING TREE**, ignoring the index. It
  silently undoes a `git rm --cached`. (This is trap #2 above, in the wild.)
- **Roll back with `db.rollback()`, the method** — `execute("ROLLBACK")` raises
  when no transaction is open, masking the error you were trying to report.
- **An uncommitted UPDATE is visible to the connection that made it.** Proving
  durability needs a *second* connection, opened while the writer is still open.
- **Provenance must never stop collection.** Event writes are wrapped and can
  only print. State commits *before* its event, in a separate transaction —
  hygiene beats provenance.
- **An unrecorded fact is NULL, never a plausible reconstruction.** A guess that
  reads like an observation is worse than a gap.
- **A shipped migration is history.** Fix it forward with a new one; editing it
  makes the code lie about what production ran.

## Practicalities

- Live tree is `~/cwatlas/collector`. `/opt/CWAtlas` is the archived Phase-1
  FlexRadio prototype — read it for precedent, never deploy it.
- Site details (LAN addresses, antenna location) live in `config.toml`, which is
  **not tracked**. Never put them back in source; `config.example.toml` is the
  published shape.
- `python -m pytest tests/ -q` and `ruff check .` must both be clean. The suite
  must pass with **no** `config.toml` present — CI has none.
- Touching the live collector: back up `catalog.db` with `sqlite3 ... ".backup"`
  (never `cp` — a live WAL DB copied without its `-wal` is a torn snapshot),
  rehearse migrations on the copy, then `systemctl restart cwatlas-collector`.
  SIGTERM is graceful; in-flight rows finalize. Confirm afterwards that solar
  weighting is live and `/api/summary`'s `provenance.ok` is true.
