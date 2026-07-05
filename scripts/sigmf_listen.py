#!/usr/bin/env python3
"""Render CWAtlas SigMF captures as audible WAV files for spot-checking.

The .sigmf-data files are raw complex int16 IQ at 1.5 kHz with the CW carrier
parked near +250 Hz baseband — media players can't guess that, so direct
playback is noise. This shifts the carrier to a comfortable sidetone pitch,
takes the real part, upsamples, and writes a standard 16-bit WAV.

Usage:
  sigmf_listen.py --top 20 [--band 40m] [--date 2026-07-04]   list best captures
  sigmf_listen.py --id 1902 [-o out.wav]                      convert by catalog id
  sigmf_listen.py <capture path> [-o out.wav]                 convert by path
                                                              (.sigmf-data/-meta/basename all accepted)

Options: --pitch HZ (default 600), --rate HZ (default 12000), -o/--out PATH.
Default output: <basename>.wav next to the capture.
"""
import argparse
import json
import math
import sqlite3
import sys
import wave
from pathlib import Path

import numpy as np

DATA_ROOT = Path("/mnt/md0/cwatlas/data")
CATALOG = DATA_ROOT / "catalog.db"


def load_capture(path_arg):
    """Resolve any of basename/.sigmf-data/.sigmf-meta to (meta dict, iq array)."""
    p = Path(path_arg)
    base = p.parent / p.name.removesuffix(".sigmf-data").removesuffix(".sigmf-meta")
    meta_path = base.with_name(base.name + ".sigmf-meta")
    data_path = base.with_name(base.name + ".sigmf-data")
    meta = json.loads(meta_path.read_text())
    dtype = meta["global"]["core:datatype"]
    if dtype != "ci16_le":
        sys.exit(f"unsupported core:datatype {dtype!r} (expected ci16_le)")
    raw = np.fromfile(data_path, dtype="<i2")
    iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    return meta, iq, base


def to_audio(meta, iq, pitch_hz, out_rate):
    sr = int(meta["global"]["core:sample_rate"])
    carrier = float(meta["global"].get("cwatlas:carrier_offset_hz", 250.0))
    factor = max(1, round(out_rate / sr))
    out_rate = sr * factor

    # FFT zero-pad upsample: exact for a band-limited signal, no scipy needed.
    n = len(iq)
    spec = np.fft.fftshift(np.fft.fft(iq))
    padded = np.zeros(n * factor, dtype=complex)
    padded[(n * factor - n) // 2 : (n * factor - n) // 2 + n] = spec
    up = np.fft.ifft(np.fft.ifftshift(padded)) * factor

    # Move the carrier from its baseband offset to the requested sidetone pitch.
    t = np.arange(len(up)) / out_rate
    audio = (up * np.exp(2j * math.pi * (pitch_hz - carrier) * t)).real

    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio * (0.9 / peak)
    return (audio * 32767).astype("<i2"), out_rate


def write_wav(path, pcm, rate):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def list_top(limit, band, date, min_conf):
    q = """SELECT id, band, printf('%.2f', freq_hz/1000.0), strength_db,
                  printf('%.2f', keyed_conf), n_samples/srate_hz, path
           FROM captures WHERE contaminated=0 AND ended_utc IS NOT NULL
                 AND keyed_conf >= ?"""
    args = [min_conf]
    if band:
        q += " AND band = ?"
        args.append(band)
    if date:
        q += " AND path LIKE ?"
        args.append(f"%/{date}/%")
    q += " ORDER BY strength_db DESC LIMIT ?"
    args.append(limit)
    rows = sqlite3.connect(CATALOG).execute(q, args).fetchall()
    print(f"{'id':>5}  {'band':<5} {'kHz':>9}  {'dB':>4}  {'conf':>4}  {'dur':>5}  path")
    for r in rows:
        print(f"{r[0]:>5}  {r[1]:<5} {r[2]:>9}  {r[3]:>4.0f}  {r[4]:>4}  {r[5]:>4}s  {r[6]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", nargs="?", help="capture basename or .sigmf-data/-meta path")
    ap.add_argument("--id", type=int, help="catalog id (looks up path in catalog.db)")
    ap.add_argument("--top", type=int, help="list N strongest clean captures and exit")
    ap.add_argument("--band", help="filter --top by band, e.g. 40m")
    ap.add_argument("--date", help="filter --top by day, e.g. 2026-07-04")
    ap.add_argument("--min-conf", type=float, default=0.0, help="filter --top by keyed_conf")
    ap.add_argument("--pitch", type=float, default=600.0, help="sidetone pitch in Hz")
    ap.add_argument("--rate", type=int, default=12000, help="output sample rate")
    ap.add_argument("-o", "--out", help="output wav path")
    args = ap.parse_args()

    if args.top:
        list_top(args.top, args.band, args.date, args.min_conf)
        return
    if args.id is not None:
        row = sqlite3.connect(CATALOG).execute(
            "SELECT path FROM captures WHERE id = ?", (args.id,)).fetchone()
        if not row:
            sys.exit(f"no capture with id {args.id}")
        args.path = row[0]
    if not args.path:
        ap.error("give a capture path, --id, or --top")

    meta, iq, base = load_capture(args.path)
    pcm, rate = to_audio(meta, iq, args.pitch, args.rate)
    out = Path(args.out) if args.out else base.with_name(base.name + ".wav")
    write_wav(out, pcm, rate)
    g = meta["global"]
    print(f"{out}  ({len(pcm)/rate:.0f}s @ {rate} Hz, {g['cwatlas:band']} "
          f"{meta['captures'][0]['core:frequency']/1000:.2f} kHz, "
          f"{g['cwatlas:strength_db']:.0f} dB, tone {args.pitch:.0f} Hz)")


if __name__ == "__main__":
    main()
