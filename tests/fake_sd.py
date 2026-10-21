"""Fake `sounddevice` for headless tests: real-time paced streams with a synthetic microphone."""
import sys
import threading
import time
import types

import numpy as np

DEVICES = [
    {'name': 'Test Mic', 'max_input_channels': 1, 'max_output_channels': 0, 'hostapi': 0,
     'default_samplerate': 48000.0},
    {'name': 'Test Speakers', 'max_input_channels': 0, 'max_output_channels': 2, 'hostapi': 0,
     'default_samplerate': 48000.0},
    {'name': 'Second Mic', 'max_input_channels': 2, 'max_output_channels': 0, 'hostapi': 0,
     'default_samplerate': 44100.0},
]
MIC_FREQ = {'Test Mic': 300.0, 'Second Mic': 700.0}


class _Stream:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.cb = kw.get('callback')
        self.samplerate = kw.get('samplerate', 48000)
        self.channels = kw.get('channels', 1)
        self.device = kw.get('device')
        self.dtype = kw.get('dtype', 'float32')
        self._run = False
        self._th = None
        self.written = []

    def start(self):
        if self._run:
            return
        self._run = True
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def stop(self):
        self._run = False
        if self._th:
            self._th.join(timeout=1)

    def close(self):
        self.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()

    def _loop(self):
        frames = int(self.samplerate * 0.01)
        n = 0
        name = DEVICES[self.device if isinstance(self.device, int) else 0]['name']
        freq = MIC_FREQ.get(name, 300.0)
        t0 = time.monotonic()
        while self._run:
            if self.kind == 'in':
                t = (np.arange(frames) + n) / float(self.samplerate)
                x = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
                data = np.repeat(x[:, None], self.channels, axis=1)
                if self.dtype == 'int16':
                    data = (data * 32767).astype(np.int16)
                self.cb(data, frames, None, None)
            else:
                out = np.zeros((frames, self.channels), dtype=np.int16 if self.dtype == 'int16' else np.float32)
                self.cb(out, frames, None, None)
                self.written.append(out[:, 0].copy())
            n += frames
            d = t0 + n / float(self.samplerate) - time.monotonic()
            if d > 0:
                time.sleep(d)


def install():
    sd = types.ModuleType('sounddevice')
    sd.query_hostapis = lambda: [{'name': 'MME'}]

    def query_devices(device=None, kind=None):
        if device is None:
            return list(DEVICES)
        return DEVICES[device]
    sd.query_devices = query_devices
    sd.default = types.SimpleNamespace(device=(0, 1))
    sd.InputStream = lambda **kw: _Stream('in', **kw)
    sd.OutputStream = lambda **kw: _Stream('out', **kw)
    sd.PortAudioError = Exception
    sys.modules['sounddevice'] = sd
    return sd
