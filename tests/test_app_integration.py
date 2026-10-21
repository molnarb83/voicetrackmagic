"""End-to-end: main window + SIP phone + recorder, with a fake sound card and the mock SIP provider."""
import os
import shutil
import sys
import tempfile
import time
import wave

import numpy as np
import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import fake_sd                                                   # noqa: E402
fake_sd.install()

from PyQt5.QtWidgets import QApplication                         # noqa: E402
from test_integration import MockProvider, spectrum_peaks, wait_for   # noqa: E402

_app = QApplication.instance() or QApplication([])


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        _app.processEvents()
        time.sleep(0.01)


def pump_until(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        _app.processEvents()
        v = cond()
        if v:
            return v
        time.sleep(0.01)
    return None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('APPDATA', str(home / 'appdata'))
    monkeypatch.setenv('LOCALAPPDATA', str(home / 'local'))
    monkeypatch.setenv('USERPROFILE', str(home))
    prov = MockProvider()
    yield {'prov': prov, 'tmp': tmp_path}
    prov.close()


def make_window(prov, **extra):
    import main as appmain
    from modules.config_manager import ConfigManager
    cfg = ConfigManager()
    s = cfg.load_settings()
    s.update({
        'sip_enabled': True, 'sip_user': 'user', 'sip_pass': 'secret', 'sip_domain': 'mock.test',
        'sip_proxy': '127.0.0.1', 'sip_port': prov.port, 'sip_auto_open': True,
        'sip_rtp_min': 31000, 'sip_rtp_max': 31400,
        'device_index': 0, 'coffee_reminder_month': time.strftime('%Y-%m'),
    })
    s.update(extra)
    cfg.save_settings(s)
    win = appmain.VoiceTrackMagic()
    return appmain, win


def read_wav(path):
    with wave.open(path) as w:
        ch, n = w.getnchannels(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).reshape(-1, ch)
        return data, w.getframerate()


def full_call(env, win, cid='app-call-0001@mock'):
    prov = env['prov']
    assert pump_until(lambda: win.phone and win.phone.registered), 'not registered'
    prov.invite_ua(cid, '36301234567', 'Alice')
    assert pump_until(lambda: win.phone.lines() and win.phone.lines()[0]['ringing']), 'no incoming'
    assert win.phone.answer(1)
    assert pump_until(lambda: prov.responses(cid, 200))
    prov.ack(cid)
    return cid


def test_settings_roundtrip_and_password_obfuscated(env):
    appmain, win = make_window(env['prov'])
    try:
        raw = open(win.config.config_file).read()
        assert 'secret' not in raw and '"sip_pass": "enc:' in raw
        assert win.config.load_settings()['sip_pass'] == 'secret'
        dlg = appmain.SettingsDialog(win.recorder, win.config, win)
        assert dlg.sip_tab.user.text() == 'user' and dlg.sip_tab.password.text() == 'secret'
        dlg.sip_tab.layout_combo.setCurrentIndex(1)
        dlg.sip_tab.switch_combo.setCurrentIndex(1)
        dlg.sip_tab.in_combo.setCurrentIndex(dlg.sip_tab.in_combo.findData('Second Mic'))
        dlg._save_accept()
        s = win.config.load_settings()
        assert s['sip_rec_split'] is True and s['sip_switch_mode'] == 'hangup' and s['sip_input_name'] == 'Second Mic'
        assert s['sip_pass'] == 'secret' and s['filter_preset'] == win.config.load_settings()['filter_preset']
    finally:
        win.close()


def test_incoming_call_window_strip_and_recording_mixes_both_voices(env):
    prov = env['prov']
    appmain, win = make_window(prov)
    try:
        cid = full_call(env, win)
        # window opened automatically, strip shows the call
        assert win._call_dlg is not None and win._call_dlg.isVisible()
        win._phone_tick()
        assert 'In call' in win.phone_strip.lbl.text() or 'incoming' in win.phone_strip.lbl.text().lower()
        # the mic stream is opened for the call (monitor) and the caller hears the microphone (300 Hz)
        assert pump_until(lambda: win.recorder.is_monitoring()), 'mic monitor not started for the call'
        assert pump_until(lambda: len(prov.rtp.audio_payload(8)) > 4000)
        from sipphone import g711
        heard = g711.decode(prov.rtp.audio_payload(8), 8)[2000:10000]
        band, peak = spectrum_peaks(heard)
        assert band(300) > 0.5 * peak, 'caller does not hear the microphone'
        # caller speaks a 1 kHz tone; we record
        prov.rtp.send_tone(1000, 4.0)
        pump(0.3)
        assert win.recorder.start_recording(0)
        pump(2.5)
        assert win.recorder.stop_recording()
        data, sr = read_wav(win.recorder.temp_file)
        assert data.shape[1] == 1 and sr == 48000
        seg = data[sr:sr + 24000, 0]
        band, peak = spectrum_peaks(seg, sr)
        assert band(300) > 0.15 * peak and band(1000) > 0.15 * peak, (band(300), band(1000), peak)
        # after the recording stops the phone keeps the mic alive
        win._phone_tick()
        assert pump_until(lambda: win.recorder.is_monitoring()), 'monitor not restarted after recording'
        # remote hang-up -> everything is released
        prov.in_dialog_request(cid, 'BYE')
        assert pump_until(lambda: win.phone.lines() == [])
        pump(0.2)
        win._phone_tick()
        assert not win.recorder.is_monitoring() and not win.phone.needs_mic
    finally:
        win.close()


def test_split_recording_left_me_right_caller(env):
    prov = env['prov']
    appmain, win = make_window(prov, sip_rec_split=True)
    try:
        full_call(env, win, 'app-call-0002@mock')
        win._phone_tick()
        prov.rtp.send_tone(1000, 3.5)
        pump(0.3)
        assert win.recorder.start_recording(0)
        pump(2.0)
        assert win.recorder.stop_recording()
        data, sr = read_wav(win.recorder.temp_file)
        assert data.shape[1] == 2
        left, right = data[sr // 2:sr // 2 + 24000, 0], data[sr // 2:sr // 2 + 24000, 1]
        bl, pl = spectrum_peaks(left, sr)
        br, pr = spectrum_peaks(right, sr)
        assert bl(300) > 0.5 * pl and bl(1000) < 0.2 * pl, 'left must be me only'
        assert br(1000) > 0.5 * pr and br(300) < 0.2 * pr, 'right must be the caller only'
    finally:
        win.close()


def test_recording_without_call_is_unchanged(env):
    prov = env['prov']
    appmain, win = make_window(prov)
    try:
        assert pump_until(lambda: win.phone.registered)
        assert win.recorder.start_recording(0)
        pump(1.2)
        assert win.recorder.stop_recording()
        data, sr = read_wav(win.recorder.temp_file)
        band, peak = spectrum_peaks(data[sr // 4:sr // 4 + 24000, 0], sr)
        assert band(300) > 0.5 * peak and band(1000) < 0.05 * peak
    finally:
        win.close()


def test_custom_caller_input_device(env):
    prov = env['prov']
    appmain, win = make_window(prov, sip_input_name='Second Mic')
    try:
        full_call(env, win, 'app-call-0003@mock')
        assert pump_until(lambda: len(prov.rtp.audio_payload(8)) > 4000)
        from sipphone import g711
        heard = g711.decode(prov.rtp.audio_payload(8), 8)[2000:10000]
        band, peak = spectrum_peaks(heard)
        assert band(700) > 0.5 * peak and band(300) < 0.1 * peak, 'caller must hear the custom device'
        assert not win.phone.needs_mic, 'recorder mic must not be required for a custom caller input'
    finally:
        win.close()


def test_two_lines_in_call_window(env):
    prov = env['prov']
    appmain, win = make_window(prov)
    try:
        assert pump_until(lambda: win.phone.registered)
        a, b = 'app-call-0004@mock', 'app-call-0005@mock'
        prov.invite_ua(a, '111111111', 'A')
        prov.invite_ua(b, '222222222', 'B')
        assert pump_until(lambda: len(win.phone.lines()) == 2)
        assert pump_until(lambda: win._call_dlg is not None), 'call window did not open'
        dlg = win._call_dlg
        dlg.refresh()
        assert dlg.table.rowCount() == 2
        assert win.phone.answer(1)
        assert pump_until(lambda: prov.responses(a, 200))
        prov.ack(a)
        win.phone.answer(2)                                # second call: first goes on hold
        assert pump_until(lambda: prov.responses(b, 200))
        assert pump_until(lambda: [r for r in prov.requests('INVITE', a) if b'a=sendonly' in r.body])
        rows = win.phone.lines()
        assert [r['active'] for r in rows] == [False, True] and rows[0]['state'].startswith('held')
        dlg.refresh()
        assert dlg.table.item(1, 2).text() == 'ACTIVE'
    finally:
        win.close()
