"""Signalling + media integration tests against a local mock SIP provider (UDP, loopback)."""
import hashlib
import os
import socket
import struct
import sys
import threading
import time
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sipphone import g711                                            # noqa: E402
from sipphone.media import Mixer, NullAudio                          # noqa: E402
from sipphone.sipmsg import SipMessage, param, uri_of                # noqa: E402
from sipphone.ua import SipConfig, UserAgent                         # noqa: E402


def wait_for(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class ProviderRtp:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.rx = []
        self.ua_addr = None
        self.run = True
        self.seq, self.ts = 100, 5000
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.run:
            try:
                data, addr = self.sock.recvfrom(2000)
            except socket.timeout:
                continue
            except OSError:
                return
            self.ua_addr = addr
            self.rx.append((data[1] & 0x7F, data[12:], bool(data[1] & 0x80)))

    def send_tone(self, freq, seconds, pt=8):
        def run():
            t0 = time.monotonic()
            n = int(seconds * 50)
            for i in range(n):
                if not self.run or not self.ua_addr:
                    return
                t = (np.arange(160) + i * 160) / 8000.0
                pcm = (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
                hdr = struct.pack('!BBHII', 0x80, pt, self.seq & 0xFFFF, self.ts & 0xFFFFFFFF, 0xABCD)
                self.sock.sendto(hdr + g711.encode(pcm, pt), self.ua_addr)
                self.seq += 1
                self.ts += 160
                d = t0 + (i + 1) * 0.02 - time.monotonic()
                if d > 0:
                    time.sleep(d)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th

    def audio_payload(self, pt=8):
        return b''.join(p for q, p, m in self.rx if q == pt)

    def close(self):
        self.run = False
        self.sock.close()


class MockProvider:
    def __init__(self, user='user', password='secret', realm='mock.test'):
        self.user, self.password, self.realm = user, password, realm
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.ua_sip = None                 # (ip, port) of the UA (learned from REGISTER)
        self.rx = []                       # everything the UA sent us (SipMessage)
        self.run = True
        self.nonce = 'nonce123456'
        self.rtp = ProviderRtp()
        self.auth_ok = 0
        self.auth_bad = 0
        self.dialogs = {}                  # call-id -> dict
        self.auto_answer_out = True
        threading.Thread(target=self._loop, daemon=True).start()

    # ── helpers ────────────────────────────────────────────────────────────
    def send(self, msg, addr=None):
        self.sock.sendto(msg.to_bytes(), addr or self.ua_sip)

    def reply(self, req, code, reason, tag=None, body=None, extra=()):
        r = SipMessage()
        r.is_request = False
        r.status, r.reason = code, reason
        for v in req.get_all('via'):
            r.add('via', v)
        r.add('from', req.get('from'))
        to = req.get('to')
        if tag and param(to, 'tag') is None:
            to += ';tag=' + tag
        r.add('to', to)
        r.add('call-id', req.call_id)
        r.add('cseq', req.get('cseq'))
        r.add('contact', '<sip:mock@127.0.0.1:%d>' % self.port)
        for n, v in extra:
            r.add(n, v)
        if body:
            r.add('content-type', 'application/sdp')
            r.body = body.encode()
        return r

    def sdp(self, direction='sendrecv', pts=(8, 0)):
        names = {8: 'PCMA', 0: 'PCMU'}
        lines = ['v=0', 'o=- 1 1 IN IP4 127.0.0.1', 's=mock', 'c=IN IP4 127.0.0.1', 't=0 0',
                 'm=audio %d RTP/AVP %s 101' % (self.rtp.port, ' '.join(str(p) for p in pts))]
        lines += ['a=rtpmap:%d %s/8000' % (p, names[p]) for p in pts]
        lines += ['a=rtpmap:101 telephone-event/8000', 'a=%s' % direction]
        return '\r\n'.join(lines) + '\r\n'

    # ── server loop ────────────────────────────────────────────────────────
    def _loop(self):
        while self.run:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data.strip():
                continue
            m = SipMessage.parse(data)
            if m.is_request:
                self.ua_sip = self.ua_sip or addr
                self.rx.append(m)
                self._handle(m, addr)
            else:
                self.rx.append(m)          # responses from UA (180/200 to our INVITEs)

    def _check_auth(self, req):
        h = req.get('authorization')
        if not h:
            return False
        from sipphone.sipmsg import parse_challenge
        p = parse_challenge(h)
        ha1 = md5('%s:%s:%s' % (self.user, self.realm, self.password))
        ha2 = md5('%s:%s' % (req.method, p['uri']))
        if p.get('qop') == 'auth':
            exp = md5('%s:%s:%s:%s:%s:%s' % (ha1, self.nonce, p['nc'], p['cnonce'], 'auth', ha2))
        else:
            exp = md5('%s:%s:%s' % (ha1, self.nonce, ha2))
        ok = p.get('response') == exp and p.get('username') == self.user
        if ok:
            self.auth_ok += 1
        else:
            self.auth_bad += 1
        return ok

    def _challenge(self, req):
        return self.reply(req, 401, 'Unauthorized', tag='srv', extra=[(
            'www-authenticate', 'Digest realm="%s", nonce="%s", algorithm=MD5, qop="auth"' % (self.realm, self.nonce))])

    def _handle(self, m, addr):
        if m.method == 'REGISTER':
            if not self._check_auth(m):
                return self.send(self._challenge(m), addr)
            r = self.reply(m, 200, 'OK', tag='reg')
            r.headers = [(n, v) for n, v in r.headers if n != 'via']
            r.headers.insert(0, ('via', m.get('via') + ';received=127.0.0.1'))
            r.add('contact', m.get('contact'))
            return self.send(r, addr)
        if m.method == 'OPTIONS':
            return self.send(self.reply(m, 200, 'OK'), addr)
        in_dialog = param(m.get('to'), 'tag') is not None
        if m.method == 'INVITE' and in_dialog:                       # re-INVITE from the UA (hold/resume)
            d = (m.body or b'').decode()
            direction = 'recvonly' if 'a=sendonly' in d else 'sendrecv'
            return self.send(self.reply(m, 200, 'OK', body=self.sdp(direction, pts=(8,))), addr)
        if m.method == 'INVITE':                                     # outgoing call from the UA
            if not self._check_auth(m):
                return self.send(self._challenge(m), addr)
            self.send(self.reply(m, 180, 'Ringing', tag='out1'), addr)
            if self.auto_answer_out:
                self.send(self.reply(m, 200, 'OK', tag='out1', body=self.sdp()), addr)
            self.dialogs[m.call_id] = {'to_tag': 'out1', 'from': m.get('from'), 'to': m.get('to') + ';tag=out1',
                                       'cseq': 1, 'contact': uri_of(m.get('contact'))}
            return
        if m.method == 'BYE':
            return self.send(self.reply(m, 200, 'OK'), addr)
        if m.method == 'CANCEL':
            self.send(self.reply(m, 200, 'OK'), addr)
            inv = [x for x in self.rx if x.method == 'INVITE' and x.call_id == m.call_id][-1]
            return self.send(self.reply(inv, 487, 'Request Terminated', tag='out1'), addr)

    # ── provider-initiated actions ─────────────────────────────────────────
    def invite_ua(self, call_id, number='36301234567', name='Alice', sdp=True):
        ua = self.ua_sip
        m = SipMessage()
        m.method, m.uri = 'INVITE', 'sip:user@127.0.0.1:%d' % ua[1]
        m.add('via', 'SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bK%s;rport' % (self.port, call_id[:6]))
        m.add('max-forwards', '70')
        m.add('from', '"%s" <sip:%s@mock.test>;tag=prov%s' % (name, number, call_id[:4]))
        m.add('to', '<sip:user@mock.test>')
        m.add('call-id', call_id)
        m.add('cseq', '1 INVITE')
        m.add('contact', '<sip:%s@127.0.0.1:%d>' % (number, self.port))
        m.add('record-route', '<sip:127.0.0.1:%d;lr>' % self.port)
        if sdp:
            m.add('content-type', 'application/sdp')
            m.body = self.sdp().encode()
        self.dialogs[call_id] = {'from_tag': 'prov' + call_id[:4], 'number': number, 'inv': m}
        self.send(m)
        return m

    def responses(self, call_id, code=None):
        return [x for x in self.rx if not x.is_request and x.call_id == call_id and (code is None or x.status == code)]

    def requests(self, method, call_id=None):
        return [x for x in self.rx if x.is_request and x.method == method and (call_id is None or x.call_id == call_id)]

    def ack(self, call_id):
        d = self.dialogs[call_id]
        ok = self.responses(call_id, 200)[-1]
        a = SipMessage()
        a.method, a.uri = 'ACK', uri_of(ok.get('contact'))
        a.add('via', 'SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKack%s' % (self.port, call_id[:5]))
        a.add('max-forwards', '70')
        a.add('from', d['inv'].get('from'))
        a.add('to', ok.get('to'))
        a.add('call-id', call_id)
        a.add('cseq', '1 ACK')
        self.send(a)

    def in_dialog_request(self, call_id, method, body=None, cseq=2):
        d = self.dialogs[call_id]
        ok = self.responses(call_id, 200)[-1]
        m = SipMessage()
        m.method, m.uri = method, uri_of(ok.get('contact'))
        m.add('via', 'SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bK%s%d' % (self.port, method.lower(), cseq))
        m.add('max-forwards', '70')
        m.add('from', d['inv'].get('from'))
        m.add('to', ok.get('to'))
        m.add('call-id', call_id)
        m.add('cseq', '%d %s' % (cseq, method))
        if body:
            m.add('contact', '<sip:mock@127.0.0.1:%d>' % self.port)
            m.add('content-type', 'application/sdp')
            m.body = body.encode()
        self.send(m)
        return m

    def close(self):
        self.run = False
        self.rtp.close()
        self.sock.close()


class Harness:
    def __init__(self, mic_freq=300.0, password='secret', **cfgkw):
        self.prov = MockProvider()
        self.events = []
        cfg = SipConfig('user', password, 'mock.test', proxy='127.0.0.1:%d' % self.prov.port,
                        rtp_port_min=30000, rtp_port_max=30400, **cfgkw)
        self.ua = UserAgent(cfg, lambda n, c, i: self.events.append((n, c, i)))
        self.audio = NullAudio(mic_freq=mic_freq)
        self.mixer = Mixer(self.audio, self.ua.media_call)

    def start(self):
        self.ua.start()
        self.mixer.start()
        assert wait_for(lambda: self.ua.registered), 'registration failed: %r' % self.events
        return self

    def stop(self):
        self.mixer.stop()
        self.ua.stop()
        self.prov.close()

    def ev(self, name):
        return [e for e in self.events if e[0] == name]


def spectrum_peaks(x, rate=8000):
    x = x.astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)

    def band(f):
        return spec[(freqs > f - 40) & (freqs < f + 40)].max()
    return band, spec.max()


def test_registration_digest_and_wrong_password():
    h = Harness().start()
    try:
        assert h.prov.auth_ok >= 1 and h.prov.auth_bad == 0
    finally:
        h.stop()
    prov = MockProvider()
    events = []
    cfg = SipConfig('user', 'WRONG', 'mock.test', proxy='127.0.0.1:%d' % prov.port)
    ua = UserAgent(cfg, lambda n, c, i: events.append((n, i)))
    ua.start()
    try:
        assert wait_for(lambda: any(n == 'registration_failed' for n, _ in events))
        assert not ua.registered
    finally:
        ua.stop()
        prov.close()


def test_incoming_call_media_recording_hold_dtmf_hangup(tmp_path):
    h = Harness(mic_freq=300.0).start()
    p, ua = h.prov, h.ua
    try:
        cid = 'call-a-0001@mock'
        p.invite_ua(cid, '36301234567', 'Alice')
        assert wait_for(lambda: p.responses(cid, 180)), 'no 180'
        assert p.responses(cid, 100)
        inc = h.ev('incoming')[0][1]
        assert inc.number == '36301234567' and inc.name == 'Alice' and inc.line == 1
        assert ua.answer(1)
        ok = wait_for(lambda: p.responses(cid, 200))
        assert ok, 'no 200'
        body = ok[-1].body.decode()
        assert 'PCMA' in body and 'a=sendrecv' in body
        p.ack(cid)
        wav = str(tmp_path / 'rec.wav')
        h.mixer.start_recording(wav)
        p.rtp.send_tone(1000, 1.6).join()
        time.sleep(0.3)
        path, secs = h.mixer.stop_recording()
        assert secs > 1.0, secs
        # what the provider received from us = our mic (300 Hz) as G.711
        rx = g711.decode(p.rtp.audio_payload(8), 8)
        assert len(rx) > 1.0 * 8000
        band, peak = spectrum_peaks(rx[4000:12000])
        assert band(300) > 0.5 * peak and band(1000) < 0.1 * peak
        # recording: dual-mono mix of 300 Hz (us) + 1000 Hz (caller)
        with wave.open(wav) as w:
            assert w.getnchannels() == 2 and w.getframerate() == 8000
            st = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, 2)
        assert np.array_equal(st[:, 0], st[:, 1])
        seg = st[8000:8000 + 6400, 0]
        band, peak = spectrum_peaks(seg)
        assert band(300) > 0.2 * peak and band(1000) > 0.2 * peak, (band(300), band(1000), peak)
        # DTMF
        assert ua.send_dtmf(1, '5')
        ev = wait_for(lambda: [x for x in p.rtp.rx if x[0] == 101 and x[1][1] & 0x80])
        assert ev and ev[0][1][0] == 5
        # hold / resume
        assert ua.hold(1)
        assert wait_for(lambda: [r for r in p.requests('INVITE', cid) if b'a=sendonly' in r.body])
        assert wait_for(lambda: p.requests('ACK', cid) and len(p.requests('ACK', cid)) >= 1)
        assert ua.status()[0]['state'].startswith('held')
        assert ua.media_call() is None
        assert ua.resume(1)
        assert wait_for(lambda: [r for r in p.requests('INVITE', cid) if b'a=sendrecv' in r.body])
        assert wait_for(lambda: ua.media_call() is not None)
        # remote hang-up
        p.in_dialog_request(cid, 'BYE')
        assert wait_for(lambda: h.ev('ended'))
        assert ua.status() == []
    finally:
        h.stop()


