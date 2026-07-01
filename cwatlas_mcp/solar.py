"""Solar-aware band weighting: HF propagation follows the sun.

Rather than hard sunrise/sunset gates, we compute solar elevation (NOAA
approximation, good to ~0.1 deg — far more than propagation needs) and map it to
per-band priority weights the supervisor multiplies into candidate scoring:

  * high bands (10/12/15/17m [, 6m]) are F2/E daylight paths -> weight up by day,
    down hard at night (nighttime "detections" up there are mostly local noise —
    this is the false-positive filter the weighting exists for);
  * 20m/30m run day and night -> near-neutral;
  * low bands (40/80/160m) are D-layer-absorbed by day, open after dark;
  * twilight (+/-8 deg) is gray-line enhancement -> low bands boosted extra.

Weights BIAS capture assignment; they never gate the scanner — every watering
hole is still swept, so a surprise opening still gets seen (just needs to beat
the weighted competition, which strong real signals do).
"""
from __future__ import annotations

import math
import time

DAY, GRAY, NIGHT = "day", "gray", "night"

#                 day   gray  night
BAND_WEIGHTS = {
    "160m": (0.2,  1.3,  1.3),
    "80m":  (0.3,  1.4,  1.4),
    "40m":  (0.6,  1.5,  1.5),
    "30m":  (1.0,  1.3,  1.2),
    "20m":  (1.2,  1.5,  1.0),
    "17m":  (1.4,  1.0,  0.5),
    "15m":  (1.5,  0.9,  0.4),
    "12m":  (1.5,  0.8,  0.3),
    "10m":  (1.5,  0.8,  0.3),
    "6m":   (1.3,  0.7,  0.2),
}


def solar_elevation_deg(lat_deg: float, lon_deg: float,
                        unix_ts: float | None = None) -> float:
    """Solar elevation angle (deg) at lat/lon, UTC-based NOAA approximation."""
    t = unix_ts if unix_ts is not None else time.time()
    days = t / 86400.0 - 10957.5          # days since J2000.0 epoch
    # mean longitude / anomaly (deg)
    L = (280.460 + 0.9856474 * days) % 360.0
    g = math.radians((357.528 + 0.9856003 * days) % 360.0)
    # ecliptic longitude -> declination & right ascension
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * days)
    decl = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    # local hour angle via Greenwich mean sidereal time
    gmst = (18.697374558 + 24.06570982441908 * days) % 24.0
    lmst = math.radians((gmst * 15.0 + lon_deg) % 360.0)
    ha = lmst - ra
    lat = math.radians(lat_deg)
    elev = math.asin(math.sin(lat) * math.sin(decl) +
                     math.cos(lat) * math.cos(decl) * math.cos(ha))
    return math.degrees(elev)


def sun_phase(elev_deg: float) -> str:
    if elev_deg > 8.0:
        return DAY
    if elev_deg < -8.0:
        return NIGHT
    return GRAY


def band_weights(lat_deg: float, lon_deg: float,
                 unix_ts: float | None = None) -> tuple[str, dict[str, float]]:
    """-> (phase, {band: weight}) for the supervisor's band_priority."""
    elev = solar_elevation_deg(lat_deg, lon_deg, unix_ts)
    phase = sun_phase(elev)
    col = {DAY: 0, GRAY: 1, NIGHT: 2}[phase]
    return phase, {band: w[col] for band, w in BAND_WEIGHTS.items()}
