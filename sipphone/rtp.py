"""RTP session: G.711 audio, RFC 2833 DTMF, tiny jitter buffer, symmetric RTP."""
import random
import socket
import struct
import threading
import time
from collections import deque

import numpy as np

from . import g711

FRAME = 160                 # samples per 20 ms @ 8 kHz


class RtpSession:
    def __init__(self, sock, log=None):
        self.sock = sock
        self.sock.settimeout(0.2)
        self.log = log or (lambda *a: None)
        self.remote = None              # (ip, port) where we send
        self.latched = False
        self.pt = g711.PCMA
        self.dtmf_pt = 101
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self._marker = True
        self._lock = threading.Lock()
        self._fifo = np.zeros(0, dtype=np.int16)
        self._started = False
        self._last_seq = None
        self._running = False
        self._thread = None
        self._dtmf_queue = deque()
        self._last_dtmf_ts = None
        self.rx_packets = 0
        self.tx_packets = 0
        self.on_dtmf = None

    @property
    def port(self):
        return self.sock.getsockname()[1]

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True, name='rtp-rx')
        self._thread.start()

    def close(self):
        self._running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def reset_receive(self):
        with self._lock:
            self._fifo = np.zeros(0, dtype=np.int16)
            self._started = False

    # ── receive ────────────────────────────────────────────────────────────
    def _recv_loop(self):
        while self._running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._on_packet(data, addr)
            except Exception as exc:     # never let a bad packet kill the thread
                self.log('rtp error: %r' % (exc,))

    def _on_packet(self, data, addr):
        if len(data) < 12 or (data[0] >> 6) != 2:
            return
        cc = data[0] & 0x0F
        has_ext = bool(data[0] & 0x10)
        pt = data[1] & 0x7F
        seq = struct.unpack('!H', data[2:4])[0]
        off = 12 + 4 * cc
        if has_ext and len(data) >= off + 4:
            off += 4 + 4 * struct.unpack('!H', data[off + 2:off + 4])[0]
        payload = data[off:]
        if data[0] & 0x20 and payload:          # padding
            payload = payload[:-payload[-1]]
        if not self.latched or self.remote != addr:
            if not self.latched:
                self.latched = True
                if self.remote != addr:
                    self.log('rtp: latching to %s:%d' % addr)
                    self.remote = addr
        self.rx_packets += 1
        if pt == self.dtmf_pt and len(payload) >= 4:
            self._on_dtmf_event(payload, struct.unpack('!I', data[4:8])[0])
            return
        if pt not in (g711.PCMA, g711.PCMU):
            return                              # comfort noise etc.
        with self._lock:
            if self._last_seq is not None:
                diff = (seq - self._last_seq) & 0xFFFF
                if diff == 0 or diff > 0x8000:  # duplicate or old
                    return
            self._last_seq = seq
            self._fifo = np.concatenate([self._fifo, g711.decode(payload, pt)])
            if len(self._fifo) > 1600:          # > 200 ms: drop oldest
                self._fifo = self._fifo[-640:]

    def _on_dtmf_event(self, payload, ts):
        event, flags = payload[0], payload[1]
        if flags & 0x80 and ts != self._last_dtmf_ts:       # end bit, once per digit
            self._last_dtmf_ts = ts
            digit = '0123456789*#ABCD'[event] if event < 16 else '?'
            if self.on_dtmf:
                self.on_dtmf(digit)

    def pop_samples(self, n=FRAME):
        """Next n decoded samples, or None when the buffer is (re)filling."""
        with self._lock:
            if not self._started:
                if len(self._fifo) >= 480:      # 60 ms prefill
                    self._started = True
                else:
                    return None
            if len(self._fifo) < n:
                self._started = False
                return None
            out, self._fifo = self._fifo[:n], self._fifo[n:]
            return out

    # ── send ───────────────────────────────────────────────────────────────
    def _send(self, pt, payload, marker=False, ts=None, advance=True):
        if not self.remote:
            return
        hdr = struct.pack('!BBHII', 0x80, (0x80 if marker else 0) | pt,
                          self.seq & 0xFFFF, (self.ts if ts is None else ts) & 0xFFFFFFFF, self.ssrc)
        try:
            self.sock.sendto(hdr + payload, self.remote)
            self.tx_packets += 1
        except OSError:
            pass
        self.seq = (self.seq + 1) & 0xFFFF

    def send_audio(self, pcm16):
        if self._dtmf_queue:
            ev = self._dtmf_queue.popleft()
            self._send(self.dtmf_pt, ev[0], marker=ev[1], ts=ev[2])
            self.ts = (self.ts + len(pcm16)) & 0xFFFFFFFF
            return
        self._send(self.pt, g711.encode(pcm16, self.pt), marker=self._marker)
        self._marker = False
        self.ts = (self.ts + len(pcm16)) & 0xFFFFFFFF

    def send_silence(self, frames=1):
        for _ in range(frames):
            self.send_audio(np.zeros(FRAME, dtype=np.int16))

    def queue_dtmf(self, digit, duration_ms=160):
        idx = '0123456789*#ABCD'.find(digit.upper())
        if idx < 0:
            raise ValueError('bad DTMF digit %r' % digit)
        start_ts = self.ts
        n = max(1, duration_ms // 20)
        for i in range(n):
            dur = (i + 1) * FRAME
            self._dtmf_queue.append((struct.pack('!BBH', idx, 10, dur), i == 0, start_ts))
        end = struct.pack('!BBH', idx, 0x80 | 10, n * FRAME)
        for _ in range(3):
            self._dtmf_queue.append((end, False, start_ts))