def test_two_calls_ring_together_switch_holds_previous_and_cancel():
    h = Harness().start()
    p, ua = h.prov, h.ua
    try:
        a, b, c = 'call-a-0002@mock', 'call-b-0002@mock', 'call-c-0002@mock'
        p.invite_ua(a, '111', 'A')
        p.invite_ua(b, '222', 'B')
        p.invite_ua(c, '333', 'C')
        assert wait_for(lambda: p.responses(a, 180) and p.responses(b, 180) and p.responses(c, 180))
        lines = {x['who']: x['line'] for x in ua.status()}
        assert sorted(lines.values()) == [1, 2, 3] and all(s['state'] == 'ringing_in' for s in ua.status())
        # caller C gives up while ringing
        cancel = SipMessage()
        cancel.method, cancel.uri = 'CANCEL', 'sip:user@127.0.0.1'
        inv = p.dialogs[c]['inv']
        for n in ('via', 'from', 'to', 'call-id'):
            cancel.add(n, inv.get(n))
        cancel.add('cseq', '1 CANCEL')
        p.send(cancel)
        assert wait_for(lambda: p.responses(c, 487)), 'UA did not send 487'
        # answer A, then switch to B: A must be put on hold automatically
        assert ua.answer(1)
        assert wait_for(lambda: p.responses(a, 200))
        p.ack(a)
        assert ua.resume(2)
        assert wait_for(lambda: p.responses(b, 200))
        assert wait_for(lambda: [r for r in p.requests('INVITE', a) if b'a=sendonly' in r.body]), 'A not held'
        st = {s['line']: s for s in ua.status()}
        assert st[2]['active'] and not st[1]['active'] and st[1]['state'].startswith('held')
        # switch back to A: B held, A resumed
        assert ua.resume(1)
        assert wait_for(lambda: [r for r in p.requests('INVITE', a) if b'a=sendrecv' in r.body])
        assert wait_for(lambda: [r for r in p.requests('INVITE', b) if b'a=sendonly' in r.body])
        # hangup mode: switching ends the previous call
        ua.cfg.switch_mode = 'hangup'
        assert ua.resume(2)
        assert wait_for(lambda: p.requests('BYE', a)), 'A not hung up'
        assert [s['line'] for s in ua.status()] == [2]
    finally:
        h.stop()


