# Provenance

The corpus records **what** was captured in detail. Provenance records **the
conditions under which it was captured**, so that questions asked years from now
have answers instead of recollections:

> *Which band weights were in force when this sample was collected?*
> *Which detector thresholds triggered it?*
> *Why did this sample end up in Dataset 4?*

The rule this exists to serve: **nothing important should become impossible to
answer later.** Raw IQ is valuable; the history of the IQ is often just as
valuable, and unlike the IQ it cannot be reconstructed after the fact.

## Two structures, because there are two kinds of fact

| | `runs` | `capture_events` |
| --- | --- | --- |
| Grain | one row per collector process | one row per thing that happened to a capture |
| Rate | a few dozen a year | unbounded, forever |
| Answers | *what was running* | *what has been done to this since* |
| Mutable? | `ended_utc` written once at exit | never — enforced by trigger |

Version and config facts (firmware, git commit, detector thresholds, band
weights) are identical across thousands of consecutive captures and change only
when the process restarts. Lifecycle facts (reviewed, added to a dataset,
corrected) are per-capture and never stop accumulating. Putting both in one
event log means either duplicating a config blob 35,000 times or writing config
events from inside the capture path — see [Design notes](#design-notes).

```text
runs.id  <──  captures.run_id            "what was running when this was made"
             captures.id  <──  capture_events.capture_id
                                         "what has happened to it since"
             capture_events.run_id ──>  runs.id
                                         "which process wrote the event"
```

## `runs` — what was running

Written once at startup by `Catalog.begin_run()` from
`provenance.build_run_info()`; closed at clean shutdown by `end_run()`.

| Column | Notes |
| --- | --- |
| `kind` | `'collector'`, or `'synthetic'` for the pre-provenance backfill |
| `started_utc` / `ended_utc` | **`ended_utc IS NULL` means the process did not exit cleanly** — SIGKILL, OOM, power. That's a fact worth having, not a bug. |
| `git_commit` | 40-char HEAD, or NULL if not a checkout |
| `git_dirty` | 1 = the tracked source on disk differed from `git_commit` |
| `git_diff_sha256` | fingerprint of `git diff HEAD`; NULL unless dirty |
| `sdr_firmware` / `sdr_rx_chans` | from the device's MSG config at connect |
| `config_json` | the **effective, resolved** config, verbatim |
| `config_sha256` | grouping key over `config_json` |
| `dependencies_json` | installed versions of the runtime deps + the sqlite C library |
| `dependencies_sha256` | grouping key over `dependencies_json` |
| `note` | prose; carries the synthetic run's explanation |

`config_json` holds `scheduler` (including the hardware-derived
`n_rx_channels`), `detector` (defaults read from `detect_cw`'s signature, not
retyped), `solar.band_weights`, `dsp` (which determines the on-disk sample
format), `search_plan` (what was lookable-for), and the resolved CLI/env `args`.

**Why store it verbatim rather than hash it?** Because at a few dozen rows a
year you can afford the truth. `config_sha256` answers *"which captures ran
under the old weights?"*; `config_json` answers *"...and what were they?"* One
query gets both:

```sql
SELECT r.config_sha256,
       json_extract(r.config_json, '$.solar.band_weights.20m') AS weights_20m,
       COUNT(c.id) AS captures
FROM runs r JOIN captures c ON c.run_id = r.id
GROUP BY r.config_sha256;
```

**Why a git commit *and* a config hash?** They see different things. A config
hash cannot see an algorithm change — rewrite `detect_cw`'s max-hold logic and
every threshold hash is unchanged. A commit cannot see hardware-derived or
CLI-supplied values, which vary with no source change at all. Neither is a
substitute for the other, and a hand-bumped version string is a substitute for
neither: it lies the first time someone tunes a constant and forgets.

### Dependencies: intent vs. reality

A third fingerprint, because the first two share a blind spot: **same
`git_commit`, same `config_sha256`, different installed packages.** `pip install
-U numpy` changes the decimator's arithmetic — and therefore the IQ bytes on
disk — with both existing fingerprints identical.

`dependencies_json` records what was *installed*, not what pyproject *asked
for*. Declared requirements describe intent (`numpy>=1.26`); only the
environment knows reality (`2.5.0`). The distinction is not academic here: the
installed numpy is a major version past the declared floor, so the environment
crossed the 1.x→2.x boundary at some point no run row can name. `dsp.py` pins
its dtypes explicitly (`.astype(np.float32)`, `.astype(np.complex64)`) so it is
well defended against numpy 2.0's NEP 50 promotion changes — but that is a
property of today's code that nobody recorded, not a guarantee anyone can rely
on for the next upgrade.

Two rules keep the set honest and bounded:

- **The names are derived, never curated.** `_declared_runtime_packages()` reads
  the distribution's own requirements, so adding a dependency to pyproject puts
  it in provenance with nobody remembering to. A hand-listed set is maintainable
  state that goes stale — the same failure mode as retyping detector thresholds
  instead of reading the signature.
- **Direct requirements only, extras excluded.** `numpy` and `websockets` — the
  two that shape the corpus (decimator math; the IQ stream itself) — have *zero*
  runtime deps, so direct capture covers their whole surface. `mcp` drags 17 and
  is dormant in production under `--no-mcp`; hashing that tree would churn the
  fingerprint on code that never runs. `dev` (pytest/ruff) and `dash`
  (flask/otel) cannot touch the corpus.

The known gap: a *transitive* change (say `httpcore` under `httpx`) is invisible.
That's a deliberate trade — `httpx` only reads `/status`, and it doesn't touch
the IQ.

`sqlite3` is in there too, nested under its own key rather than mixed in with
`packages`, because it isn't a pip distribution and pretending otherwise is the
kind of small lie that costs somebody an hour in 2031. It's a real dependency —
`mark_window`'s `RETURNING` needs ≥ 3.35 — that no pip freeze would ever show and
that an OS upgrade moves silently.

```sql
SELECT json_extract(dependencies_json, '$.packages.numpy') AS numpy,
       json_extract(dependencies_json, '$.sqlite3')        AS sqlite,
       COUNT(c.id) AS captures
FROM runs r JOIN captures c ON c.run_id = r.id
GROUP BY r.dependencies_sha256;
```

## `capture_events` — what happened since

Append-only, enforced by `BEFORE UPDATE` / `BEFORE DELETE` triggers that
`RAISE(ABORT)`. A log whose entire purpose is immutability cannot rest on
everyone remembering not to `UPDATE` it. (This stops buggy code, not a
determined operator: `DROP TABLE` still works. That's the intended threat
model.)

| Column | Notes |
| --- | --- |
| `capture_id`, `ts` | what and when |
| `event_type` | `contaminated`, `finalize_recovered`, `reviewed`, `dataset_added`, `dataset_removed`, `published` |
| `actor` | `collector` \| `agent:<tool>` \| `human:<who>` \| `script:<name>` |
| `run_id` | which process wrote the event — the log gets provenance too |
| `details_json` | event-specific payload |

Written today:

| `event_type` | Written by | Why it exists |
| --- | --- | --- |
| `contaminated` | `capture.py` (PTT, `actor=collector`) and `mark_window` (`actor=agent:…`) | the flag is overwritten with no history or attribution |
| `finalize_recovered` | `scripts/backfill_orphans.py` | its `ended_utc`/`n_samples` are *inferred*, and the row otherwise looks observed |

`reviewed`, `dataset_added`, `dataset_removed` and `published` have no writers
yet — the schema is ready for them; add the emitter when a consumer exists.

### What earns an event, and what doesn't

The test is **not** "acquisition vs. lifecycle". It is:

> Does `captures` already hold this fact *immutably*? If so, no event. If the
> table overwrites it, the event is the only record there will ever be.

- `started_utc` is written once and never updated → **no `Captured` event.** It
  would duplicate authoritative state, and two independently-failing writes that
  must agree is exactly the orphaned-row pathology, re-introduced.
- `contaminated` is overwritten by `mark_contaminated` and bulk-overwritten by
  `mark_window` — no history, no attribution, no timestamp → **event.**
- `ended_utc`/`n_samples` are overwritten by `scripts/backfill_orphans.py` with
  values *inferred from file mtime and filesize*, producing a row that is
  identical in shape to an honestly-finalized one → **event.**

That last one is the sharpest argument for the log. Today the catalog cannot
distinguish an observed finalize from a reconstructed one.

## Invariants

- **Provenance must never stop collection.** Every event write is wrapped in
  `try/except sqlite3.Error` inside `Catalog` and can only print. `begin_run` is
  the sole exception: it runs at startup, writes the same DB `start_capture`
  needs, and fails loudly — because a collector that cannot write the catalog is
  not going to collect anything anyway.
- **Hygiene beats provenance.** The `contaminated` flag and its event are
  committed *separately*, flag first — never in one transaction. If the event
  write fails, rolling back would undo the flag, and that flag is what keeps
  dirty IQ out of the training set. A crash between the two loses an event and
  keeps the flag. That is the correct trade, always.

  This is a **transaction boundary, not statement order**. `UPDATE captures …;
  INSERT INTO capture_events …; commit()` reads flag-first and is wrong — it's
  one implicit transaction, and the event's failure takes the flag with it. The
  caller must `commit()` before calling `add_events()`. Proving it needs a
  *second connection*: an uncommitted UPDATE is visible to the connection that
  made it, so asserting on the writer proves only visibility, never durability.
  See `test_flag_is_durable_before_the_event_is_tried`.

- **Roll back with `rollback()`, never `execute("ROLLBACK")`.** If a write fails
  at prepare time (missing table), no transaction was ever opened, and the
  statement form raises *"cannot rollback - no transaction is active"* over the
  top of the real error. The method is a no-op when there's nothing to undo.
- **An unrecorded fact is NULL, never a reconstruction.** A guess that reads
  like an observation is worse than a gap, because a gap is honest.
- **`foreign_keys` stays OFF.** The `REFERENCES` clauses are documentation. With
  enforcement on, a bad `run_id` would fail every `start_capture` INSERT — a
  provenance bug taking collection down with it.

## Health signals

Provenance is only worth having if a break in it is visible, so
`/api/summary` carries a `provenance` panel (`cwatlas_dash.sources.provenance_health`):

| Signal | Healthy | Meaning |
| --- | --- | --- |
| `unstamped_captures` | **0** | a capture written with no declared run — provenance is silently broken |
| `captures_from_dirty_code` | **0** | IQ produced by code that exists nowhere in git: unreproducible by construction |
| `unclean_exits` | any | runs killed without unwinding (SIGKILL, OOM, power) — history, not an error |

`unstamped_captures` needs **no "since deployment" cutoff**, and that's a payoff
of refusing to leave `run_id IS NULL` behind: m1 adopted every pre-existing
capture, and `begin_run` stamps every new one, so a NULL anywhere in the corpus —
past or future — is a bug. Nothing in production inserts captures except
`start_capture` (`backfill_orphans` only UPDATEs).

`unclean_exits` needs no current-run parameter either: only one collector runs at
a time, so any run that isn't the newest and has no `ended_utc` was killed. The
newest is genuinely ambiguous (running vs. died) and the `service` panel already
resolves that.

`captures_from_dirty_code` is the one that matters most — and it's why
`cwatlas_collector.egg-info/` is untracked. It's regenerated by every
`pip install -e .`; tracked, it would set `git_dirty=1` on routine reinstalls and
this alarm would be noise inside a week.

## The pre-provenance era

Captures from **2026-07-01 20:34 UTC** (the first soak — before production, which
the README dates to 07-03) to the migration are adopted by a single
`kind='synthetic'` run whose version columns are all NULL and whose `note` says
why.

`run_id IS NULL` would have been ambiguous between *"collected before we
recorded this"* and *"the stamping is broken"*. `kind='synthetic'` says which,
and can be JOINed, so the answer is a sentence rather than a NULL.

Their versions are **not** reconstructed from git history. The window spans many
commits with no way to attribute a row to one, and a plausible fiction in a
provenance table is worse than an honest gap.

**Read its `started_utc`/`ended_utc` carefully.** For a `kind='collector'` row
they are one process's lifetime. For the synthetic row they bound observed
capture *activity* across many processes whose individual boundaries are
unrecorded — same columns, two meanings keyed on `kind`, which is why the row
spells it out in `note`. m1 originally set `ended_utc = MAX(started_utc)`, the
last capture's *start*; since a capture runs for up to `rotate_s` afterwards,
nine live rows ended outside their own run's envelope. m2 corrects it to
`MAX(COALESCE(ended_utc, started_utc))` (COALESCE because a row orphaned in
flight has no end, and its start is then the best honest bound). If a second
synthetic row ever appears — importing a historical corpus from another site —
that overload stops being cosmetic and the split into `observed_from_utc` /
`observed_through_utc` earns its keep. One row doesn't justify it yet.

## Schema evolution

`catalog.SCHEMA` is the **frozen v0 baseline** — never edit it. It builds
`captures` with `CREATE TABLE IF NOT EXISTS`, which can create a table but never
alter one, so every change from here is a migration in `migrations.py`, keyed on
`PRAGMA user_version`. A fresh DB runs `SCHEMA` (→ v0) then every migration; a
live DB runs only what it's missing. One code path, no drift.

Each migration runs inside one `BEGIN IMMEDIATE` together with its
`user_version` bump, so DDL, backfill and version land together or not at all — a
crash mid-migration leaves a clean v0, not a half-built schema. `BEGIN IMMEDIATE`
also takes the write lock *before* `user_version` is read, so two collectors
starting at once cannot both migrate.

> **Never use `executescript()` inside a migration.** It implicitly COMMITs the
> pending transaction, which silently breaks all of the above: the DDL survives a
> rollback, the write lock releases early, and the `ROLLBACK` in the error path
> raises *"no transaction is active"* over the top of the real exception.
> `_M1_DDL` is a tuple of statements for exactly this reason.

Migration 1 cost **52 ms** against the live 35,186-row catalog (`ALTER TABLE ADD
COLUMN` is metadata-only; the backfill `UPDATE` is ~9 ms).

**A migration that shipped is history — fix it forward, never edit it.** m1 wrote
a wrong `ended_utc` and m2 corrects it; m1 still contains the bug on purpose.
Editing m1 would make the code lie about what production actually executed,
which in a provenance feature is the one unaffordable kind of bug. A fresh DB
runs m1 then m2 and lands exactly where production did.

## Design notes

**Why not one table, as originally sketched?** Because a single
`capture_events` log cannot avoid touching `capture.py`. Emitting a `Captured`
event means writing it where the capture happens — in or beside the `finally:`
block that this project has already bled over (a disk mounted on `/mnt` shadowed
the data dir and orphaned 7 rows for 18.5 h; `24847b8` hardened it). The `runs`
design writes provenance once at process start, in `runtime.py`, before any
capture exists. **`capture.py` was not modified at all.** Duplicating a config
blob 35,000 times is merely wasteful; writing config from the capture path is
how you lose captures.

**Why doesn't `run_id` get threaded through the layers?** It doesn't need to.
`Catalog` is already the object passed from `runtime.py` → `Supervisor` →
`ChannelPool` → `channel_worker` → `start_capture`, so the run lives on the
instance as `self._run_id`. `start_capture`'s signature is unchanged. A `Catalog`
opened without `begin_run` (tests, scripts, the dash) leaves it `None` and
records NULL — correct, since no run was declared.

**Prior art:** the archived Phase-1 FlexRadio prototype
(`/opt/CWAtlas/cwatlas/storage/schema.sql`) already had a `sessions` table
(per-process anchor with host/config/start/end), `radios.firmware_version`,
`events.detector_version` and `captures.sha256`. The Web-888 rewrite dropped all
of it. This is a restoration.

**Not done, deliberately:** `PRAGMA foreign_keys=ON`; emitters for review /
dataset membership / publication (the schema is ready — write them when a
consumer exists); `captures.sha256` content hashing; transitive dependency
capture (see above); any general-purpose environment-capture framework.

The concept grew out of design discussions with ChatGPT ("Morgan") during the
early CWAtlas architecture work.
