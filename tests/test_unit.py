import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sipphone import g711                                   # noqa: E402
from sipphone.resample import Resampler                     # noqa: E402
from sipphone.sipmsg import (SipMessage, digest_response, display_name_of, param,   # noqa: E402
                             parse_challenge, parse_uri, split_commas, uri_of)


def test_g711_reference_values():
    assert g711.decode(bytes([0xFF, 0x00, 0x80]), g711.PCMU).tolist() == [0, -32124, 32124]
    assert g711.decode(bytes([0xD5, 0x55, 0xAA]), g711.PCMA).tolist() == [8, -8, 32256]
    assert g711.encode(np.zeros(1, np.int16), g711.PCMU) == b'\xff'
    assert g711.encode(np.zeros(1, np.int16), g711.PCMA) == b'\xd5'


def test_g711_roundtrip_error_is_small():
    t = np.arange(8000) / 8000.0
    x = (12000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    for pt in (g711.PCMU, g711.PCMA):
        y = g711.decode(g711.encode(x, pt), pt).astype(np.float64)
        err = np.sqrt(np.mean((y - x) ** 2)) / np.sqrt(np.mean(x.astype(np.float64) ** 2))
        assert err < 0.02, (pt, err)


def test_digest_rfc2617_vector():
    ch = parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                         'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    hdr = digest_response(ch, 'Mufasa', 'Circle Of Life', 'GET', '/dir/index.html', nc=1, cnonce='0a4f113b')
    assert 'response="6629fae49393a05397450978507c4ef1"' in hdr, hdr
    assert 'qop=auth' in hdr and 'nc=00000001' in hdr


def test_digest_without_qop():
    # RFC 2069 style: response = MD5(HA1:nonce:HA2)
    import hashlib
    ch = {'realm': 'r', 'nonce': 'n'}
    h = lambda s: hashlib.md5(s.encode()).hexdigest()
    exp = h('%s:n:%s' % (h('u:r:p'), h('REGISTER:sip:d')))
    assert 'response="%s"' % exp in digest_response(ch, 'u', 'p', 'REGISTER', 'sip:d')


def test_parse_request_and_compact_headers():
    raw = (b'INVITE sip:bob@example.com SIP/2.0\r\n'
           b'v: SIP/2.0/UDP 1.2.3.4:5060;branch=z9hG4bKabc;rport, SIP/2.0/UDP 5.6.7.8;branch=z9hG4bKdef\r\n'
           b'f: "Alice A" <sip:3630123@example.com>;tag=xyz\r\n'
           b't: <sip:bob@example.com>\r\n'
           b'i: abc@host\r\nCSeq: 7 INVITE\r\n'
           b'm: <sip:3630123@1.2.3.4:5060;transport=udp>\r\n'
           b'Record-Route: <sip:p1;lr>, <sip:p2;lr>\r\n'
           b'l: 4\r\n\r\ntest')
    m = SipMessage.parse(raw)
    assert m.method == 'INVITE' and m.cseq == (7, 'INVITE') and m.call_id == 'abc@host'
    assert len(m.get_all('via')) == 2 and m.top_branch() == 'z9hG4bKabc'
    assert param(m.get('from'), 'tag') == 'xyz'
    assert display_name_of(m.get('from')) == 'Alice A'
    assert parse_uri(uri_of(m.get('from')))['user'] == '3630123'
    assert len(m.get_all('record-route')) == 2
    assert m.body == b'test'
    back = SipMessage.parse(m.to_bytes())
    assert back.body == b'test' and len(back.get_all('via')) == 2


def test_parse_response_and_split_commas():
    m = SipMessage.parse(b'SIP/2.0 401 Unauthorized\r\nCSeq: 1 REGISTER\r\n\r\n')
    assert not m.is_request and m.status == 401 and m.cseq == (1, 'REGISTER')
    assert split_commas('"a, b" <sip:x,y@z>, <sip:q>') == ['"a, b" <sip:x,y@z>', '<sip:q>']


def test_resampler_preserves_tone_and_length():
    r_down, r_up = Resampler(48000, 8000), Resampler(8000, 48000)
    t = np.arange(48000) / 48000.0
    x = (10000 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
    out = np.concatenate([r_down.process(x[i:i + 480]) for i in range(0, len(x), 480)])
    assert abs(len(out) - 8000) <= 3
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    assert abs(np.argmax(spec) * 8000 / len(out) - 1000) < 15
    up = np.concatenate([r_up.process(out[i:i + 160]) for i in range(0, len(out), 160)])
    assert abs(len(up) - 48000) <= 20
    spec = np.abs(np.fft.rfft(up * np.hanning(len(up))))
    assert abs(np.argmax(spec) * 48000 / len(up) - 1000) < 20
