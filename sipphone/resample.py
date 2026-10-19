"""Small streaming resampler (windowed-sinc low-pass + linear interpolation).

Good enough for telephone-band (8 kHz) audio <-> sound card rate conversion.
"""
import numpy as np


class _Lowpass:
    def __init__(self, cutoff_hz, rate, taps=63):
        n = np.arange(taps) - (taps - 1) / 2.0
        fc = cutoff_hz / float(rate)
        h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
        self.h = (h / h.sum()).astype(np.float32)
        self.tail = np.zeros(taps - 1, dtype=np.float32)

    def process(self, x):
        buf = np.concatenate([self.tail, x])
        y = np.convolve(buf, self.h, mode='valid')
        self.tail = buf[-(len(self.h) - 1):].copy()
        return y.astype(np.float32)


class Resampler:
    """Streaming int16 -> int16 resampler between arbitrary rates."""

    def __init__(self, in_rate, out_rate):
        self.in_rate, self.out_rate = in_rate, out_rate
        self.step = in_rate / float(out_rate)
        self.pos = 0.0
        self.buf = np.zeros(1, dtype=np.float32)
        self.lp = None
        if in_rate != out_rate:
            lo = min(in_rate, out_rate)
            self.lp = _Lowpass(lo * 0.45, max(in_rate, out_rate) if out_rate > in_rate else in_rate)
        self._up = out_rate > in_rate

    def process(self, pcm16):
        x = pcm16.astype(np.float32)
        if self.in_rate == self.out_rate:
            return pcm16.astype(np.int16)
        if not self._up:                      # down: filter first, then decimate
            x = self.lp.process(x)
        buf = np.concatenate([self.buf, x])
        n_out = int(np.floor((len(buf) - 1 - self.pos) / self.step)) + 1 if len(buf) > 1 else 0
        if n_out <= 0:
            self.buf = buf
            return np.zeros(0, dtype=np.int16)
        t = self.pos + np.arange(n_out) * self.step
        y = np.interp(t, np.arange(len(buf)), buf).astype(np.float32)
        nxt = self.pos + n_out * self.step
        consumed = int(np.floor(nxt))
        self.buf = buf[consumed:]
        self.pos = nxt - consumed
        if self._up:                          # up: filter images afterwards
            y = self.lp.process(y)
        return np.clip(y, -32768, 32767).astype(np.int16)
