"""SIP message parsing/building and digest authentication (RFC 3261 / 7616)."""
import hashlib
import os
import re

COMPACT = {
    'v': 'via', 'f': 'from', 't': 'to', 'i': 'call-id', 'm': 'contact',
    'l': 'content-length', 'c': 'content-type', 'k': 'supported',
}
MULTI = ('via', 'record-route', 'route')


def new_branch():
    return 'z9hG4bK' + os.urandom(6).hex()


def new_tag():
    return os.urandom(4).hex()


def new_call_id(host):
    return os.urandom(8).hex() + '@' + host


def split_commas(value):
    """Split a header value at commas that are outside <> and quotes."""
    out, cur, depth, quoted = [], [], 0, False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch == '<':
            depth += 1
        elif not quoted and ch == '>':
            depth = max(0, depth - 1)
        if ch == ',' and not quoted and depth == 0:
            out.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    last = ''.join(cur).strip()
    if last:
        out.append(last)
    return out


class SipMessage:
    def __init__(self):
        self.is_request = True
        self.method = None
        self.uri = None
        self.status = None
        self.reason = ''
        self.headers = []          # [(name, value)], one entry per value
        self.body = b''

    # ── parsing ────────────────────────────────────────────────────────────
    @staticmethod
    def parse(data):
        head, sep, body = data.partition(b'\r\n\r\n')
        if not sep:
            head, sep, body = data.partition(b'\n\n')
        text = head.decode('utf-8', 'replace').replace('\r\n', '\n')
        lines = []
        for ln in text.split('\n'):
            if ln[:1] in (' ', '\t') and lines:
                lines[-1] += ' ' + ln.strip()
            else:
                lines.append(ln)
        if not lines or not lines[0].strip():
            raise ValueError('empty message')
        m = SipMessage()
        first = lines[0].strip()
        if first.upper().startswith('SIP/2.0'):
            parts = first.split(None, 2)
            m.is_request = False
            m.status = int(parts[1])
            m.reason = parts[2] if len(parts) > 2 else ''
        else:
            parts = first.split()
            if len(parts) != 3 or not parts[2].upper().startswith('SIP/'):
                raise ValueError('bad start line: %r' % first)
            m.method, m.uri = parts[0].upper(), parts[1]
        for ln in lines[1:]:
            if ':' not in ln:
                continue
            name, value = ln.split(':', 1)
            name = name.strip().lower()
            name = COMPACT.get(name, name)
            value = value.strip()
            if name in MULTI or name == 'contact':
                for v in split_commas(value):
                    m.headers.append((name, v))
            else:
                m.headers.append((name, value))
        clen = m.get('content-length')
        if clen and clen.isdigit():
            body = body[:int(clen)]
        m.body = body
        return m

    # ── header access ──────────────────────────────────────────────────────
    def get(self, name, default=None):
        name = name.lower()
        for n, v in self.headers:
            if n == name:
                return v
        return default

    def get_all(self, name):
        name = name.lower()
        return [v for n, v in self.headers if n == name]

    def add(self, name, value):
        self.headers.append((name.lower(), value))

    @property
    def cseq(self):
        parts = (self.get('cseq') or '0 X').split()
        return int(parts[0]), parts[1].upper() if len(parts) > 1 else ''

    @property
    def call_id(self):
        return self.get('call-id')

    def top_branch(self):
        via = self.get('via') or ''
        return param(via, 'branch')

    # ── building ───────────────────────────────────────────────────────────
    def to_bytes(self):
        if self.is_request:
            start = '%s %s SIP/2.0' % (self.method, self.uri)
        else:
            start = 'SIP/2.0 %d %s' % (self.status, self.reason)
        lines = [start]
        for n, v in self.headers:
            if n == 'content-length':
                continue
            lines.append('%s: %s' % (_pretty(n), v))
        lines.append('Content-Length: %d' % len(self.body))
        return ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8') + self.body