def test_reject_and_busy_limit():
    h = Harness(max_calls=2).start()
    p, ua = h.prov, h.ua
    try:
        ids = ['busy-%d@mock' % i for i in range(3)]
        for i in ids:
            p.invite_ua(i, '40' + i[-6:-5])
        assert wait_for(lambda: p.responses(ids[0], 180) and p.responses(ids[1], 180))
        assert wait_for(lambda: p.responses(ids[2], 486)), 'third call must get 486'
        assert ua.reject(1)
        assert wait_for(lambda: p.responses(ids[0], 486))
    finally:
        h.stop()


def test_invite_without_sdp_delayed_offer():
    h = Harness().start()
    p, ua = h.prov, h.ua
    try:
        cid = 'delayed-0001@mock'
        p.invite_ua(cid, sdp=False)
        assert wait_for(lambda: p.responses(cid, 180))
        assert ua.answer(1)
        ok = wait_for(lambda: p.responses(cid, 200))
        assert ok and 'PCMA' in ok[-1].body.decode() and 'PCMU' in ok[-1].body.decode()
        d = p.dialogs[cid]
        ack = SipMessage()
        ack.method, ack.uri = 'ACK', uri_of(ok[-1].get('contact'))
        ack.add('via', 'SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKd1' % p.port)
        ack.add('from', d['inv'].get('from'))
        ack.add('to', ok[-1].get('to'))
        ack.add('call-id', cid)
        ack.add('cseq', '1 ACK')
        ack.add('content-type', 'application/sdp')
        ack.body = p.sdp(pts=(0,)).encode()
        p.send(ack)
        assert wait_for(lambda: ua.calls[cid].pt == 0), 'codec from ACK not applied'
    finally:
        h.stop()


