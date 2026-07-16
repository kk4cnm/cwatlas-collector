# CWAtlas Session Journal — Provenance, and What the Corpus Became

**Date:** 2026-07-16
**People:** Daniel (KK4CNM) + Claude (Opus 4.8)
**Follows:** `2026-07-03_m3-m4-and-migration.md`

## 1. The corpus stopped being a test

The dashboard this morning: **~35,186 captures, ~3,001 IQ-hours, ~81 GB**,
roughly 9 simultaneous capture channels, over 1,300 IQ-hours in the last week
alone. Two weeks ago this was a soak test. It isn't anymore — it's data
acquisition, and the collector has become an instrument that happens to be built
out of SDR software.

That reframing is what prompted everything below. A test harness doesn't need to
know what version of itself produced a file. An instrument does.

## 2. What the architecture became

The original concept was, roughly: *listen to lots of CW and save IQ.* What
exists now has a clean separation of responsibilities:

```text
Search plane  ->  Activity Map  ->  Deterministic Supervisor  ->  Capture Plane  ->  MorseBase
```

with the LLM deliberately **outside** the critical path. The README's line —
*the system must survive the agent being absent, slow, or wrong* — is the whole
philosophy in one sentence. The AI isn't flying the airplane. It's advising the
autopilot, and the autopilot is a few hundred lines of deterministic Python that
has never once needed the agent to be awake.

The scar tissue is visible and load-bearing. "Never churn connections." "Catalog
rows must always be closed." "Signals must earn a channel." "MCP is control
plane only." None of those are theoretical; each has an incident behind it, and
the design invariants section of the README is really an incident log wearing a
nicer shirt.

## 3. The gap

For all that metadata, the corpus could not answer a basic question: **what was
running when this was captured?**

Not recorded, anywhere, for any of the 35,186 rows:

- which collector code produced it — no git hash at runtime; `__version__ =
  "0.0.1"` had *zero call sites*
- which detector thresholds fired — source literals, unrecorded
- which band weights biased the assignment — `BAND_WEIGHTS` is a hardcoded dict,
  and **changing it silently changes what gets collected**
- which scheduler constants applied — the scar-tissue values have been retuned in
  response to live incidents, and rows from before and after are indistinguishable
- what firmware the receiver ran — *fetched at startup, printed, discarded*

That last one stung. The information was in a local variable, eight lines above
the catalog, and we threw it away every time.

The uncomfortable part: this is a **restoration**, not an invention. The archived
Phase-1 FlexRadio prototype had a `sessions` table, `radios.firmware_version`,
`events.detector_version`, and `captures.sha256`. The Web-888 rewrite dropped all
of it and nobody noticed for six weeks, because nothing breaks when you stop
recording history. It just quietly becomes unanswerable.

## 4. Provenance as an append-only log

The sketch was one table — `capture_events(capture_id, ts, event_type, actor,
details_json)` — carrying everything from `Captured` to `Added to Dataset v2.1`.
The instinct was right; the shape needed one change.

It became **two** structures, because there are two kinds of fact:

- **`runs`** — one row per collector process: firmware, git commit + dirty flag,
  Python, and the *effective resolved config* as verbatim JSON. Written once at
  startup. `captures.run_id` points at it.
- **`capture_events`** — append-only, per-capture, unbounded: review, corrections,
  dataset membership. Trigger-enforced immutability.

The deciding argument wasn't cardinality (duplicating a config blob 35k times is
merely wasteful). It was that **the one-table design cannot avoid touching
`capture.py`** — emitting a `Captured` event means writing it in or beside the
`finally:` block that orphaned 7 rows for 18.5 h in July. The `runs` design
writes provenance once, in `runtime.py`, before any capture exists. `capture.py`
diff: **zero lines**. That decided it.

Corollary: there is no `Captured` event. The right test isn't *acquisition vs.
lifecycle*, it's **does `captures` already hold this immutably?** `started_utc`
does, so an event would just be a second copy that can disagree with the first —
the orphan pathology in a new hat. But `contaminated` gets overwritten with no
history, and `ended_utc` gets overwritten by `backfill_orphans.py` with values
*inferred from file mtime and filesize* — producing a row identical in shape to
an honestly-finalized one. **The catalog currently cannot tell an observed
finalize from a reconstructed one.** That's what the log is for.

## 5. Things that only turn up once you look

