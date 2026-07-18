# CWAtlas Session Journal — A Crash, a Recovery, and the Bug the Recovery Found

**Date:** 2026-07-18
**People:** Daniel (KK4CNM) + Claude (Opus 4.8)
**Follows:** `2026-07-16_provenance.md`

## 1. What happened

An NVIDIA driver / VRAM fault took the host down overnight. The collector had
been running since 2026-07-16 22:46 (run 6) and died with it. Daniel spent the
morning rebuilding the driver state; by the time we sat down the box was back,
both services were up, the GPU was idle and clean (2 MiB, no processes), and the
dashboard was showing sessions that were never going to end.

The crash timeline is worth recording because the two clocks disagree, and the
disagreement is the useful part:

```text
07:18:46  journald's last entry on boot -2
07:19:24  IQ .sigmf-data files stop growing
07:19:25  (last of them)
```

journald died 38 seconds before the capture workers did. The workers held open
fds to `/mnt/md0` and kept writing after logging was already gone — consistent
with a GPU hang taking the box down in stages rather than all at once. **File
mtime was the more truthful clock than the journal**, which is exactly why
`backfill_orphans.py` uses mtime for `ended_utc` and not the last log line.

## 2. The recovery

Nine capture rows were left with `ended_utc` NULL — killed between
`start_capture()` and `finalize_capture()`. The dash rendered them as
"capturing" indefinitely, and by morning `inflight()` had flagged all nine
`stale`. This is the failure mode `scripts/backfill_orphans.py` already exists
for (written after the 07-15 mount-shadow incident), so recovery was mechanical:

1. dry run — 9 rows, all ending 07:19:24–25, one clean cluster
2. back up the catalog → `catalog.pre-crashbackfill.20260718T113809Z.db`
3. `--apply`

All nine recovered `n_samples` from file size and `ended_utc` from mtime.
`smeter_avg` and `gps_start_sec` stay NULL — they lived in worker memory and are
gone. An honest gap beats a fabricated average.

### What we deliberately did NOT clean up

Run 6's own `ended_utc` is still NULL, and the dash still reports
`unclean_exits: 1`. That is not leftover mess — it is the record of the crash.
`catalog.py` says so directly:

> Leaving `ended_utc` NULL is meaningful — it says the process died without
> unwinding (SIGKILL, OOM, power).

Clearing it to make the dashboard look tidy would have deleted the only
structured evidence that last night happened. `sources.py` already classifies it
as "not an error, operationally interesting" and correctly keeps it out of `ok`.
The count is history; it should never go down.

**The general rule this session reinforced: a dashboard that looks wrong because
reality was wrong is working. Fix the reality or fix the rendering — never edit
the record to make the panel green.**

## 3. The bug the verification found

Verifying the backfill meant checking `n_samples` against actual file sizes.
Across all 3,171 of run 6's captures, 18 came back with **the file larger than
the catalog claimed** — consistently by ~900,000 samples, which is exactly one
600 s rotate at 1500 sps. None of them were rows the backfill had touched.

Widening the query: **172 path groups corpus-wide had two catalog rows pointing
at the same file**, spanning 2026-07-02 through 2026-07-18. Ongoing, not a
one-off, and nothing to do with the crash.

Every group had the same shape — a pair, identical `freq_hz` to full float
precision, same channel, sub-second apart:

```text
id   freq_hz             started              n_samples
984  18084016.1132813    15:06:23.844         192       <-- truncated
985  18084016.1132813    15:06:23.968         900032    <-- full dwell
```

### Root cause

The collector's own log had the whole sequence in one second:

```text
[capture ch5] ...T025122Z_ch5: 900096 samples (600.1s) ->rotate
[supervisor] ch5 max-dwell release 7047.49 kHz after 30 min
[supervisor] ch5 -> capture 7047.49 kHz (40m, 40 dB, keyed=1.00)
[capture ch5] ...T030122Z_ch5: 128 samples
```

Two independent defects, and it took both to cause damage:

**(a) The max-dwell cooldown was applied one tick too late.** `tick()` scores
`desired` *before* calling `_reconcile()`. Step 1 of `_reconcile` evicts a
max-dwell hog and stamps `cooldown_until` on its Detection — but `desired` was
already built, still contains that Detection, and step 2 assigns straight from
it. The cooldown filter in `_score_candidates` is correct and simply runs a tick
early. Net effect: the hog guard's stated intent — *"back into competition +
cooldown so the same signal doesn't instantly re-win the freed slot"* — was a
no-op for 16 days. The signal re-won the slot it had just been evicted from,
every time.

**(b) A filename collision silently truncated IQ.** Capture names are
second-granular (`{band}_{freq}kHz_{YYYYMMDDTHHMMSSZ}Z_ch{n}`). When (a) fired,
the worker rotated into a new file, took the release from its inbox after ~128
samples, finalized, then immediately picked up the re-assignment and started
*another* capture in the same second — same band, same frequency, same channel,
therefore the same name. `open(..., "wb")` truncated the first file and wrote
over it.

The result is 172 catalog rows pointing at a file whose contents belong to
their pair partner.

### How bad

Less bad than it sounds, but not nothing:

- 344 rows of 38,869 (**0.44%**)
- all 172 groups are exactly 2 rows — no 3-way collisions
- the truncated row averages **421 samples (~0.28 s)**, max 0.81 s

So the *lost IQ* is ~48 seconds of sub-second fragments across 16 days —
negligible for MorseBase. The real damage is catalog integrity: 172 rows claim
IQ they don't own, and any consumer resolving path → row hits an ambiguity.

## 4. The fix

Both defects patched, because each is independently wrong:

**Root cause** (`scheduler.py`) — re-check the cooldown at assignment time,
where it can see stamps applied earlier in the same tick:

```python
if time.time() < det.cooldown_until:
    continue
```

**Safety net** (`capture.py`) — a name collision must never be able to destroy
IQ, regardless of what the scheduler does. Uniquify with a `_2` suffix if the
target exists, and switch `"wb"` → `"xb"` so any residual collision raises
instead of silently overwriting. That costs one channel a 65 s backoff in
exchange for hearing about a bug that otherwise hides for 16 days. Worth it.

Regression tests in `tests/test_capture_collision.py`, both written to fail
against the unpatched code first:

- `test_same_second_recapture_does_not_truncate` — the exact 07-18 shape; asserts
  every row's file holds exactly the samples that row claims
- `test_distinct_seconds_keep_the_plain_name` — the uniquifier is collision-only
- `test_max_dwell_release_does_not_instantly_reassign` — the hog guard actually guards

Full suite: 93 passed.

## 5. Open question

The 172 historical pairs are still in the catalog. The truncated row in each
pair is a phantom — it points at its partner's IQ. Options are to leave them as
history, flag them via the event log, or delete them. Deleting catalog rows is
destructive and this is an instrument's record, so it's Daniel's call, not a
cleanup to do unilaterally. Deferred.

## 6. What this session was actually about

The bug was not found by looking for bugs. It was found because verifying a
routine backfill meant asking *what would prove this went wrong* — and the
answer, "a row whose `n_samples` disagrees with its file," happened to also be
the signature of an unrelated defect that had been running quietly since the
2nd.

The crash cost a few hours and nine rows. The verification that followed it
found a 16-day integrity bug. That trade is the entire argument for checking
your work against something falsifiable rather than against your own
expectations.
