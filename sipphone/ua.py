"""Minimal SIP user agent: registration, several simultaneous incoming calls,
outgoing calls, hold/resume, DTMF, G.711 over UDP.

Own implementation (no GPL dependency). Scope is deliberately small:
UDP transport, digest auth, PCMA/PCMU, one proxy (the provider).
"""
import heapq
import itertools
import random
import socket
import threading
import time

from . import g711
from .rtp import RtpSession
from .sipmsg import (SipMessage, digest_response, display_name_of, new_branch, new_call_id,
                     new_tag, param, parse_challenge, parse_uri, uri_of)

T1 = 0.5
IDLE, RINGING_IN, DIALING, RINGING_OUT, CONNECTED, ENDED = (
    'idle', 'ringing_in', 'dialing', 'ringing_out', 'connected', 'ended')
CODEC_NAMES = {g711.PCMU: 'PCMU', g711.PCMA: 'PCMA'}


class SipConfig:
    def __init__(self, user, password, domain, **kw):
        self.user, self.password, self.domain = user, password, domain
        self.auth_user = kw.get('auth_user') or user
        self.display_name = kw.get('display_name', '')
        self.proxy = kw.get('proxy') or domain          # host or host:port
        self.port = int(kw.get('port', 5060))
        self.local_port = int(kw.get('local_port', 0))
        self.expires = int(kw.get('expires', 300))
        self.rtp_port_min = int(kw.get('rtp_port_min', 10000))
        self.rtp_port_max = int(kw.get('rtp_port_max', 10200))
        self.max_calls = int(kw.get('max_calls', 10))
        self.switch_mode = kw.get('switch_mode', 'hold')   # hold | hangup
        self.keepalive = int(kw.get('keepalive', 25))
        self.user_agent = kw.get('user_agent', 'VoiceTrackMagic-SIP/0.1')


class Call:
    def __init__(self, ua, direction):
        self.ua, self.direction = ua, direction
        self.state = IDLE
        self.line = None
        self.call_id = None
        self.local_tag = new_tag()
        self.local_hdr = self.remote_hdr = None
        self.local_cseq = 1
        self.remote_cseq = 0
        self.remote_target = None
        self.route_set = []
        self.peer_addr = None
        self.rtp = None
        self.invite = None             # UAS: received INVITE; UAC: our sent INVITE
        self.invite_branch = None
        self.invite_uri = None
        self.last_response = None
        self.ack_received = False
        self.delayed_offer = False
        self.remote_offer = None
        self.pt = None
        self.dtmf_pt = 101
        self.remote_dir = 'sendrecv'
        self.local_held = False
        self.created = time.time()
        self.connected_at = None
        self.end_reason = ''
        self.number = ''
        self.name = ''
        self.sdp_id = random.randint(100000, 999999)
        self.sdp_ver = random.randint(1000, 9999)
        self.auth_tried = False
        self.last_reinvite_resp = None

    @property
    def label(self):
        return '%s <%s>' % (self.name, self.number) if self.name else self.number

    @property
    def remote_held(self):
        return self.remote_dir in ('sendonly', 'inactive')

    def snapshot(self):
        st = self.state
        if st == CONNECTED:
            st = 'held (by us)' if self.local_held else ('held (by remote)' if self.remote_held else 'connected')
        return {'line': self.line, 'state': st, 'who': self.label, 'dir': self.direction,
                'secs': int(time.time() - (self.connected_at or self.created)),
                'number': self.number, 'name': self.name, 'ringing': self.state == RINGING_IN,
                'call_id': self.call_id}


