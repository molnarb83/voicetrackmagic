"""Ring tone played on its own output device while lines are ringing."""
import numpy as np


class RingTone:
    """device=None means silent (no sound at all)."""

    def __init__(self, device, rate=48000):
        self.device, self.rate = device, rate
        self.stream, self.pos, self.buf = None, 0, None

    @property
    def enabled(self):
        return self.device is not None

    def start(self):
        if self.stream or self.device is None:
            return
        import sounddevice as sd
        t = np.arange(self.rate * 4) / float(self.rate)
        tone = 0.25 * (np.sin(2 * np.pi * 425 * t) + np.sin(2 * np.pi * 475 * t))
        env = ((t % 4.0) < 1.0).astype('float32')               # 1 s on, 3 s off
        self.buf = (tone * env * 12000).astype('int16')
        self.pos = 0

        def cb(out, frames, ti, status):
            n = len(self.buf)
            idx = (self.pos + np.arange(frames)) % n
            out[:, 0] = self.buf[idx]
            self.pos = (self.pos + frames) % n
        self.stream = sd.OutputStream(device=self.device, samplerate=self.rate, channels=1,
                                      dtype='int16', callback=cb)
        self.stream.start()

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
