"""PyQt5 widgets for the SIP phone: settings tab, phonebook, call manager window, main-window strip."""
import os

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

C_BG, C_CARD, C_BORDER = "#F0F2F5", "#FFFFFF", "#E2E8F0"
C_TEXT, C_MUTED = "#1E293B", "#64748B"
C_ACCENT, C_GREEN, C_RED, C_ORANGE = "#2563EB", "#16A34A", "#DC2626", "#EA580C"

LABEL_CSS = "font-size: 12px; font-weight: 600; color: %s;" % C_MUTED


def _btn(text, color, hover=None, small=True):
    b = QPushButton(text)
    pad = "4px 10px" if small else "10px 14px"
    b.setStyleSheet(
        "QPushButton{background:%s;color:white;font-weight:700;font-size:12px;border:none;"
        "border-radius:6px;padding:%s;min-height:18px;}"
        "QPushButton:hover{background:%s;}QPushButton:disabled{background:#CBD5E1;color:#94A3B8;}"
        % (color, pad, hover or color))
    return b


def resolve_device(devices, name):
    """devices: list of dicts with 'index' and 'name' (recorder.get_*_devices()). '' / unknown -> None."""
    if not name:
        return None
    for d in devices:
        if d['name'] == name:
            return d['index']
    return None


class PhoneSignals(QObject):
    """Marshals SIP engine events (worker threads) to the GUI thread."""
    event = pyqtSignal(str, object, object)


