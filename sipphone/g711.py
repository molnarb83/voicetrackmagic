"""G.711 (A-law / u-law) codec, numpy based, no external dependencies.

The lookup tables are generated at import time from the public-domain
reference algorithms (CCITT G.711), so the output is standard compliant.
"""
import numpy as np

_ULAW_SEG_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _search(val, table):
    for i, end in enumerate(table):
        if val <= end:
            return i
    return len(table)


def _linear2ulaw(pcm):
    pcm >>= 2
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    if pcm > 8159:
        pcm = 8159
    pcm += 0x84 >> 2
    seg = _search(pcm, _ULAW_SEG_END)
    if seg >= 8:
        return 0x7F ^ mask
    return (((seg << 4) | ((pcm >> (seg + 1)) & 0xF))) ^ mask


def _ulaw2linear(u):
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)


def _linear2alaw(pcm):
    pcm >>= 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = _search(pcm, _ALAW_SEG_END)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm >> 1) & 0xF
    else:
        aval |= (pcm >> seg) & 0xF
    return aval ^ mask


def _alaw2linear(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & 0x80) else -t


_ULAW_ENC = np.array([_linear2ulaw(i - 32768) for i in range(65536)], dtype=np.uint8)
_ALAW_ENC = np.array([_linear2alaw(i - 32768) for i in range(65536)], dtype=np.uint8)
_ULAW_DEC = np.array([_ulaw2linear(i) for i in range(256)], dtype=np.int16)
_ALAW_DEC = np.array([_alaw2linear(i) for i in range(256)], dtype=np.int16)

PCMU = 0
PCMA = 8


def encode(pcm16, payload_type):
    """int16 numpy array -> G.711 bytes."""
    idx = pcm16.astype(np.int32) + 32768
    table = _ALAW_ENC if payload_type == PCMA else _ULAW_ENC
    return table[idx].tobytes()


def decode(data, payload_type):
    """G.711 bytes -> int16 numpy array."""
    arr = np.frombuffer(data, dtype=np.uint8)
    table = _ALAW_DEC if payload_type == PCMA else _ULAW_DEC
    return table[arr]


def silence_byte(payload_type):
    return 0xD5 if payload_type == PCMA else 0xFF
