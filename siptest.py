"""VoiceTrack Magic - SIP test console.

Usage:  SipTest.exe [--config siptest.json] [--list-devices]
Reads the SIP account from a JSON file (never commit it!). See siptest.example.json.

Commands (type in the console):
  status                 list lines
  answer N               answer ringing line N (makes it active, holds/hangs up the previous one)
  switch N               make line N active (resume if held, answer if ringing)
  hold N | reject N | hangup N
  dial NUMBER            outgoing call
  dtmf DIGITS            send DTMF on the active line
  rec start [file.wav]   start recording (mic + active call)
  rec stop               stop recording
  split on|off           recording: you=left / caller=right  (default: both mixed on both channels)
  mode hold|hangup       what happens to the previous line when switching
  devices                list audio devices
  quit
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime

from sipphone.media import Mixer, NullAudio
from sipphone.ring import RingTone
from sipphone.ua import SipConfig, UserAgent


def _ts():
    return datetime.now().strftime('%H:%M:%S')


def say(msg):
    print('[%s] %s' % (_ts(), msg), flush=True)


def list_devices():
    import sounddevice as sd
    print(sd.query_devices())


def main(argv=None):
    ap = argparse.ArgumentParser(description='VoiceTrack Magic SIP test console')
    ap.add_argument('--config', default='siptest.json')
    ap.add_argument('--list-devices', action='store_true')
    ap.add_argument('--no-audio', action='store_true', help='no sound card (signalling test only)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(argv)

    if args.list_devices:
        list_devices()
        return 0
    if not os.path.exists(args.config):
        print('Config file not found: %s\nCopy siptest.example.json to siptest.json and fill it in.' % args.config)
        return 2
    with open(args.config, encoding='utf-8') as f:
        conf = json.load(f)
    pwd = os.environ.get('SIP_PASSWORD') or conf.get('password', '')
    cfg = SipConfig(conf['user'], pwd, conf['domain'],
                    **{k: v for k, v in conf.items() if k in (
                        'auth_user', 'display_name', 'proxy', 'port', 'local_port', 'expires',
                        'rtp_port_min', 'rtp_port_max', 'max_calls', 'switch_mode', 'keepalive')})

    if args.no_audio:
        audio = NullAudio()
    else:
        from sipphone.media import SoundDeviceAudio
        audio = SoundDeviceAudio(conf.get('input_device'), conf.get('output_device'),
                                 int(conf.get('device_rate', 48000)))
    ring = RingTone(conf.get('ring_device')) if not args.no_audio else None
    split = {'on': bool(conf.get('record_split', False))}
    state = {'ringing': set()}

    def refresh_ring():
        if not ring:
            return
        if state['ringing']:
            try:
                ring.start()
            except Exception as exc:
                say('ring device error: %s' % exc)
        else:
            ring.stop()

    def on_event(name, call, info):
        if name == 'incoming':
            state['ringing'].add(call.call_id)
            say('INCOMING  line %d  %s   (answer %d / reject %d)' % (call.line, call.label, call.line, call.line))
        elif name in ('connected', 'ended', 'call_failed'):
            if call:
                state['ringing'].discard(call.call_id)
            if name == 'connected':
                say('CONNECTED line %d  %s' % (call.line, call.label))
            elif name == 'ended':
                say('ENDED     line %s  %s  (%s)' % (call.line, call.label, info))
            else:
                say('FAILED    %s  %s' % (call.label, info))
        elif name == 'registered':
            say('REGISTERED  %s' % info)
        elif name == 'registration_failed':
            say('REGISTRATION FAILED: %s' % info)
        elif name == 'dtmf':
            say('DTMF from line %d: %s' % (call.line, info))
        elif name in ('held', 'resumed', 'remote_hold', 'remote_resume', 'ringing_out', 'dialing', 'hold_failed'):
            say('%s  line %s  %s %s' % (name.upper(), call.line, call.label, info or ''))
        refresh_ring()

    ua = UserAgent(cfg, on_event, log=(say if args.verbose else (lambda *a: None)))
    audio.start()
    mixer = Mixer(audio, ua.media_call, log=say)
    mixer.start()
    say('Starting SIP as %s@%s -> %s' % (cfg.user, cfg.domain, cfg.proxy))
    ua.start()

    print(__doc__)
    try:
        while True:
            try:
                line = input('> ').strip()
            except EOFError:
                break
            if not line:
                continue
            cmd, _, rest = line.partition(' ')
            cmd, rest = cmd.lower(), rest.strip()
            try:
                if cmd in ('quit', 'exit', 'q'):
                    break
                elif cmd == 'status':
                    rows = ua.status()
                    print('registered: %s' % ua.registered)
                    for r in rows:
                        print('  line %(line)s %(state)-18s %(who)s  %(secs)ds %(star)s' % dict(r, star='*ACTIVE*' if r['active'] else ''))
                    if not rows:
                        print('  (no calls)')
                    print('mic/remote level: %.0f / %.0f' % mixer.last_rms)
                elif cmd == 'answer':
                    ua.answer(int(rest)) or say('no such ringing line')
                elif cmd == 'switch':
                    ua.resume(int(rest)) or say('cannot switch to that line')
                elif cmd == 'hold':
                    ua.hold(int(rest)) or say('cannot hold')
                elif cmd == 'reject':
                    ua.reject(int(rest)) or say('no such ringing line')
                elif cmd == 'hangup':
                    ua.hangup(int(rest)) or say('no such line')
                elif cmd == 'dial':
                    ua.dial(rest)
                elif cmd == 'dtmf':
                    c = ua.media_call()
                    if c:
                        ua.send_dtmf(c, rest)
                    else:
                        say('no active call')
                elif cmd == 'rec':
                    sub, _, fn = rest.partition(' ')
                    if sub == 'start':
                        fn = fn.strip() or datetime.now().strftime('rec_%Y%m%d_%H%M%S.wav')
                        mixer.start_recording(fn, split['on'])
                        say('recording -> %s (%s)' % (fn, 'split' if split['on'] else 'dual-mono mix'))
                    elif sub == 'stop':
                        path, secs = mixer.stop_recording()
                        say('saved %s (%.1f s)' % (path, secs) if path else 'not recording')
                elif cmd == 'split':
                    split['on'] = rest.lower() == 'on'
                    say('split %s (applies to the next recording)' % ('ON' if split['on'] else 'OFF'))
                elif cmd == 'mode':
                    ua.cfg.switch_mode = 'hangup' if rest.lower() == 'hangup' else 'hold'
                    say('switch mode: %s' % ua.cfg.switch_mode)
                elif cmd == 'devices':
                    list_devices()
                else:
                    print('unknown command')
            except Exception as exc:
                say('error: %r' % (exc,))
    finally:
        say('shutting down...')
        mixer.stop()
        ua.stop()
        audio.stop()
        if ring:
            ring.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
