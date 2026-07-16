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

**The table exists; nothing writes to it yet.** It was created with the `runs`
work so the schema is in place for review, labeling, and dataset membership when
consumers for those exist.

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
- **An unrecorded fact is NULL, never a reconstruction.** A guess that reads
  like an observation is worse than a gap, because a gap is honest.
- **`foreign_keys` stays OFF.** The `REFERENCES` clauses are documentation. With
  enforcement on, a bad `run_id` would fail every `start_capture` INSERT — a
  provenance bug taking collection down with it.

## The pre-provenance era

Captures from **2026-07-01 20:34 UTC to 2026-07-16 20:07 UTC** (35,186 rows)
predate this feature. They are adopted by a single `kind='synthetic'` run whose
version columns are all NULL and whose `note` says why.

`run_id IS NULL` would have been ambiguous between *"collected before we
recorded this"* and *"the stamping is broken"*. `kind='synthetic'` says which,
and can be JOINed, so the answer is a sentence rather than a NULL.

Their versions are **not** reconstructed from git history. The window spans many
commits with no way to attribute a row to one, and a plausible fiction in a
provenance table is worse than an honest gap.

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
consumer exists); `captures.sha256` content hashing; any general-purpose
provenance framework.

The concept grew out of design discussions with ChatGPT ("Morgan") during the
early CWAtlas architecture work.