**The dirty flag was almost born useless.** The plan called for cleaning five
stray `*.bak` files so `git_dirty` would mean something. On inspection they were
stranger than that: staged as *added*, already deleted from disk, never
committed. The worktree matched HEAD exactly — `git describe --dirty` said
**clean** while `git status --porcelain` listed five entries. Same repo, two
answers. So the flag's definition mattered more than the cleanup: `git_dirty` now
means **`git diff HEAD` is non-empty** — the tracked source on disk differs from
the commit, i.e. the code that ran is not the code at `git_commit`. Index state
and untracked files are excluded, because neither changes what executed, and a
flag that trips on an untracked scratch file is a flag you learn to ignore. The
`.bak` entries were left alone; they were never the problem.

**`executescript()` would have silently broken the migration.** The plan said
DDL + backfill + `user_version` land atomically in one `BEGIN IMMEDIATE`. They
wouldn't have: Python's `executescript` implicitly COMMITs the pending
transaction first. Verified in about ninety seconds — the tables survived a
rollback, the write lock released early, and the `ROLLBACK` in the error path
raised *"cannot rollback - no transaction is active"* **over the top of the real
exception**. The DDL is a tuple of `execute()` calls now, and
`test_failed_migration_rolls_back_to_clean_v0` guards it. A design can be right
and still be wrong about the one library call that implements it.

**The corpus starts before production does, and that's correct.** Corpus
`MIN(started_utc)` is 2026-07-01 20:34 UTC, while the README says production
since 07-03 — which first read like a two-day error in the README. It isn't:
07-01 and 07-02 hold 336 and 1,491 captures from the first soak, and "in
production since 07-03" is a status claim, not a claim about the earliest row.
Both are true. The lesson is narrower and more useful than a doc bug: the
synthetic run computes its span from `MIN/MAX(started_utc)` rather than from any
date a human wrote down, and it covers the soak captures too — because they're in
the corpus, and provenance describes what's there, not what was meant to be
there.

## 6. What landed

`migrations.py` (a `user_version` mechanism the project didn't have — the old
`CREATE TABLE IF NOT EXISTS` could create but never alter), `provenance.py`,
`runs` + `capture_events` + `captures.run_id`, and one reordering in
`runtime.py`. 28 existing tests unchanged and passing; 18 new.

Migration against the live catalog: **52 ms** for 35,186 rows. All adopted by
the synthetic run, zero left NULL. The dashboard returns byte-identical results
across all four windows — it opens a fresh `mode=ro` connection per request and
names every column, so it never noticed.

The pre-provenance era keeps its honest gap. Its versions are **not**
reconstructed from git log: the window spans many commits with no way to
attribute a row to one, and a plausible fiction in a provenance table is worse
than a NULL. `kind='synthetic'` plus a `note` explaining that every NULL means
*unrecorded, not failed-to-record*.

## 7. Where this points

MorseBase is turning into something richer than a database of recordings: a
**time-indexed observation of HF activity**. The interesting queries stop being
audio queries and start being scientific ones — *find weak European CW during
grayline*, *stations with severe QSB but no adjacent interference*. The collector
already produces most of the metadata those need.

Most ML datasets start as a pile of data, and researchers spend months trying to
infer the context afterward. This one is doing the opposite: signal, RF
environment, timestamp, frequency, SNR, detector confidence, contamination state,
dwell history, solar weighting, hardware state — all captured *at acquisition
time*, when they're free, rather than reconstructed later, when they're gone.

The dataset isn't just audio. It's contextualized from the moment it's born. That
opens doors well past a Morse decoder: propagation studies, signal-quality
analysis, weak-signal detection research.

Provenance is the part that keeps that promise honest. It's the same reason Git
stores history rather than just files, the same reason a lab notebook is dated
and signed, and the same reason a museum specimen without its collection label is
a curiosity rather than a data point.

The provenance concept grew out of design discussions with ChatGPT ("Morgan")
during the early CWAtlas architecture work; it's recorded here because the
*reason* for a design outlives the design.

**Next:** the event log has a schema and no writers. The first ones should be
contamination attribution (`server.py` already accepts a `reason` for
agent-reported contamination and `scheduler.py` **silently drops it** — an agent
says why it flagged 200 captures and the system throws the why away), and
`backfill_orphans.py` marking reconstructed finalizes as reconstructed.