class UserAgent:
    def __init__(self, cfg, on_event=None, log=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda *a: None)
        self.log = log or (lambda *a: None)
        self.lock = threading.RLock()
        self.calls = {}                      # call-id -> Call
        self.active_id = None
        self.registered = False
        self.public_ip = None
        self.public_port = None
        self._txns = {}                      # (branch, method) -> dict
        self._timers = []
        self._timer_seq = itertools.count()
        self._timer_cv = threading.Condition()
        self._running = False
        self._reg_call_id = None
        self._reg_cseq = 0
        self._reg_tries = 0
        self._contact_fixed = False
        self.sock = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        host, _, p = self.cfg.proxy.partition(':')
        port = int(p) if p else self.cfg.port
        self.server_addr = (socket.gethostbyname(host), port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', self.cfg.local_port))
        self.sock.settimeout(0.25)
        self.local_port = self.sock.getsockname()[1]
        self.local_ip = self._detect_local_ip()
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True, name='sip-rx').start()
        threading.Thread(target=self._timer_loop, daemon=True, name='sip-timers').start()
        with self.lock:
            self.register()
        self._schedule(self.cfg.keepalive, self._keepalive)

    def stop(self):
        for call in list(self.calls.values()):
            if call.state != ENDED:
                try:
                    self.hangup(call)
                except Exception:
                    pass
        if self.registered:
            try:
                self.register(expires=0)
                time.sleep(0.3)
            except Exception:
                pass
        self._running = False
        with self._timer_cv:
            self._timer_cv.notify_all()
        time.sleep(0.3)
        try:
            self.sock.close()
        except Exception:
            pass

    def _detect_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(self.server_addr)
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'
        finally:
            s.close()

    # ── timers ─────────────────────────────────────────────────────────────
    def _schedule(self, delay, fn, *args):
        with self._timer_cv:
            heapq.heappush(self._timers, (time.monotonic() + delay, next(self._timer_seq), fn, args))
            self._timer_cv.notify()

    def _timer_loop(self):
        while self._running:
            with self._timer_cv:
                if not self._timers:
                    self._timer_cv.wait(0.5)
                    continue
                due, _, fn, args = self._timers[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._timer_cv.wait(min(wait, 0.5))
                    continue
                heapq.heappop(self._timers)
            try:
                fn(*args)
            except Exception as exc:
                self.log('timer error: %r' % (exc,))

    def _keepalive(self):
        if not self._running:
            return
        try:
            self.sock.sendto(b'\r\n\r\n', self.server_addr)
        except OSError:
            pass
        self._schedule(self.cfg.keepalive, self._keepalive)

    # ── transport ──────────────────────────────────────────────────────────
    def _sendto(self, data, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError as exc:
            self.log('send error: %r' % (exc,))

    def _recv_loop(self):
        while self._running:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.strip():
                continue
            try:
                msg = SipMessage.parse(data)
            except Exception as exc:
                self.log('bad SIP datagram from %s: %r' % (addr, exc))
                continue
            try:
                with self.lock:
                    if msg.is_request:
                        self._on_request(msg, addr)
                    else:
                        self._on_response(msg, addr)
            except Exception as exc:
                import traceback
                self.log('handler error: %r\n%s' % (exc, traceback.format_exc()))

    def _send_request(self, msg, addr, handler, timeout_handler=None):
        branch = param(msg.get('via'), 'branch')
        key = (branch, msg.method)
        txn = {'data': msg.to_bytes(), 'addr': addr, 'handler': handler, 'tries': 0,
               'interval': T1, 'start': time.monotonic(), 'done': False,
               'timeout': timeout_handler, 'invite': msg.method == 'INVITE'}
        self._txns[key] = txn
        self._sendto(txn['data'], addr)
        self._schedule(txn['interval'], self._retransmit, key)

    def _retransmit(self, key):
        with self.lock:
            txn = self._txns.get(key)
            if not txn or txn['done']:
                return
            if time.monotonic() - txn['start'] > 32:
                self._txns.pop(key, None)
                if txn['timeout']:
                    txn['timeout']()
                return
            txn['tries'] += 1
            txn['interval'] = min(txn['interval'] * 2, 4.0)
            self._sendto(txn['data'], txn['addr'])
            self._schedule(txn['interval'], self._retransmit, key)

    # ── message builders ───────────────────────────────────────────────────
    def _contact_hdr(self):
        ip = self.public_ip if (self._contact_fixed and self.public_ip) else self.local_ip
        port = self.public_port if (self._contact_fixed and self.public_port) else self.local_port
        return '<sip:%s@%s:%d>' % (self.cfg.user, ip, port)

    def _via(self, branch):
        return 'SIP/2.0/UDP %s:%d;branch=%s;rport' % (self.local_ip, self.local_port, branch)

    def _base_request(self, method, uri, branch=None):
        m = SipMessage()
        m.method, m.uri = method, uri
        m.add('via', self._via(branch or new_branch()))
        m.add('max-forwards', '70')
        return m

    def _response(self, req, code, reason, addr, to_tag=None, contact=False, body=None, extra=()):
        r = SipMessage()
        r.is_request = False
        r.status, r.reason = code, reason
        vias = req.get_all('via')
        for i, v in enumerate(vias):
            if i == 0:
                host = v.split(';')[0].split()[-1].split(':')[0]
                if param(v, 'rport') == '':
                    v = v.replace(';rport', ';rport=%d' % addr[1], 1)
                if host != addr[0]:
                    v += ';received=%s' % addr[0]
            r.add('via', v)
        for v in req.get_all('record-route'):
            r.add('record-route', v)
        r.add('from', req.get('from'))
        to = req.get('to')
        if to_tag and param(to, 'tag') is None:
            to += ';tag=' + to_tag
        r.add('to', to)
        r.add('call-id', req.call_id)
        r.add('cseq', req.get('cseq'))
        if contact:
            r.add('contact', self._contact_hdr())
        r.add('user-agent', self.cfg.user_agent)
        for n, v in extra:
            r.add(n, v)
        if body:
            r.add('content-type', 'application/sdp')
            r.body = body.encode() if isinstance(body, str) else body
        return r

    def _sdp(self, call, direction='sendrecv', only_pt=None):
        ip = self.public_ip or self.local_ip
        pts = [only_pt] if only_pt is not None else [g711.PCMA, g711.PCMU]
        lines = ['v=0', 'o=- %d %d IN IP4 %s' % (call.sdp_id, call.sdp_ver, ip), 's=VoiceTrackMagic',
                 'c=IN IP4 %s' % ip, 't=0 0',
                 'm=audio %d RTP/AVP %s %d' % (call.rtp.port, ' '.join(str(p) for p in pts), call.dtmf_pt)]
        for p in pts:
            lines.append('a=rtpmap:%d %s/8000' % (p, CODEC_NAMES[p]))
        lines += ['a=rtpmap:%d telephone-event/8000' % call.dtmf_pt, 'a=fmtp:%d 0-16' % call.dtmf_pt,
                  'a=ptime:20', 'a=%s' % direction]
        return '\r\n'.join(lines) + '\r\n'

    @staticmethod
    def parse_sdp(body):
        """-> dict(ip, port, pts, dtmf_pt, direction) or None."""
        if not body:
            return None
        text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else body
        ip, port, pts, dtmf, names = None, None, [], None, {}
        direction, in_audio, session_ip = 'sendrecv', False, None
        for ln in text.replace('\r', '').split('\n'):
            if ln.startswith('c=IN IP4 '):
                val = ln[9:].strip().split('/')[0]
                if in_audio:
                    ip = val
                else:
                    session_ip = val
            elif ln.startswith('m='):
                parts = ln[2:].split()
                if parts[0] == 'audio' and port is None:
                    in_audio = True
                    port = int(parts[1])
                    pts = [int(p) for p in parts[3:] if p.isdigit()]
                else:
                    in_audio = False
            elif ln.startswith('a=rtpmap:') and in_audio:
                num, _, enc = ln[9:].partition(' ')
                names[int(num)] = enc.split('/')[0].upper()
            elif ln.startswith('a=') and in_audio and ln[2:] in ('sendrecv', 'sendonly', 'recvonly', 'inactive'):
                direction = ln[2:]
        if port is None:
            return None
        ip = ip or session_ip
        if ip == '0.0.0.0' and direction == 'sendrecv':
            direction = 'sendonly'                       # RFC 2543 style hold
        for p, n in names.items():
            if n == 'TELEPHONE-EVENT':
                dtmf = p
        return {'ip': ip, 'port': port, 'pts': pts, 'names': names, 'dtmf_pt': dtmf, 'direction': direction}

    @staticmethod
    def _pick_codec(sdp):
        for p in sdp['pts']:
            name = sdp['names'].get(p, CODEC_NAMES.get(p))
            if p in (g711.PCMA, g711.PCMU) and (name in (None, 'PCMA', 'PCMU')):
                return p
        return None

    # ── helpers on calls ───────────────────────────────────────────────────
    def _free_line(self):
        used = {c.line for c in self.calls.values() if c.state != ENDED}
        n = 1
        while n in used:
            n += 1
        return n

    def get_call(self, ref):
        """ref = line number or Call object."""
        if isinstance(ref, Call):
            return ref
        for c in self.calls.values():
            if c.line == ref and c.state != ENDED:
                return c
        return None

    def live_calls(self):
        return sorted((c for c in self.calls.values() if c.state != ENDED), key=lambda c: c.line or 0)

    def _open_rtp(self, call):
        for _ in range(200):
            port = random.randrange(self.cfg.rtp_port_min, self.cfg.rtp_port_max, 2)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(('0.0.0.0', port))
            except OSError:
                s.close()
                continue
            call.rtp = RtpSession(s, self.log)
            call.rtp.on_dtmf = lambda d, c=call: self._event('dtmf', c, d)
            return
        raise RuntimeError('no free RTP port')

    def _apply_remote_sdp(self, call, sdp):
        if not sdp:
            return False
        pt = self._pick_codec(sdp)
        if pt is None:
            return False
        call.pt = pt
        call.remote_dir = sdp['direction']
        if sdp['dtmf_pt']:
            call.dtmf_pt = sdp['dtmf_pt']
        if call.rtp:
            call.rtp.pt = pt
            call.rtp.dtmf_pt = call.dtmf_pt
            if sdp['ip'] and sdp['ip'] != '0.0.0.0' and not call.rtp.latched:
                call.rtp.remote = (sdp['ip'], sdp['port'])
        return True

    def _end_call(self, call, reason=''):
        if call.state == ENDED:
            return
        call.state = ENDED
        call.end_reason = reason
        if self.active_id == call.call_id:
            self.active_id = None
        if call.rtp:
            call.rtp.close()
        self._event('ended', call, reason)

    def _event(self, name, call=None, info=None):
        try:
            self.on_event(name, call, info)
        except Exception as exc:
            self.log('event handler error: %r' % (exc,))

    def media_call(self):
        c = self.calls.get(self.active_id) if self.active_id else None
        if c and c.state == CONNECTED and not c.local_held and not c.remote_held:
            return c
        return None

    # ── registration ───────────────────────────────────────────────────────
    def register(self, expires=None, challenge=None):
        exp = self.cfg.expires if expires is None else expires
        cfg = self.cfg
        if not self._reg_call_id:
            self._reg_call_id = new_call_id(self.local_ip)
        self._reg_cseq += 1
        uri = 'sip:%s' % cfg.domain
        m = self._base_request('REGISTER', uri)
        m.add('from', '<sip:%s@%s>;tag=%s' % (cfg.user, cfg.domain, self._reg_tag()))
        m.add('to', '<sip:%s@%s>' % (cfg.user, cfg.domain))
        m.add('call-id', self._reg_call_id)
        m.add('cseq', '%d REGISTER' % self._reg_cseq)
        m.add('contact', '%s;expires=%d' % (self._contact_hdr(), exp))
        m.add('expires', str(exp))
        m.add('allow', 'INVITE, ACK, CANCEL, BYE, OPTIONS, INFO')
        m.add('user-agent', cfg.user_agent)
        if challenge:
            hdr = 'proxy-authorization' if challenge['_proxy'] else 'authorization'
            m.add(hdr, digest_response(challenge, cfg.auth_user, cfg.password, 'REGISTER', uri))
        else:
            self._reg_tries = 0
        self._send_request(m, self.server_addr, lambda r, e=exp, ch=challenge: self._on_register_resp(r, e, ch),
                           timeout_handler=self._on_register_timeout)

    def _reg_tag(self):
        if not hasattr(self, '_regtag'):
            self._regtag = new_tag()
        return self._regtag

    def _on_register_timeout(self):
        self.registered = False
        self._event('registration_failed', None, 'timeout (no answer from %s:%d)' % self.server_addr)
        self._schedule(30, self.register)

    def _on_register_resp(self, resp, exp, used_challenge):
        if resp.status < 200:
            return
        if resp.status in (401, 407):
            hdr = resp.get('www-authenticate') or resp.get('proxy-authenticate')
            try:
                ch = parse_challenge(hdr)
            except Exception as exc:
                self._event('registration_failed', None, 'cannot parse challenge: %s' % exc)
                return
            stale = (ch.get('stale', '').lower() == 'true')
            if used_challenge is not None and not stale:
                self.registered = False
                self._event('registration_failed', None, '%d %s (wrong user/password?)' % (resp.status, resp.reason))
                self._schedule(60, self.register)
                return
            ch['_proxy'] = resp.status == 407
            self.register(expires=exp, challenge=ch)
            return
        if resp.status == 200:
            via = resp.get('via') or ''
            rec, rport = param(via, 'received'), param(via, 'rport')
            if rec:
                self.public_ip = rec
            if rport and rport.isdigit():
                self.public_port = int(rport)
            granted = exp
            for c in resp.get_all('contact'):
                e = param(c, 'expires')
                if e and e.isdigit() and ('@%s' % self.local_ip in c or (self.public_ip and self.public_ip in c)
                                          or self.cfg.user in c):
                    granted = int(e)
            if granted == exp and resp.get('expires', '').isdigit():
                granted = int(resp.get('expires'))
            if exp == 0:
                self.registered = False
                return
            need_fix = (not self._contact_fixed and self.public_ip and
                        (self.public_ip != self.local_ip or self.public_port != self.local_port))
            was = self.registered
            self.registered = True
            if need_fix:
                self._contact_fixed = True
                self.register()                      # re-register with the public contact
            else:
                self._schedule(max(20, int(granted * 0.75)), self._reregister)
            if not was:
                self._event('registered', None, 'expires %ds, public %s:%s' % (granted, self.public_ip, self.public_port))
        else:
            self.registered = False
            self._event('registration_failed', None, '%d %s' % (resp.status, resp.reason))
            self._schedule(60, self.register)

    def _reregister(self):
        if self._running:
            with self.lock:
                self.register()

    # ── incoming requests ──────────────────────────────────────────────────
    def _on_request(self, msg, addr):
        m = msg.method
        if m == 'INVITE':
            return self._on_invite(msg, addr)
        if m == 'ACK':
            return self._on_ack(msg)
        if m == 'BYE':
            return self._on_bye(msg, addr)
        if m == 'CANCEL':
            return self._on_cancel(msg, addr)
        if m == 'OPTIONS':
            return self._sendto(self._response(msg, 200, 'OK', addr, extra=[
                ('allow', 'INVITE, ACK, CANCEL, BYE, OPTIONS, INFO')]).to_bytes(), addr)
        if m in ('INFO', 'UPDATE', 'NOTIFY', 'MESSAGE'):
            ok = msg.call_id in self.calls or m in ('NOTIFY', 'MESSAGE')
            return self._sendto(self._response(msg, 200 if ok else 481, 'OK' if ok else
                                               'Call/Transaction Does Not Exist', addr).to_bytes(), addr)
        self._sendto(self._response(msg, 405, 'Method Not Allowed', addr).to_bytes(), addr)

    def _on_invite(self, msg, addr):
        cid = msg.call_id
        call = self.calls.get(cid)
        if call and param(msg.get('to'), 'tag'):
            return self._on_reinvite(call, msg, addr)
        if call:                                        # retransmitted initial INVITE
            if call.last_response:
                self._sendto(call.last_response, addr)
            return
        if len([c for c in self.calls.values() if c.state != ENDED]) >= self.cfg.max_calls:
            return self._sendto(self._response(msg, 486, 'Busy Here', addr, to_tag=new_tag()).to_bytes(), addr)
        call = Call(self, 'in')
        call.call_id = cid
        call.invite = msg
        call.invite_branch = msg.top_branch()
        call.peer_addr = addr
        frm = msg.get('from')
        call.number = parse_uri(uri_of(frm))['user']
        call.name = display_name_of(frm)
        pai = msg.get('p-asserted-identity')
        if pai and not call.name:
            call.name = display_name_of(pai)
        call.local_hdr = msg.get('to') + ';tag=' + call.local_tag
        call.remote_hdr = frm
        call.remote_target = uri_of(msg.get('contact')) or uri_of(frm)
        call.route_set = [uri_of(r) for r in msg.get_all('record-route')]
        call.remote_cseq = msg.cseq[0]
        if msg.body:
            sdp = self.parse_sdp(msg.body)
            call.remote_offer = sdp
            if not sdp or self._pick_codec(sdp) is None:
                return self._sendto(self._response(msg, 488, 'Not Acceptable Here', addr,
                                                   to_tag=call.local_tag).to_bytes(), addr)
        else:
            call.delayed_offer = True
        call.line = self._free_line()
        call.state = RINGING_IN
        self.calls[cid] = call
        self._sendto(self._response(msg, 100, 'Trying', addr).to_bytes(), addr)
        ring = self._response(msg, 180, 'Ringing', addr, to_tag=call.local_tag, contact=True)
        call.last_response = ring.to_bytes()
        self._sendto(call.last_response, addr)
        self._event('incoming', call)

    def _on_cancel(self, msg, addr):
        self._sendto(self._response(msg, 200, 'OK', addr).to_bytes(), addr)
        call = self.calls.get(msg.call_id)
        if call and call.state == RINGING_IN:
            resp = self._response(call.invite, 487, 'Request Terminated', addr, to_tag=call.local_tag)
            call.last_response = resp.to_bytes()
            self._sendto(call.last_response, call.peer_addr)
            self._end_call(call, 'caller hung up before answer')

    def _on_bye(self, msg, addr):
        call = self.calls.get(msg.call_id)
        if not call or call.state == ENDED:
            return self._sendto(self._response(msg, 481, 'Call/Transaction Does Not Exist', addr).to_bytes(), addr)
        self._sendto(self._response(msg, 200, 'OK', addr).to_bytes(), addr)
        self._end_call(call, 'remote hung up')

    def _on_ack(self, msg):
        call = self.calls.get(msg.call_id)
        if not call or call.state == ENDED:
            return
        if not call.ack_received:
            call.ack_received = True
            if call.delayed_offer and msg.body:
                self._apply_remote_sdp(call, self.parse_sdp(msg.body))

    def _on_reinvite(self, call, msg, addr):
        if call.state == ENDED:
            return self._sendto(self._response(msg, 481, 'Call/Transaction Does Not Exist', addr).to_bytes(), addr)
        if msg.cseq[0] == call.remote_cseq and call.last_reinvite_resp:
            return self._sendto(call.last_reinvite_resp, addr)
        call.remote_cseq = msg.cseq[0]
        was_held = call.remote_held
        sdp = self.parse_sdp(msg.body) if msg.body else None
        if msg.body and (not sdp or self._pick_codec(sdp) is None):
            return self._sendto(self._response(msg, 488, 'Not Acceptable Here', addr).to_bytes(), addr)
        if sdp:
            self._apply_remote_sdp(call, sdp)
            if sdp['ip'] and sdp['ip'] != '0.0.0.0' and call.rtp and call.rtp.remote != (sdp['ip'], sdp['port']):
                call.rtp.remote = (sdp['ip'], sdp['port'])
                call.rtp.latched = False
        rd = call.remote_dir
        ours = {'sendrecv': 'sendonly' if call.local_held else 'sendrecv',
                'sendonly': 'inactive' if call.local_held else 'recvonly',
                'recvonly': 'sendonly', 'inactive': 'inactive'}[rd]
        call.sdp_ver += 1
        resp = self._response(msg, 200, 'OK', addr, contact=True, body=self._sdp(call, ours, only_pt=call.pt))
        call.last_reinvite_resp = resp.to_bytes()
        self._sendto(call.last_reinvite_resp, addr)
        if call.remote_held and not was_held:
            self._event('remote_hold', call)
        elif was_held and not call.remote_held:
            self._event('remote_resume', call)

    # ── responses ──────────────────────────────────────────────────────────
    def _on_response(self, resp, addr):
        key = (resp.top_branch(), resp.cseq[1])
        txn = self._txns.get(key)
        if not txn:
            # retransmitted 2xx for an INVITE we already ACKed -> ACK again
            call = self.calls.get(resp.call_id)
            if call and resp.cseq[1] == 'INVITE' and 200 <= resp.status < 300 and call.state != ENDED:
                self._send_ack(call, resp.cseq[0])
            return
        if resp.status >= 200 or not txn['invite']:
            txn['done'] = True
            if resp.status >= 200:
                self._txns.pop(key, None)
        elif txn['invite']:
            txn['done'] = True                           # stop retransmitting after a 1xx
        txn['handler'](resp)

    def _dialog_request(self, call, method, cseq=None, branch=None):
        m = self._base_request(method, call.remote_target, branch)
        for r in call.route_set:
            m.add('route', '<%s>' % r if not r.startswith('<') else r)
        m.add('from', call.local_hdr)
        m.add('to', call.remote_hdr)
        m.add('call-id', call.call_id)
        m.add('cseq', '%d %s' % (cseq if cseq is not None else call.local_cseq, method))
        m.add('user-agent', self.cfg.user_agent)
        return m

    def _send_ack(self, call, cseq):
        ack = self._dialog_request(call, 'ACK', cseq=cseq)
        self._sendto(ack.to_bytes(), call.peer_addr or self.server_addr)

    # ── user actions ───────────────────────────────────────────────────────
    def answer(self, ref):
        with self.lock:
            call = self.get_call(ref)
            if not call or call.state != RINGING_IN:
                return False
            self._open_rtp(call)
            if call.delayed_offer:
                body = self._sdp(call, 'sendrecv')
            else:
                self._apply_remote_sdp(call, call.remote_offer)
                body = self._sdp(call, 'sendrecv', only_pt=call.pt)
            resp = self._response(call.invite, 200, 'OK', call.peer_addr, to_tag=call.local_tag,
                                  contact=True, body=body)
            call.last_response = resp.to_bytes()
            self._sendto(call.last_response, call.peer_addr)
            self._schedule(T1, self._retx_200, call, T1, time.monotonic())
            self._become_active(call)
            return True

    def _retx_200(self, call, interval, started):
        with self.lock:
            if call.ack_received or call.state != CONNECTED:
                return
            if time.monotonic() - started > 32:
                self.log('no ACK for 200 OK, hanging up')
                self.hangup(call)
                return
            self._sendto(call.last_response, call.peer_addr)
            nxt = min(interval * 2, 4.0)
            self._schedule(nxt, self._retx_200, call, nxt, started)

    def _become_active(self, call):
        prev = self.calls.get(self.active_id) if self.active_id else None
        call.state = CONNECTED
        call.connected_at = time.time()
        call.rtp.start()
        if call.rtp.remote:
            call.rtp.send_silence(2)                     # open the NAT pinhole
        self.active_id = call.call_id
        if prev and prev is not call and prev.state == CONNECTED:
            if self.cfg.switch_mode == 'hangup':
                self.hangup(prev)
            else:
                self.hold(prev)
        self._event('connected', call)

    def reject(self, ref, code=486, reason='Busy Here'):
        with self.lock:
            call = self.get_call(ref)
            if not call or call.state != RINGING_IN:
                return False
            resp = self._response(call.invite, code, reason, call.peer_addr, to_tag=call.local_tag)
            call.last_response = resp.to_bytes()
            self._sendto(call.last_response, call.peer_addr)
            self._end_call(call, 'rejected')
            return True

    def hangup(self, ref):
        with self.lock:
            call = self.get_call(ref)
            if not call or call.state == ENDED:
                return False
            if call.state == RINGING_IN:
                return self.reject(call, 603, 'Decline')
            if call.state in (DIALING, RINGING_OUT):
                self._cancel(call)
                return True
            call.local_cseq += 1
            bye = self._dialog_request(call, 'BYE')
            self._send_request(bye, call.peer_addr or self.server_addr, lambda r: None)
            self._end_call(call, 'we hung up')
            return True

    def hold(self, ref):
        with self.lock:
            call = self.get_call(ref)
            if not call or call.state != CONNECTED or call.local_held:
                return False
            call.local_held = True
            self._reinvite(call, 'sendonly')
            self._event('held', call)
            return True

    def resume(self, ref):
        """Make this line the active one (answers it when ringing, resumes it when held)."""
        with self.lock:
            call = self.get_call(ref)
            if not call:
                return False
            if call.state == RINGING_IN:
                return self.answer(call)
            if call.state != CONNECTED:
                return False
            if call.call_id != self.active_id:
                prev = self.calls.get(self.active_id) if self.active_id else None
                self.active_id = call.call_id
                if prev and prev.state == CONNECTED:
                    if self.cfg.switch_mode == 'hangup':
                        self.hangup(prev)
                    else:
                        self.hold(prev)
            if call.local_held:
                call.local_held = False
                call.rtp.reset_receive()
                self._reinvite(call, 'sendrecv')
                self._event('resumed', call)
            return True

    def _reinvite(self, call, direction):
        call.local_cseq += 1
        call.sdp_ver += 1
        m = self._dialog_request(call, 'INVITE')
        m.add('contact', self._contact_hdr())
        m.add('content-type', 'application/sdp')
        m.body = self._sdp(call, direction, only_pt=call.pt).encode()
        cseq = call.local_cseq

        def handler(resp, c=call, n=cseq):
            if resp.status < 200:
                return
            if 200 <= resp.status < 300:
                self._send_ack(c, n)
            else:
                self._event('hold_failed', c, '%d %s' % (resp.status, resp.reason))
        self._send_request(m, call.peer_addr or self.server_addr, handler)

    def send_dtmf(self, ref, digits):
        call = self.get_call(ref)
        if not call or call.state != CONNECTED or not call.rtp:
            return False
        for d in digits:
            call.rtp.queue_dtmf(d)
        return True

    # ── outgoing calls ─────────────────────────────────────────────────────
    def dial(self, number):
        with self.lock:
            if len([c for c in self.calls.values() if c.state != ENDED]) >= self.cfg.max_calls:
                raise RuntimeError('all lines busy')
            cfg = self.cfg
            call = Call(self, 'out')
            call.call_id = new_call_id(self.local_ip)
            call.number = number
            call.line = self._free_line()
            call.state = DIALING
            call.peer_addr = self.server_addr
            call.invite_uri = 'sip:%s@%s' % (number, cfg.domain)
            name = '"%s" ' % cfg.display_name if cfg.display_name else ''
            call.local_hdr = '%s<sip:%s@%s>;tag=%s' % (name, cfg.user, cfg.domain, call.local_tag)
            call.remote_hdr = '<%s>' % call.invite_uri
            call.remote_target = call.invite_uri
            self._open_rtp(call)
            self.calls[call.call_id] = call
            self._send_invite(call)
            self._event('dialing', call)
            return call

    def _send_invite(self, call, challenge=None):
        branch = new_branch()
        m = self._base_request('INVITE', call.invite_uri, branch)
        m.add('from', call.local_hdr)
        m.add('to', call.remote_hdr)
        m.add('call-id', call.call_id)
        m.add('cseq', '%d INVITE' % call.local_cseq)
        m.add('contact', self._contact_hdr())
        m.add('allow', 'INVITE, ACK, CANCEL, BYE, OPTIONS, INFO')
        m.add('user-agent', self.cfg.user_agent)
        if challenge:
            hdr = 'proxy-authorization' if challenge['_proxy'] else 'authorization'
            m.add(hdr, digest_response(challenge, self.cfg.auth_user, self.cfg.password, 'INVITE', call.invite_uri))
        m.add('content-type', 'application/sdp')
        m.body = self._sdp(call, 'sendrecv').encode()
        call.invite, call.invite_branch = m, branch
        self._send_request(m, self.server_addr, lambda r, c=call: self._on_invite_resp(c, r),
                           timeout_handler=lambda c=call: self._end_call(c, 'no answer from server'))

    def _on_invite_resp(self, call, resp):
        if call.state == ENDED and resp.status >= 300:
            self._ack_failure(call, resp)
            return
        if resp.status < 200:
            if resp.status in (180, 183) and call.state == DIALING:
                call.state = RINGING_OUT
                self._event('ringing_out', call)
            return
        if 200 <= resp.status < 300:
            call.remote_hdr = resp.get('to')
            call.remote_target = uri_of(resp.get('contact')) or call.invite_uri
            call.route_set = [uri_of(r) for r in reversed(resp.get_all('record-route'))]
            sdp = self.parse_sdp(resp.body)
            if call.state == ENDED:                       # answered after we cancelled: hang up
                call.local_cseq = resp.cseq[0]
                self._send_ack(call, resp.cseq[0])
                call.state = CONNECTED
                self.hangup(call)
                return
            if not sdp or not self._apply_remote_sdp(call, sdp):
                self._send_ack(call, resp.cseq[0])
                call.state = CONNECTED
                self.hangup(call)
                self._event('call_failed', call, 'no common codec')
                return
            self._send_ack(call, resp.cseq[0])
            call.ack_received = True
            self._become_active(call)
            return
        # final failure
        self._ack_failure(call, resp)
        if resp.status in (401, 407) and not call.auth_tried:
            try:
                ch = parse_challenge(resp.get('www-authenticate') or resp.get('proxy-authenticate'))
            except Exception as exc:
                return self._end_call(call, 'auth error: %s' % exc)
            ch['_proxy'] = resp.status == 407
            call.auth_tried = True
            call.local_cseq += 1
            self._send_invite(call, ch)
            return
        reason = '%d %s' % (resp.status, resp.reason)
        self._end_call(call, reason)
        self._event('call_failed', call, reason)

    def _ack_failure(self, call, resp):
        ack = SipMessage()
        ack.method, ack.uri = 'ACK', call.invite_uri
        ack.add('via', self._via(call.invite_branch))
        ack.add('max-forwards', '70')
        ack.add('from', call.local_hdr)
        ack.add('to', resp.get('to'))
        ack.add('call-id', call.call_id)
        ack.add('cseq', '%d ACK' % resp.cseq[0])
        self._sendto(ack.to_bytes(), self.server_addr)

    def _cancel(self, call):
        inv = call.invite
        m = SipMessage()
        m.method, m.uri = 'CANCEL', inv.uri
        m.add('via', inv.get('via'))
        m.add('max-forwards', '70')
        m.add('from', inv.get('from'))
        m.add('to', inv.get('to'))
        m.add('call-id', call.call_id)
        m.add('cseq', '%d CANCEL' % inv.cseq[0])
        self._send_request(m, self.server_addr, lambda r: None)
        # call is ended when the 487 (or a late 200 -> BYE) arrives; mark intent now
        call.state = ENDED
        call.end_reason = 'cancelled'
        if call.rtp:
            call.rtp.close()
        self._event('ended', call, 'cancelled')

    # ── status ─────────────────────────────────────────────────────────────
    def status(self):
        with self.lock:
            return [dict(c.snapshot(), active=(c.call_id == self.active_id)) for c in self.live_calls()]
