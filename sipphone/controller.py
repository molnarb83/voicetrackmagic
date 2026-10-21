"""PhoneController: owns the SIP user agent, the media mixer, the audio bridge and the ring tone,
and wires them to the recorder. No Qt in here; the GUI talks to this class."""
import os

from .bridge import CallAudio
from .media import Mixer
from .phonebook import Phonebook
from .ring import RingTone
from .ua import CONNECTED, RINGING_IN, SipConfig, UserAgent


class PhoneController:
    """settings keys: sip_user, sip_pass, sip_domain, sip_proxy, sip_port, sip_auth_user, sip_display_name,
    sip_max_calls, sip_switch_mode, sip_rec_split; device indexes are passed separately (resolved by the GUI)."""

    def __init__(self, settings, recorder, phonebook_path, in_device=None, out_device=None, ring_device=None,
                 on_event=None, log=None, use_devices=True):
        s = settings
        self.recorder = recorder
        self.on_event = on_event or (lambda *a: None)
        self.log = log or (lambda *a: None)
        self.phonebook = Phonebook(phonebook_path)
        cfg = SipConfig(s.get('sip_user', ''), s.get('sip_pass', ''), s.get('sip_domain', ''),
                        proxy=(s.get('sip_proxy') or None), port=s.get('sip_port', 5060) or 5060,
                        auth_user=s.get('sip_auth_user') or None, display_name=s.get('sip_display_name', ''),
                        max_calls=s.get('sip_max_calls', 10) or 10,
                        switch_mode=s.get('sip_switch_mode', 'hold'),
                        expires=s.get('sip_expires', 300) or 300,
                        rtp_port_min=s.get('sip_rtp_min', 10000), rtp_port_max=s.get('sip_rtp_max', 10200))
        self.audio = CallAudio(in_device, out_device, use_devices)
        self.ua = UserAgent(cfg, self._on_ua_event, log=self.log)
        self.mixer = Mixer(self.audio, self.ua.media_call, log=self.log)
        self.ring = RingTone(ring_device)
        self.split = bool(s.get('sip_rec_split', False))
        self.started = False
        self._ringing = set()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        self.recorder.mic_tap = self.audio.feed_mic
        self.recorder.call_mix = self.audio.get_remote
        self.recorder.call_split = self.split
        self.ua.start()
        self.mixer.start()
        self.started = True

    def stop(self):
        if not self.started:
            return
        self.started = False
        self.recorder.mic_tap = None
        self.recorder.call_mix = None
        self.recorder.call_split = False
        try:
            self.ring.stop()
        except Exception:
            pass
        self.mixer.stop()
        self.ua.stop()
        self.audio.deactivate()

    # ── events ─────────────────────────────────────────────────────────────
    def _on_ua_event(self, name, call, info):
        try:
            if name == 'incoming':
                self._ringing.add(call.call_id)
            elif name in ('connected', 'ended', 'call_failed') and call:
                self._ringing.discard(call.call_id)
            self._update_ring()
            if name == 'connected':
                self.audio.activate()
            elif name in ('ended', 'call_failed'):
                if not any(c.state == CONNECTED for c in self.ua.live_calls()):
                    self.audio.deactivate()
        except Exception as exc:
            self.log('controller event error: %r' % (exc,))
            self.on_event('error', call, 'audio/ring: %s' % exc)
        self.on_event(name, call, info)

    def _update_ring(self):
        ringing = any(c.state == RINGING_IN for c in self.ua.live_calls())
        try:
            if ringing:
                self.ring.start()
            else:
                self.ring.stop()
        except Exception as exc:
            self.log('ring error: %r' % (exc,))

    # ── state ──────────────────────────────────────────────────────────────
    @property
    def registered(self):
        return self.ua.registered

    @property
    def needs_mic(self):
        """True while a call is connected and the caller should hear the recording input."""
        return self.audio.active and self.audio.uses_recorder_mic

    def set_split(self, split):
        self.split = bool(split)
        self.recorder.call_split = self.split

    def lines(self):
        """Snapshot for the UI: list of dicts; 'name' = phonebook name, else the SIP display name."""
        rows = []
        for r in self.ua.status():
            r = dict(r)
            r['name'] = self.phonebook.lookup(r['number']) or r['name']
            rows.append(r)
        return rows

    # ── actions (thin wrappers) ────────────────────────────────────────────
    def dial(self, number):
        return self.ua.dial(number.strip())

    def answer(self, line):
        return self.ua.answer(line)

    def switch_to(self, line):
        return self.ua.resume(line)

    def hold(self, line):
        return self.ua.hold(line)

    def hangup(self, line):
        return self.ua.hangup(line)

    def reject(self, line):
        return self.ua.reject(line)

    def dtmf(self, digits):
        c = self.ua.media_call()
        return bool(c) and self.ua.send_dtmf(c, digits)
