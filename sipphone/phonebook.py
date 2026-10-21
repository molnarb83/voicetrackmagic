"""Local phonebook: name lookup for incoming numbers, vCard / CSV import."""
import csv
import json
import os
import re


def normalize(number):
    """Digits only; '+36 30 123 4567' and '0036301234567' -> '36301234567'."""
    digits = re.sub(r'\D', '', number or '')
    if digits.startswith('00'):
        digits = digits[2:]
    return digits


def match_key(number):
    """Numbers are compared on their last 9 digits so that 06 30 / +36 30 / 0036 30 all match."""
    d = normalize(number)
    return d[-9:] if len(d) >= 9 else d


class Phonebook:
    def __init__(self, path):
        self.path = path
        self.entries = []          # [{'name': str, 'number': str}]
        self.load()

    # ── storage ────────────────────────────────────────────────────────────
    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            self.entries = [{'name': str(e.get('name', '')).strip(), 'number': str(e.get('number', '')).strip()}
                            for e in data if isinstance(e, dict) and e.get('number')]
        except (OSError, ValueError):
            self.entries = []

    def save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.entries, f, ensure_ascii=False, indent=2)

    # ── editing / lookup ───────────────────────────────────────────────────
    def lookup(self, number):
        k = match_key(number)
        if not k:
            return ''
        for e in self.entries:
            if match_key(e['number']) == k:
                return e['name']
        return ''

    def add(self, name, number):
        number = (number or '').strip()
        if not match_key(number):
            return False
        k = match_key(number)
        for e in self.entries:
            if match_key(e['number']) == k:
                e['name'] = (name or '').strip() or e['name']
                e['number'] = number
                return True
        self.entries.append({'name': (name or '').strip(), 'number': number})
        return True

    def remove(self, index):
        if 0 <= index < len(self.entries):
            del self.entries[index]
            return True
        return False

    def sorted_entries(self):
        return sorted(self.entries, key=lambda e: (e['name'].lower(), e['number']))

    # ── import ─────────────────────────────────────────────────────────────
    @staticmethod
    def _read_text(path):
        raw = open(path, 'rb').read()
        for enc in ('utf-8-sig', 'cp1250', 'latin-1'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', 'replace')

    def import_vcf(self, path):
        text = self._read_text(path)
        text = re.sub(r'\r?\n[ \t]', '', text)               # unfold lines
        count = 0
        for card in re.findall(r'BEGIN:VCARD(.*?)END:VCARD', text, flags=re.S | re.I):
            name, tels = '', []
            for line in card.splitlines():
                if ':' not in line:
                    continue
                key, _, val = line.partition(':')
                base = key.split(';')[0].strip().upper()
                if base == 'FN' and not name:
                    name = val.strip()
                elif base == 'N' and not name:
                    parts = [p for p in val.split(';') if p.strip()]
                    name = ' '.join(reversed(parts[:2])).strip() if parts else ''
                elif base.endswith('TEL'):                    # TEL or item1.TEL
                    tels.append(val.strip())
            for t in tels:
                if self.add(name, t):
                    count += 1
        return count

    def import_csv(self, path):
        text = self._read_text(path)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(text.splitlines(), dialect))
        if not rows:
            return 0
        head = [h.strip().lower() for h in rows[0]]

        def idx(*needles):
            return [i for i, h in enumerate(head) if any(n in h for n in needles)]

        name_cols = idx('display name', 'full name', 'név', 'nev')
        if not name_cols:
            name_cols = [i for i, h in enumerate(head) if h in ('name', 'contact')]
        first = idx('first name', 'given name', 'keresztnév', 'utónév')
        last = idx('last name', 'family name', 'vezetéknév', 'családi')
        tel_cols = [i for i in idx('phone', 'tel', 'mobil', 'szám', 'szam', 'number') if i not in name_cols]
        has_header = bool(name_cols or first or last or tel_cols)
        data = rows[1:] if has_header else rows
        if not has_header:
            name_cols, tel_cols = [0], [1] if len(rows[0]) > 1 else []
        count = 0
        for r in data:
            def cell(i):
                return r[i].strip() if i < len(r) else ''
            name = ' '.join(cell(i) for i in name_cols if cell(i)) if name_cols else ''
            if not name:
                name = ' '.join(x for x in [cell(i) for i in first + last] if x)
            for i in tel_cols:
                val = cell(i)
                for part in re.split(r'\s*:::\s*|\s*;\s*', val):       # Google packs several numbers in one cell
                    if len(normalize(part)) >= 5 and self.add(name, part):
                        count += 1
        return count