# ─── Settings tab ─────────────────────────────────────────────────────────────
class PhoneSettingsTab(QWidget):
    def __init__(self, recorder, phonebook_path, parent=None):
        super().__init__(parent)
        self.recorder = recorder
        self.phonebook_path = phonebook_path
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self.enable_cb = QCheckBox("Enable SIP phone (receive and make calls, record them)")
        lay.addWidget(self.enable_cb)

        acc = QGroupBox("SIP account")
        f = QFormLayout(acc)
        f.setSpacing(8)
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.domain = QLineEdit()
        self.domain.setPlaceholderText("e.g. sip.kompaas.tech")
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("optional - only if different from the domain")
        self.port = QLineEdit("5060")
        self.port.setFixedWidth(70)
        self.auth_user = QLineEdit()
        self.auth_user.setPlaceholderText("optional - if different from the user name")
        self.display_name = QLineEdit()
        self.display_name.setPlaceholderText("optional - shown to the called party")
        for text, w in (("User:", self.user), ("Password:", self.password), ("Domain / server:", self.domain),
                        ("Outbound proxy:", self.proxy), ("Port:", self.port),
                        ("Auth user:", self.auth_user), ("Display name:", self.display_name)):
            l = QLabel(text)
            l.setStyleSheet(LABEL_CSS)
            f.addRow(l, w)
        lay.addWidget(acc)

        aud = QGroupBox("Audio")
        g = QFormLayout(aud)
        g.setSpacing(8)
        self.in_combo, self.out_combo, self.ring_combo = QComboBox(), QComboBox(), QComboBox()
        self.in_combo.addItem("Same as the recording input", "")
        self.out_combo.addItem("Same as the Output device setting", "")
        self.ring_combo.addItem("Silent (visual only)", "")
        try:
            for d in recorder.get_input_devices():
                self.in_combo.addItem(d['name'], d['name'])
            for d in recorder.get_output_devices():
                self.out_combo.addItem(d['name'], d['name'])
                self.ring_combo.addItem(d['name'], d['name'])
        except Exception as exc:                       # no audio system (e.g. headless test)
            print("phone settings: cannot list devices: %r" % (exc,))
        self.in_combo.setToolTip("What the caller hears. By default the same signal that is recorded.")
        self.out_combo.setToolTip("Where YOU hear the caller (use headphones, otherwise the caller hears an echo).")
        self.ring_combo.setToolTip("Ring tone device. Silent = no sound, so ringing never ends up in a recording.")
        for text, w in (("Caller hears (input):", self.in_combo), ("You hear caller (output):", self.out_combo),
                        ("Ring tone:", self.ring_combo)):
            l = QLabel(text)
            l.setStyleSheet(LABEL_CSS)
            g.addRow(l, w)
        lay.addWidget(aud)

        beh = QGroupBox("Behaviour")
        h = QFormLayout(beh)
        h.setSpacing(8)
        self.switch_combo = QComboBox()
        self.switch_combo.addItem("Put the previous call on hold", "hold")
        self.switch_combo.addItem("Hang up the previous call", "hangup")
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Everything on both channels (default)", False)
        self.layout_combo.addItem("Split: me on the left, caller on the right", True)
        self.lines_spin = QSpinBox()
        self.lines_spin.setRange(1, 10)
        self.lines_spin.setValue(10)
        self.auto_open_cb = QCheckBox("Open the call window on incoming calls")
        self.auto_open_cb.setChecked(True)
        for text, w in (("When switching lines:", self.switch_combo), ("Recording layout:", self.layout_combo),
                        ("Max. simultaneous calls:", self.lines_spin), ("", self.auto_open_cb)):
            l = QLabel(text)
            l.setStyleSheet(LABEL_CSS)
            h.addRow(l, w)
        lay.addWidget(beh)

        row = QHBoxLayout()
        pb = _btn("📖  Phonebook…", C_ACCENT, "#1D4ED8", small=False)
        pb.clicked.connect(self._open_phonebook)
        row.addWidget(pb)
        row.addStretch()
        lay.addLayout(row)
        note = QLabel("Calls are recorded only when you press Record. Tell the other party that the call is "
                      "being recorded where the law requires it.")
        note.setWordWrap(True)
        note.setStyleSheet("font-size:11px;color:%s;" % C_MUTED)
        lay.addWidget(note)
        lay.addStretch()

    def _open_phonebook(self):
        PhonebookDialog(self.phonebook_path, self).exec_()

    @staticmethod
    def _select(combo, data):
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def load(self, s):
        self.enable_cb.setChecked(bool(s.get('sip_enabled', False)))
        self.user.setText(s.get('sip_user', ''))
        self.password.setText(s.get('sip_pass', ''))
        self.domain.setText(s.get('sip_domain', ''))
        self.proxy.setText(s.get('sip_proxy', ''))
        self.port.setText(str(s.get('sip_port', 5060)))
        self.auth_user.setText(s.get('sip_auth_user', ''))
        self.display_name.setText(s.get('sip_display_name', ''))
        self._select(self.in_combo, s.get('sip_input_name', ''))
        self._select(self.out_combo, s.get('sip_output_name', ''))
        self._select(self.ring_combo, s.get('sip_ring_name', ''))
        self._select(self.switch_combo, s.get('sip_switch_mode', 'hold'))
        self._select(self.layout_combo, bool(s.get('sip_rec_split', False)))
        self.lines_spin.setValue(int(s.get('sip_max_calls', 10) or 10))
        self.auto_open_cb.setChecked(bool(s.get('sip_auto_open', True)))

    def collect(self, s):
        s['sip_enabled'] = self.enable_cb.isChecked()
        s['sip_user'] = self.user.text().strip()
        s['sip_pass'] = self.password.text()
        s['sip_domain'] = self.domain.text().strip()
        s['sip_proxy'] = self.proxy.text().strip()
        try:
            s['sip_port'] = int(self.port.text().strip() or 5060)
        except ValueError:
            s['sip_port'] = 5060
        s['sip_auth_user'] = self.auth_user.text().strip()
        s['sip_display_name'] = self.display_name.text().strip()
        s['sip_input_name'] = self.in_combo.currentData() or ''
        s['sip_output_name'] = self.out_combo.currentData() or ''
        s['sip_ring_name'] = self.ring_combo.currentData() or ''
        s['sip_switch_mode'] = self.switch_combo.currentData()
        s['sip_rec_split'] = bool(self.layout_combo.currentData())
        s['sip_max_calls'] = self.lines_spin.value()
        s['sip_auto_open'] = self.auto_open_cb.isChecked()