def _pretty(name):
    special = {'call-id': 'Call-ID', 'cseq': 'CSeq', 'www-authenticate': 'WWW-Authenticate'}
    if name in special:
        return special[name]
    return '-'.join(p.capitalize() for p in name.split('-'))


# ── header value helpers ───────────────────────────────────────────────────
def param(value, name):
    """Return the value of ;name=xxx in a header value (or '' if flag, None if absent)."""
    if not value:
        return None
    # strip a <...> uri part so uri params are not confused with header params
    tail = value
    if '>' in value:
        tail = value[value.rfind('>') + 1:]
        # for Via/other headers without <>, tail == value
    for part in tail.split(';')[1:] if '>' in value else value.split(';')[1:]:
        k, _, v = part.partition('=')
        if k.strip().lower() == name.lower():
            return v.strip().strip('"')
    return None


def uri_of(value):
    """Extract the bare URI from a name-addr / addr-spec header value."""
    if not value:
        return ''
    m = re.search(r'<([^>]*)>', value)
    if m:
        return m.group(1)
    return value.split(';')[0].strip()


def display_name_of(value):
    if not value:
        return ''
    m = re.match(r'\s*"([^"]*)"', value)
    if m:
        return m.group(1)
    m = re.match(r'\s*([^<"]+?)\s*<', value)
    return m.group(1).strip() if m else ''


def parse_uri(uri):
    """sip:user@host:port;params -> dict(scheme,user,host,port,params)."""
    m = re.match(r'(?i)(sips?):(?:([^@;]*)@)?(\[[^\]]+\]|[^:;>?]+)(?::(\d+))?', uri.strip())
    if not m:
        return {'scheme': 'sip', 'user': '', 'host': uri, 'port': None, 'params': ''}
    return {
        'scheme': m.group(1).lower(), 'user': m.group(2) or '',
        'host': m.group(3), 'port': int(m.group(4)) if m.group(4) else None,
        'params': uri[m.end():],
    }


# ── digest authentication ──────────────────────────────────────────────────
def parse_challenge(value):
    v = value.strip()
    if not v.lower().startswith('digest'):
        raise ValueError('unsupported auth scheme: %s' % v[:20])
    out = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]*))', v[6:]):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def _hash(algo):
    a = algo.upper().replace('-SESS', '')
    if a == 'MD5':
        return lambda s: hashlib.md5(s.encode('utf-8')).hexdigest()
    if a in ('SHA-256', 'SHA256'):
        return lambda s: hashlib.sha256(s.encode('utf-8')).hexdigest()
    raise ValueError('unsupported digest algorithm: %s' % algo)


def digest_response(ch, username, password, method, uri, nc=1, cnonce=None):
    """Return the value for an Authorization / Proxy-Authorization header."""
    algo = ch.get('algorithm', 'MD5')
    h = _hash(algo)
    realm, nonce = ch['realm'], ch['nonce']
    qops = [q.strip() for q in ch.get('qop', '').split(',') if q.strip()]
    qop = 'auth' if 'auth' in qops else None
    cnonce = cnonce or os.urandom(6).hex()
    ha1 = h('%s:%s:%s' % (username, realm, password))
    if algo.upper().endswith('-SESS'):
        ha1 = h('%s:%s:%s' % (ha1, nonce, cnonce))
    ha2 = h('%s:%s' % (method, uri))
    ncs = '%08x' % nc
    if qop:
        resp = h('%s:%s:%s:%s:%s:%s' % (ha1, nonce, ncs, cnonce, qop, ha2))
    else:
        resp = h('%s:%s:%s' % (ha1, nonce, ha2))
    parts = ['username="%s"' % username, 'realm="%s"' % realm, 'nonce="%s"' % nonce,
             'uri="%s"' % uri, 'response="%s"' % resp, 'algorithm=%s' % algo]
    if qop:
        parts += ['qop=%s' % qop, 'cnonce="%s"' % cnonce, 'nc=%s' % ncs]
    if 'opaque' in ch:
        parts.append('opaque="%s"' % ch['opaque'])
    return 'Digest ' + ', '.join(parts)
