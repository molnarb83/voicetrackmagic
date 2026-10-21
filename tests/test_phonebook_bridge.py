import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sipphone.bridge import CallAudio                       # noqa: E402
from sipphone.phonebook import Phonebook, match_key, normalize   # noqa: E402


def test_number_normalisation_matches_all_hungarian_formats():
    forms = ['+36 30 123 4567', '0036301234567', '06301234567', '36301234567', '(30) 123-4567']
    keys = {match_key(f) for f in forms}
    assert keys == {'301234567'}
    assert normalize('+36 30 123 4567') == '36301234567'


def test_phonebook_lookup_add_update_remove(tmp_path):
    pb = Phonebook(str(tmp_path / 'pb.json'))
    assert pb.add('Alice', '+36 30 123 4567')
    assert pb.lookup('06301234567') == 'Alice'
    assert pb.lookup('0036301234567') == 'Alice'
    assert pb.lookup('+36 20 999 9999') == ''
    pb.add('Alice B.', '06 30 123 4567')                       # same person -> updated, not duplicated
    assert len(pb.entries) == 1 and pb.lookup('+36301234567') == 'Alice B.'
    assert not pb.add('x', 'abc')
    pb.save()
    assert Phonebook(str(tmp_path / 'pb.json')).lookup('301234567') == 'Alice B.'
    assert pb.remove(0) and pb.entries == []


def test_import_vcard_with_folded_lines_and_several_numbers(tmp_path):
    f = tmp_path / 'c.vcf'
    f.write_text('BEGIN:VCARD\nVERSION:3.0\nFN:Kiss Béla\nTEL;TYPE=CELL:+36 30 111 2222\n'
                 'TEL;TYPE=WORK:+36 1 222 3333\nEND:VCARD\n'
                 'BEGIN:VCARD\nVERSION:3.0\nN:Nagy;Anna;;;\nitem1.TEL:0620 555 6666\nEND:VCARD\n'
                 'BEGIN:VCARD\nVERSION:3.0\nFN:No Number\nEND:VCARD\n', encoding='utf-8')
    pb = Phonebook(str(tmp_path / 'pb.json'))
    assert pb.import_vcf(str(f)) == 3
    assert pb.lookup('+36301112222') == 'Kiss Béla'
    assert pb.lookup('+3612223333') == 'Kiss Béla'
    assert pb.lookup('06205556666') == 'Anna Nagy'


def test_import_csv_google_and_outlook_styles(tmp_path):
    g = tmp_path / 'google.csv'
    g.write_text('Name,Phone 1 - Label,Phone 1 - Value,Phone 2 - Value\n'
                 'Teszt Elek,Mobile,+36 30 777 8888 ::: +36 1 444 5555,+36 70 123 0000\n', encoding='utf-8')
    o = tmp_path / 'outlook.csv'
    o.write_text('First Name;Last Name;Mobile Phone;Business Phone\n'
                 'Jane;Doe;06 20 321 4321;\n', encoding='cp1250')
    pb = Phonebook(str(tmp_path / 'pb.json'))
    assert pb.import_csv(str(g)) == 3 and pb.lookup('06307778888') == 'Teszt Elek'
    assert pb.lookup('+3614445555') == 'Teszt Elek'
    assert pb.import_csv(str(o)) == 1 and pb.lookup('+36203214321') == 'Jane Doe'


def test_bridge_mic_to_8k_and_remote_back_to_recorder_rate():
    b = CallAudio(use_devices=False)
    b.activate()
    t = np.arange(48000) / 48000.0
    mic = (0.5 * np.sin(2 * np.pi * 500 * t)).astype(np.float32)
    for i in range(0, 4800, 480):
        b.feed_mic(mic[i:i + 480], 48000)
    got = np.concatenate([b.read_mic(160) for _ in range(3)])
    assert len(got) == 480 and np.abs(got).max() > 8000
    # remote: 8 kHz frames in, recorder-rate float samples out
    tone = (8000 * np.sin(2 * np.pi * 1000 * np.arange(1600) / 8000.0)).astype(np.int16)
    for i in range(0, 1600, 160):
        b.play(tone[i:i + 160])
    out = np.concatenate([b.get_remote(480, 48000) for _ in range(10)])
    assert len(out) == 4800 and np.abs(out).max() > 0.15
    spec = np.abs(np.fft.rfft(out[1000:4000] * np.hanning(3000)))
    assert abs(np.argmax(spec) * 48000 / 3000 - 1000) < 30
    b.deactivate()
    assert b.get_remote(480, 48000) is None


def test_bridge_ignores_mic_when_custom_device_is_used():
    b = CallAudio(in_device=5, use_devices=False)
    b.activate()
    b.feed_mic(np.full(480, 0.5, dtype=np.float32), 48000)
    assert not b.read_mic(160).any()