# ─── Phonebook ────────────────────────────────────────────────────────────────
class PhonebookDialog(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        from sipphone.phonebook import Phonebook
        self.book = Phonebook(path)
        self.setWindowTitle("Phonebook")
        self.setMinimumSize(460, 420)
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Number"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_select)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        self.name_in, self.num_in = QLineEdit(), QLineEdit()
        self.name_in.setPlaceholderText("Name")
        self.num_in.setPlaceholderText("Number (+36 30 123 4567)")
        row.addWidget(self.name_in)
        row.addWidget(self.num_in)
        lay.addLayout(row)

        btns = QHBoxLayout()
        add = _btn("Add / update", C_GREEN)
        rem = _btn("Delete", C_RED)
        imp = _btn("Import vCard / CSV…", C_ACCENT)
        close = _btn("Close", C_MUTED)
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        imp.clicked.connect(self._import)
        close.clicked.connect(self.accept)
        for b in (add, rem, imp):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(close)
        lay.addLayout(btns)
        self._refresh()

    def _refresh(self):
        self._rows = self.book.sorted_entries()
        self.table.setRowCount(len(self._rows))
        for i, e in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(e['name']))
            self.table.setItem(i, 1, QTableWidgetItem(e['number']))

    def _on_select(self):
        r = self.table.currentRow()
        if 0 <= r < len(self._rows):
            self.name_in.setText(self._rows[r]['name'])
            self.num_in.setText(self._rows[r]['number'])

    def _add(self):
        if not self.book.add(self.name_in.text(), self.num_in.text()):
            QMessageBox.warning(self, "Phonebook", "Please enter a valid phone number.")
            return
        self.book.save()
        self._refresh()

    def _remove(self):
        r = self.table.currentRow()
        if not (0 <= r < len(self._rows)):
            return
        target = self._rows[r]
        for i, e in enumerate(self.book.entries):
            if e is target or (e['name'] == target['name'] and e['number'] == target['number']):
                self.book.remove(i)
                break
        self.book.save()
        self._refresh()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import contacts", "", "Contacts (*.vcf *.csv);;All files (*)")
        if not path:
            return
        try:
            n = self.book.import_vcf(path) if path.lower().endswith('.vcf') else self.book.import_csv(path)
            self.book.save()
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._refresh()
        QMessageBox.information(self, "Phonebook", "%d number(s) imported." % n)


# ─── Call manager window ──────────────────────────────────────────────────────
class CallManagerDialog(QDialog):
    """Non-modal window: all lines, who is active, answer / hold / switch / hang up any of them."""
    COLS = ["Line", "Caller", "State", "Time", ""]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.controller = None
        self.setWindowTitle("Calls")
        self.setMinimumSize(760, 460)
        self.setStyleSheet("QDialog{background:%s;font-family:'Segoe UI',Arial;}" % C_BG)
        lay = QVBoxLayout(self)

        self.reg_lbl = QLabel("")
        self.reg_lbl.setStyleSheet("font-size:12px;font-weight:700;")
        lay.addWidget(self.reg_lbl)

        dial = QHBoxLayout()
        self.number_in = QLineEdit()
        self.number_in.setPlaceholderText("Number to call - type or paste (+36 30 123 4567)")
        self.number_in.returnPressed.connect(self._dial)
        self.dial_btn = _btn("📞  Call", C_GREEN, small=False)
        self.dial_btn.clicked.connect(self._dial)
        dial.addWidget(self.number_in)
        dial.addWidget(self.dial_btn)
        lay.addLayout(dial)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.setColumnWidth(4, 210)
        lay.addWidget(self.table, 1)

        box = QGroupBox("Keypad (sent to the active call)")
        grid = QGridLayout(box)
        for i, k in enumerate("123456789*0#"):
            b = _btn(k, "#475569", "#334155", small=False)
            b.setFixedWidth(56)
            b.clicked.connect(lambda _=False, d=k: self._dtmf(d))
            grid.addWidget(b, i // 6, i % 6)
        lay.addWidget(box)

        self._sig = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)

    def set_controller(self, controller):
        self.controller = controller
        self._sig = None
        self.refresh()

    # ── actions ────────────────────────────────────────────────────────────
    def _dial(self):
        if not self.controller:
            return
        num = self.number_in.text().strip()
        if not num:
            return
        try:
            self.controller.dial(num)
            self.number_in.clear()
        except Exception as exc:
            QMessageBox.warning(self, "Call", str(exc))

    def _dtmf(self, d):
        if self.controller:
            self.controller.dtmf(d)

    def _act(self, fn, line):
        if self.controller:
            try:
                fn(line)
            except Exception as exc:
                QMessageBox.warning(self, "Call", str(exc))
        self.refresh()

    # ── display ────────────────────────────────────────────────────────────
    @staticmethod
    def _who(r):
        num = r['number'] or ''
        return "%s  (%s)" % (r['name'], num) if r.get('name') and num else (r.get('name') or num or "Unknown")

    def refresh(self):
        c = self.controller
        if not c:
            self.reg_lbl.setText("SIP phone is off")
            self.reg_lbl.setStyleSheet("font-size:12px;font-weight:700;color:%s;" % C_MUTED)
            self.table.setRowCount(0)
            return
        ok = c.registered
        self.reg_lbl.setText("● Registered" if ok else "● Not registered (check the SIP settings)")
        self.reg_lbl.setStyleSheet("font-size:12px;font-weight:700;color:%s;" % (C_GREEN if ok else C_RED))
        rows = c.lines()
        sig = tuple((r['line'], r['state'], self._who(r), r['active']) for r in rows)
        if sig != self._sig:
            self._sig = sig
            self._rebuild(rows)
        for i, r in enumerate(rows):
            it = self.table.item(i, 3)
            if it:
                it.setText("%d:%02d" % divmod(r['secs'], 60))

    def _rebuild(self, rows):
        c = self.controller
        self.table.setRowCount(0)             # drops the old cell widgets (otherwise they pile up)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            state = r['state']
            ringing = r['ringing']
            held = state.startswith('held')
            if r['active']:
                bg = "#DCFCE7"
            elif ringing:
                bg = "#FEF3C7"
            else:
                bg = "#FFFFFF"
            label = {'ringing_in': 'Ringing…', 'dialing': 'Calling…', 'ringing_out': 'Ringing (remote)…',
                     'connected': 'ACTIVE' if r['active'] else 'Connected'}.get(state, state.capitalize())
            cells = [str(r['line']), self._who(r), label, "%d:%02d" % divmod(r['secs'], 60)]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setBackground(self._color(bg))
                f = it.font()
                f.setBold(bool(r['active']) or ringing)
                it.setFont(f)
                if held:
                    it.setForeground(self._color("#64748B"))
                self.table.setItem(i, col, it)
            self.table.setCellWidget(i, 4, self._buttons(r, ringing, held))
            self.table.setRowHeight(i, 40)

    @staticmethod
    def _color(hexcode):
        from PyQt5.QtGui import QColor
        return QColor(hexcode)

    def _buttons(self, r, ringing, held):
        c = self.controller
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(4)
        line = r['line']
        if ringing:
            a = _btn("Answer", C_GREEN)
            a.clicked.connect(lambda _=False, l=line: self._act(c.answer, l))
            x = _btn("Reject", C_RED)
            x.clicked.connect(lambda _=False, l=line: self._act(c.reject, l))
            h.addWidget(a)
            h.addWidget(x)
        elif r['state'] in ('dialing', 'ringing_out'):
            x = _btn("Cancel", C_RED)
            x.clicked.connect(lambda _=False, l=line: self._act(c.hangup, l))
            h.addWidget(x)
        else:
            if r['active']:
                hb = _btn("Hold", C_ORANGE)
                hb.clicked.connect(lambda _=False, l=line: self._act(c.hold, l))
                h.addWidget(hb)
            else:
                sw = _btn("Switch to", C_ACCENT)
                sw.clicked.connect(lambda _=False, l=line: self._act(c.switch_to, l))
                h.addWidget(sw)
            x = _btn("Hang up", C_RED)
            x.clicked.connect(lambda _=False, l=line: self._act(c.hangup, l))
            h.addWidget(x)
        return w