def test_outgoing_call_with_auth_then_remote_bye():
    h = Harness().start()
    p, ua = h.prov, h.ua
    try:
        call = ua.dial('3612345678')
        assert wait_for(lambda: call.state == 'connected'), (call.state, call.end_reason)
        invs = p.requests('INVITE', call.call_id)
        assert len(invs) == 2 and invs[1].get('authorization'), 'second INVITE must carry digest'
        assert invs[1].cseq[0] == 2
        assert wait_for(lambda: p.requests('ACK', call.call_id))
        assert ua.media_call() is call
        assert wait_for(lambda: len(p.rtp.audio_payload(8)) > 1600), 'no RTP from UA'
        ua.hangup(call)
        assert wait_for(lambda: p.requests('BYE', call.call_id))
    finally:
        h.stop()


def test_outgoing_call_cancel_while_ringing():
    h = Harness().start()
    h.prov.auto_answer_out = False
    p, ua = h.prov, h.ua
    try:
        call = ua.dial('3699999999')
        assert wait_for(lambda: call.state == 'ringing_out')
        ua.hangup(call)
        assert wait_for(lambda: p.requests('CANCEL', call.call_id))
        assert ua.status() == []
    finally:
        h.stop()


def test_split_recording_channels(tmp_path):
    from sipphone.media import WavRecorder
    rec = WavRecorder(str(tmp_path / 's.wav'), split=True)
    mic = np.full(160, 1000, dtype=np.int16)
    rem = np.full(160, -2000, dtype=np.int16)
    rec.write(mic, rem)
    rec.close()
    with wave.open(str(tmp_path / 's.wav')) as w:
        st = np.frombuffer(w.readframes(160), dtype=np.int16).reshape(-1, 2)
    assert (st[:, 0] == 1000).all() and (st[:, 1] == -2000).all()
