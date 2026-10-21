"""CallAudio: connects the recorder's microphone stream and the sound card to the SIP media mixer.

  recorder mic callback --feed_mic()--> [mic fifo] --read_mic()--> SIP mixer --RTP--> caller
  caller --RTP--> SIP mixer --play()--> [speaker buffer] --> output device (you hear the caller)
                                    \\--> [recorder fifo] --get_remote()--> mixed into the recording

Everything between the bridge and the SIP engine is 8 kHz mono int16.
"""
import threading

import numpy as np

from .resample import Resampler
from .rtp import FRAME

RATE = 8000


class CallAudio:
    def __init__(self, in_device=None, out_device=None, use_devices=True):
        """in_device=None -> the caller hears the recording input (tapped from the recorder);
        otherwise a separate input device is opened just for the call."""
        self.custom_in = in_device
        self.out_device = out_device
        self.use_devices = use_devices
        self.active = False
        self._lock = threading.Lock()
        self._mic = np.zeros(0, dtype=np.int16)
        self._mic_rs, self._mic_sr = None, None
        self._spk = np.zeros(0, dtype=np.int16)
        self._spk_rs = None
        self._rec = np.zeros(0, dtype=np.int16)          # 8 kHz remote audio waiting for the recorder
        self._rec_buf = np.zeros(0, dtype=np.float32)     # resampled to the recorder rate
        self._rec_rs, self._rec_sr = None, None
        self._in_stream = self._out_stream = None
        self.out_rate = 48000

    # ── lifecycle ──────────────────────────────────────────────────────────
    def activate(self):
        if self.active:
            return
        with self._lock:
            self._mic = np.zeros(0, dtype=np.int16)
            self._spk = np.zeros(0, dtype=np.int16)
            self._rec = np.zeros(0, dtype=np.int16)
            self._rec_buf = np.zeros(0, dtype=np.float32)
            self._mic_rs = self._spk_rs = self._rec_rs = None
        if self.use_devices:
            import sounddevice as sd
            info = sd.query_devices(self.out_device if self.out_device is not None else sd.default.device[1])
            self.out_rate = int(info['default_samplerate'])
            self._spk_rs = Resampler(RATE, self.out_rate)
            self._out_stream = sd.OutputStream(device=self.out_device, samplerate=self.out_rate, channels=1,
                                               dtype='int16', callback=self._out_cb)
            self._out_stream.start()
            if self.custom_in is not None:
                iinfo = sd.query_devices(self.custom_in)
                sr = int(iinfo['default_samplerate'])
                self._in_stream = sd.InputStream(device=self.custom_in, samplerate=sr, channels=1,
                                                 dtype='float32', callback=self._in_cb)
                self._in_stream.start()
        self.active = True

    def deactivate(self):
        self.active = False
        for s in (self._in_stream, self._out_stream):
            try:
                if s:
                    s.stop()
                    s.close()
            except Exception:
                pass
        self._in_stream = self._out_stream = None

    @property
    def uses_recorder_mic(self):
        return self.custom_in is None

    # ── microphone side ────────────────────────────────────────────────────
    def feed_mic(self, block, sr):
        """Called from the recorder's input callback with a mono float32 block."""
        if not self.active or self.custom_in is not None:
            return
        self._push_mic(block, sr)

    def _in_cb(self, indata, frames, t, status):
        self._push_mic(indata[:, 0], int(self._in_stream.samplerate))

    def _push_mic(self, block, sr):
        pcm = (np.clip(np.asarray(block, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16)
        with self._lock:
            if self._mic_rs is None or self._mic_sr != sr:
                self._mic_rs, self._mic_sr = Resampler(sr, RATE), sr
            y = self._mic_rs.process(pcm)
            self._mic = np.concatenate([self._mic, y])
            if len(self._mic) > RATE:                       # > 1 s backlog -> keep the newest 60 ms
                self._mic = self._mic[-FRAME * 3:]

    def read_mic(self, n=FRAME):
        with self._lock:
            if len(self._mic) < n:
                return np.zeros(n, dtype=np.int16)
            out, self._mic = self._mic[:n], self._mic[n:]
            return out

    # ── speaker / recorder side ────────────────────────────────────────────
    def play(self, frame):
        if not self.active:
            return
        with self._lock:
            if self.use_devices and self._spk_rs is not None:
                self._spk = np.concatenate([self._spk, self._spk_rs.process(frame)])
                if len(self._spk) > self.out_rate:
                    self._spk = self._spk[-self.out_rate // 4:]
            self._rec = np.concatenate([self._rec, frame])
            if len(self._rec) > RATE * 2:
                self._rec = self._rec[-RATE // 2:]

    def _out_cb(self, outdata, frames, t, status):
        with self._lock:
            n = min(frames, len(self._spk))
            outdata[:n, 0] = self._spk[:n]
            outdata[n:, 0] = 0
            self._spk = self._spk[n:]

    def get_remote(self, n, sr):
        """Caller audio for the recorder: float32 array of exactly n samples at rate sr (or None)."""
        if not self.active:
            return None
        with self._lock:
            if self._rec_rs is None or self._rec_sr != sr:
                self._rec_rs, self._rec_sr = Resampler(RATE, sr), sr
                self._rec_buf = np.zeros(0, dtype=np.float32)
            if len(self._rec):
                y = self._rec_rs.process(self._rec).astype(np.float32) / 32768.0
                self._rec = np.zeros(0, dtype=np.int16)
                self._rec_buf = np.concatenate([self._rec_buf, y])
            if len(self._rec_buf) > sr // 2:                # lagging > 500 ms -> catch up
                self._rec_buf = self._rec_buf[-sr // 10:]
            out = self._rec_buf[:n]
            self._rec_buf = self._rec_buf[n:]
        if len(out) < n:
            out = np.concatenate([out, np.zeros(n - len(out), dtype=np.float32)])
        return out