# ─── Main window strip ────────────────────────────────────────────────────────
class PhoneStrip(QWidget):
    open_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.lbl = QLabel("📞 SIP: off")
        self.lbl.setStyleSheet("font-size:12px;font-weight:700;color:%s;" % C_MUTED)
        self.btn = _btn("Calls…", C_ACCENT, "#1D4ED8")
        self.btn.clicked.connect(self.open_requested.emit)
        h.addWidget(self.lbl, 1)
        h.addWidget(self.btn)

    def update_state(self, controller):
        if controller is None:
            self.lbl.setText("📞 SIP: off")
            self.lbl.setStyleSheet("font-size:12px;font-weight:700;color:%s;" % C_MUTED)
            return
        rows = controller.lines()
        ringing = sum(1 for r in rows if r['ringing'])
        talking = [r for r in rows if r['active']]
        if not controller.registered:
            text, color = "📞 SIP: not registered", C_RED
        elif ringing:
            text, color = "📞 %d incoming call%s" % (ringing, "s" if ringing > 1 else ""), C_ORANGE
        elif talking:
            text, color = "📞 In call: %s" % (talking[0]['name'] or talking[0]['number'] or "line %d" % talking[0]['line']), C_GREEN
        else:
            text, color = "📞 SIP: ready", C_GREEN
        if len(rows) - ringing - len(talking) > 0 and not ringing:
            text += "  (+%d held)" % (len(rows) - len(talking))
        self.lbl.setText(text)
        self.lbl.setStyleSheet("font-size:12px;font-weight:700;color:%s;" % color)
