"""Search-plane CW detector: turn one dwell's worth of W/F frames into Detections.

Strategy (hw-validated on 2026-07-01 against live 20m CW):
  * max-hold across frames finds keyed signals that per-frame averaging dilutes;
  * per-bin ON/OFF behavior across frames separates keyed CW from steady carriers:
    a bin that is sometimes well above the floor and sometimes at it, with several
    ON/OFF transitions, is being keyed.

At wf_speed=4 each frame is ~250 ms, so individual dits aren't resolved — but CW
elements/words gate whole frames often enough that a 10 s dwell (~40 frames) shows
clear ON/OFF alternation. This is a *candidate* detector: precision comes later
from the capture-side envelope, which sees the real 12 kHz IQ.
"""
from __future__ import annotations

import statistics

from .models import Detection
from .sdr_client import WfFrame


def detect_cw(
    frames: list[WfFrame], band: str,
    min_snr_db: float = 10.0, on_snr_db: float = 6.0,
) -> list[Detection]:
    """Analyze one dwell (all frames share start_hz/bin_hz). -> Detections."""
    if len(frames) < 8:
        return []  # not enough time context to judge keying
    nbins = len(frames[0].bins)
    start_hz, bin_hz = frames[0].start_hz, frames[0].bin_hz

    # per-frame noise floor (median) -> per-bin max-hold SNR
    floors = [statistics.median(f.bins) for f in frames]
    mx = [max(f.bins[k] - floors[i] for i, f in enumerate(frames))
          for k in range(nbins)]

    dets: list[Detection] = []
    k = 0
    while k < nbins:
        if mx[k] <= min_snr_db:
            k += 1
            continue
        j = k
        while j < nbins and mx[j] > min_snr_db:
            j += 1
        peak = max(range(k, j), key=lambda m: mx[m])

        # keying signature on the peak bin: ON = above floor + on_snr_db
        on = [frames[i].bins[peak] - floors[i] > on_snr_db
              for i in range(len(frames))]
        on_frac = sum(on) / len(on)
        transitions = sum(1 for a, b in zip(on, on[1:]) if a != b)
        if on_frac >= 0.98:
            keyed = 0.1        # steady carrier — not CW (or key-down tune-up)
        elif on_frac <= 0.05:
            keyed = 0.0        # one-frame blip — noise/static crash
        else:
            # intermittent + several transitions ≈ keyed. Scale by transition count.
            keyed = min(1.0, transitions / 6) * (0.5 + 0.5 * min(on_frac / 0.5, 1.0))

        dets.append(Detection(
            freq_hz=start_hz + (peak + 0.5) * bin_hz,
            band=band,
            strength_db=mx[peak],
            keyed_confidence=keyed,
        ))
        k = j
    return dets
