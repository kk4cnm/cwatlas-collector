"""Inline decimation for the capture plane.

The Web-888 SND channel delivers 12 kHz complex samples no matter how narrow the
passband — for our 500 Hz CW window (750..1250 Hz) that's ~12-24x oversampled;
~96% of the stored rate is filter-suppressed noise with zero information. This
module shifts the passband to baseband and decimates 12 kHz -> 1.5 kHz (8x),
turning ~18 GB/night into ~2.3 GB/night (~1 TB/year) with nothing lost.

Chain (per capture file):
    mix by -750 Hz  ->  CW carrier +1000 Hz -> +250 Hz; passband -> 0..500 Hz
    FIR low-pass    ->  cutoff ~640 Hz (windowed sinc, 257 taps, unity passband)
    decimate by 8   ->  fs 1500 Hz, Nyquist +/-750 Hz comfortably holds passband

Nice property: 750 Hz at fs=12000 is EXACTLY 16 samples/cycle, so the mixer is a
16-entry lookup table — zero cumulative phase error over arbitrarily long files.
"""
from __future__ import annotations

import numpy as np

FS_IN = 12_000
DECIM = 8
FS_OUT = FS_IN // DECIM          # 1500 Hz
SHIFT_HZ = 750.0                 # CW carrier ends up at +250 Hz baseband
CARRIER_OUT_HZ = 250.0           # (tuned 1 kHz low) - 750 shift


class CwDecimator:
    """Streaming shift + low-pass + decimate for one capture file.

    Feed raw interleaved int16-LE I,Q bytes (as they come off the wire); get
    back the same format at fs/8. Keeps filter/mixer/decimator state across
    chunks, so chunk boundaries are seamless.
    """

    TAPS = 257

    def __init__(self) -> None:
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        cutoff = FS_OUT / 2 * 0.85            # ~637 Hz; transition into 750 Hz
        h = np.sinc(2 * cutoff / FS_IN * n) * np.hamming(self.TAPS)
        self._h = (h / h.sum()).astype(np.float32)   # unity DC/passband gain
        # 750/12000 = 1/16 cycle per sample -> exact 16-sample mixer table
        self._mix16 = np.exp(-2j * np.pi * np.arange(16) / 16).astype(np.complex64)
        self._n = 0                            # mixer position mod 16
        self._tail = np.zeros(self.TAPS - 1, dtype=np.complex64)
        self._doff = 0                         # decimator phase across chunks

    def process(self, iq_int16: bytes) -> bytes:
        a = np.frombuffer(iq_int16, dtype="<i2")
        if len(a) < 2:
            return b""
        x = a[0::2].astype(np.float32) + 1j * a[1::2].astype(np.float32)
        # mix down 750 Hz (table lookup, exact)
        idx = (self._n + np.arange(len(x))) & 15
        x = (x * self._mix16[idx]).astype(np.complex64)
        self._n = (self._n + len(x)) & 15
        # FIR with carried tail: output aligned to input timeline
        buf = np.concatenate([self._tail, x])
        y = np.convolve(buf, self._h, mode="valid")   # len == len(x)
        self._tail = buf[-(self.TAPS - 1):]
        # decimate with phase continuity
        sel = y[self._doff::DECIM]
        self._doff = (self._doff - len(y)) % DECIM
        out = np.empty(2 * len(sel), dtype="<i2")
        out[0::2] = np.clip(np.round(sel.real), -32768, 32767).astype("<i2")
        out[1::2] = np.clip(np.round(sel.imag), -32768, 32767).astype("<i2")
        return out.tobytes()
