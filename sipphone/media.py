"""Audio backends, recorder and the 20 ms mixer that connects mic, call and file."""
import threading
import time
import wave
from collections import deque

import numpy as np

from .resample import Resampler
from .rtp import FRAME

RATE = 8000


class NullAudio:
    """No sound card: mic returns a configurable tone/silence, speaker is dropped."""

    def __init__(self, mic_freq=0.0, amplitude=8000):
        self.mic_freq = mic_freq
        self.amplitude = amplitude
        self._n = 0
        self.played = []

    def start(self):
        pass

    def stop(self):
        pass

    def read_mic(self, n=FRAME):
        if not self.mic_freq:
            return np.zeros(n, dtype=np.int16)
        t = (np.arange(n) + self._n) / float(RATE)
        self._n += n
        return (self.amplitude * np.sin(2 * np.pi * self.mic_freq * t)).astype(np.int16)

    def play(self, frame):
        self.played.append(frame)


class SoundDeviceAudio:
    """Real devices through sounddevice. Everything inside is 8 kHz mono int16."""

    def __init__(self, in_device=None, out_device=None, device_rate=48000):
        import sounddevice as sd
        self.sd = sd
        self.in_device, self.out_device, self.rate = in_device, out_device, device_rate
        self._mic = np.zeros(0, dtype=np.int16)
        self._mic_lock = threading.Lock()
        self._out = np.zeros(0, dtype=np.int16)
        self._out_lock = threading.Lock()
        self._down = Resampler(device_rate, RATE)
        self._up = Resampler(RATE, device_rate)
        self._in_stream = self._out_stream = None

    def start(self):
        sd = self.sd
        self._in_stream = sd.InputStream(device=self.in_device, samplerate=self.rate, channels=1,
                                         dtype='int16', callback=self._in_cb)
        self._out_stream = sd.OutputStream(device=self.out_device, samplerate=self.rate, channels=1,
                                           dtype='int16', callback=self._out_cb)
        self._in_stream.start()
        self._out_stream.start()

    def stop(self):
        for s in (self._in_stream, self._out_stream):
            try:
                if s:
                    s.stop()
                    s.close()
            except Exception:
                pass

    def _in_cb(self, indata, frames, t, status):
        y = self._down.process(indata[:, 0].copy())
        with self._mic_lock:
            self._mic = np.concatenate([self._mic, y])
            if len(self._mic) > RATE:               # > 1 s backlog: drop oldest
                self._mic = self._mic[-FRAME * 3:]

    def _out_cb(self, outdata, frames, t, status):
        with self._out_lock:
            n = min(frames, len(self._out))
            outdata[:n, 0] = self._out[:n]
            outdata[n:, 0] = 0
            self._out = self._out[n:]

    def read_mic(self, n=FRAME):
        with self._mic_lock:
            if len(self._mic) < n:
                return np.zeros(n, dtype=np.int16)
            out, self._mic = self._mic[:n], self._mic[n:]
            return out

    def play(self, frame):
        y = self._up.process(frame)
        with self._out_lock:
            self._out = np.concatenate([self._out, y])
            if len(self._out) > self.rate:
                self._out = self._out[-self.rate // 4:]


class WavRecorder:
    """Records mic + call audio.

    split=False (default): the mix goes to BOTH channels (dual mono).
    split=True           : left = you, right = caller.
    """

    def __init__(self, path, split=False):
        self.path, self.split = path, split
        self._wf = wave.open(path, 'wb')
        self._wf.setnchannels(2)
        self._wf.setsampwidth(2)
        self._wf.setframerate(RATE)
        self.frames = 0

    def write(self, mic, remote):
        if self.split:
            st = np.empty(len(mic) * 2, dtype=np.int16)
            st[0::2], st[1::2] = mic, remote
        else:
            mix = np.clip(mic.astype(np.int32) + remote.astype(np.int32), -32768, 32767).astype(np.int16)
            st = np.empty(len(mix) * 2, dtype=np.int16)
            st[0::2] = st[1::2] = mix
        self._wf.writeframes(st.tobytes())
        self.frames += len(mic)

    def close(self):
        try:
            self._wf.close()
        except Exception:
            pass


class Mixer(threading.Thread):
    """Every 20 ms: mic -> active call (RTP), call -> speaker, both -> recorder."""

    def __init__(self, audio, get_media_call, log=None):
        super().__init__(daemon=True, name='mixer')
        self.audio = audio
        self.get_media_call = get_media_call
        self.log = log or (lambda *a: None)
        self.recorder = None
        self._rec_lock = threading.Lock()
        self._running = False
        self.last_rms = (0.0, 0.0)     # (mic, remote) for level meters

    def start_recording(self, path, split=False):
        with self._rec_lock:
            if self.recorder:
                raise RuntimeError('already recording')
            self.recorder = WavRecorder(path, split)

    def stop_recording(self):
        with self._rec_lock:
            rec, self.recorder = self.recorder, None
        if rec:
            rec.close()
            return rec.path, rec.frames / float(RATE)
        return None, 0.0

    def stop(self):
        self._running = False
        self.join(timeout=1.5)
        self.stop_recording()

    def run(self):
        self._running = True
        nxt = time.monotonic()
        while self._running:
            nxt += FRAME / float(RATE)
            try:
                self._tick()
            except Exception as exc:
                self.log('mixer error: %r' % (exc,))
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:                      # fell far behind: resync
                nxt = time.monotonic()

    def _tick(self):
        mic = self.audio.read_mic(FRAME)
        remote = np.zeros(FRAME, dtype=np.int16)
        call = self.get_media_call()
        if call and call.rtp:
            call.rtp.send_audio(mic)
            got = call.rtp.pop_samples(FRAME)
            if got is not None:
                remote = got
        self.audio.play(remote)
        with self._rec_lock:
            if self.recorder:
                self.recorder.write(mic, remote)
        self.last_rms = (float(np.sqrt(np.mean(mic.astype(np.float32) ** 2))),
                         float(np.sqrt(np.mean(remote.astype(np.float32) ** 2))))
