"""
VoiceTrack Magic - Voice Recorder for Radio DJs
"""

import sys
import os
import re
import glob
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QLabel, QLineEdit,
    QProgressBar, QFileDialog, QMessageBox, QDialog, QTabWidget,
    QGroupBox, QShortcut, QFrame, QListWidget, QListWidgetItem
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QRect
from PyQt5.QtGui import QKeySequence, QPainter, QPen, QColor, QIcon

from modules.recorder import AudioRecorder
from modules.audio_processor import AudioProcessor
from modules.config_manager import ConfigManager
from modules.path_manager import PathManager
from modules.phone_ui import (PhoneSettingsTab, CallManagerDialog, PhoneStrip, PhoneSignals,
                              resolve_device)


def _patch_pydub_no_window():
    """Patch pydub to never open console windows on Windows. Call once at startup."""
    if sys.platform != "win32":
        return
    try:
        import subprocess
        import pydub.utils as _pu
        _CREATE_NO_WINDOW = 0x08000000

        class _NWPopen(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault('creationflags', 0)
                kwargs['creationflags'] |= _CREATE_NO_WINDOW
                super().__init__(*args, **kwargs)

        subprocess.Popen = _NWPopen
        _pu.Popen = _NWPopen
        print("Popen patched: no console windows for pydub/ffmpeg")
    except Exception as e:
        print(f"Warning: Could not patch pydub Popen: {e}")


_patch_pydub_no_window()


def get_app_icon():
    """Load icon.png from the app directory (works both in dev and PyInstaller)."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base, 'icon.png')
    if os.path.exists(icon_path):
        return QIcon(icon_path)
    return QIcon()


# ─── Palette ─────────────────────────────────────────────────────────────────
C_BG         = "#F0F2F5"
C_CARD       = "#FFFFFF"
C_ACCENT     = "#2563EB"
C_ACCENT_H   = "#1D4ED8"
C_RED        = "#DC2626"
C_RED_H      = "#B91C1C"
C_GREEN      = "#16A34A"
C_GREEN_H    = "#15803D"
C_ORANGE     = "#EA580C"
C_PURPLE     = "#7C3AED"
C_PURPLE_H   = "#6D28D9"
C_BORDER     = "#E2E8F0"
C_TEXT       = "#1E293B"
C_MUTED      = "#64748B"


def btn_style(bg, hover, fg="white", extra=""):
    return f"""
        QPushButton {{
            background: {bg}; color: {fg}; font-weight: 700; font-size: 14px;
            border: none; border-radius: 7px; padding: 11px 8px; min-height: 20px;
            {extra}
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:disabled {{ background: #CBD5E1; color: #94A3B8; }}
    """


LABEL_CSS = f"font-size: 12px; font-weight: 600; color: {C_MUTED};"
INPUT_CSS = f"""
    QLineEdit, QComboBox {{
        padding: 7px 10px; font-size: 13px;
        border: 1px solid {C_BORDER}; border-radius: 5px;
        background: {C_CARD}; color: {C_TEXT}; min-height: 16px;
    }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {C_ACCENT}; }}
"""


# ─── Waveform widget ──────────────────────────────────────────────────────────
class WaveformWidget(QWidget):
    """Waveform display with click/drag seek support."""

    # Emits 0.0–1.0 seek position
    seek_requested = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples_raw  = None
        self.samples_proc = None
        self._playhead    = None   # 0.0–1.0 or None
        self._dragging    = False
        self.setMinimumHeight(90)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"background:{C_CARD};border:1px solid {C_BORDER};border-radius:6px;")

    def set_raw(self, s):
        self.samples_raw  = s
        self.samples_proc = None   # clear processed when new raw arrives
        self._playhead    = None
        self.update()

    def set_processed(self, s):
        # Replace raw with processed — raw is no longer playable after export
        self.samples_raw  = None
        self.samples_proc = s
        self._playhead    = None
        self.update()
    def set_playhead(self, pos): self._playhead = pos; self.update()
    def clear(self):
        self.samples_raw = self.samples_proc = None
        self._playhead = None
        self.update()

    def _content_rect(self):
        return self.rect().adjusted(4, 4, -4, -4)

    def _pos_to_fraction(self, x):
        r = self._content_rect()
        return max(0.0, min(1.0, (x - r.left()) / max(r.width(), 1)))

    # ── Mouse events ──────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._has_content():
            self._dragging = True
            frac = self._pos_to_fraction(event.x())
            self._playhead = frac
            self.seek_requested.emit(frac)
            self.update()

    def mouseMoveEvent(self, event):
        if self._dragging and self._has_content():
            frac = self._pos_to_fraction(event.x())
            self._playhead = frac
            self.seek_requested.emit(frac)
            self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def _has_content(self):
        return self.samples_raw is not None or self.samples_proc is not None

    # ── Drawing ───────────────────────────────────────────────────────────────
    def _draw_wave(self, painter, samples, color, rect):
        import numpy as np
        w, h = rect.width(), rect.height()
        mid  = rect.top() + h // 2
        painter.setPen(QPen(QColor(color), 1))
        n    = len(samples)
        step = max(1, n // max(w, 1))
        cols = min(w, n // step)
        for x in range(cols):
            chunk = samples[x*step:(x+1)*step]
            if len(chunk) == 0: continue
            peak = float(np.max(np.abs(chunk)))
            amp  = int(peak * (h // 2 - 6))
            painter.drawLine(rect.left()+x, mid-amp, rect.left()+x, mid+amp)

    def _draw_playhead(self, painter, rect, frac):
        x = rect.left() + int(frac * rect.width())
        painter.setPen(QPen(QColor(C_ORANGE), 2))
        painter.drawLine(x, rect.top(), x, rect.bottom())
        # Small triangle handle at top
        painter.setBrush(QColor(C_ORANGE))
        painter.setPen(Qt.NoPen)
        from PyQt5.QtGui import QPolygon
        from PyQt5.QtCore import QPoint
        tri = QPolygon([QPoint(x-5, rect.top()),
                        QPoint(x+5, rect.top()),
                        QPoint(x,   rect.top()+10)])
        painter.drawPolygon(tri)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self._content_rect()

        if not self._has_content():
            painter.setPen(QColor(C_MUTED))
            painter.drawText(rect, Qt.AlignCenter, "No recording")
            return

        f = painter.font(); f.setPointSize(7); painter.setFont(f)

        if self.samples_raw is not None and self.samples_proc is not None:
            hw = rect.width()//2 - 2
            lr = QRect(rect.left(),       rect.top(), hw, rect.height())
            rr = QRect(rect.left()+hw+4,  rect.top(), hw, rect.height())
            self._draw_wave(painter, self.samples_raw,  C_ACCENT, lr)
            self._draw_wave(painter, self.samples_proc, C_GREEN,  rr)
            painter.setPen(QColor(C_BORDER))
            cx = rect.left()+hw+2
            painter.drawLine(cx, rect.top(), cx, rect.bottom())
            painter.setPen(QColor(C_MUTED))
            painter.drawText(QRect(lr.left(),lr.top(),lr.width(),12), Qt.AlignLeft, "Raw")
            painter.drawText(QRect(rr.left(),rr.top(),rr.width(),12), Qt.AlignLeft, "Processed")
            # Playhead only on active (processed) side when present
            if self._playhead is not None:
                self._draw_playhead(painter, rr, self._playhead)
        elif self.samples_raw is not None:
            self._draw_wave(painter, self.samples_raw, C_ACCENT, rect)
            painter.setPen(QColor(C_MUTED))
            painter.drawText(QRect(rect.left(),rect.top(),rect.width(),12), Qt.AlignLeft, "Raw recording")
            if self._playhead is not None:
                self._draw_playhead(painter, rect, self._playhead)
        else:
            self._draw_wave(painter, self.samples_proc, C_GREEN, rect)
            painter.setPen(QColor(C_MUTED))
            painter.drawText(QRect(rect.left(),rect.top(),rect.width(),12), Qt.AlignLeft, "Processed")
            if self._playhead is not None:
                self._draw_playhead(painter, rect, self._playhead)


# ─── Processing thread ────────────────────────────────────────────────────────
FILTER_PRESETS = {
    "None":           "",
    "Studio Vocal":   "highpass=f=80, equalizer=f=7000:width_type=h:width=4000:g=-4, acompressor=threshold=-25dB:ratio=3:attack=1:release=10:makeup=1, acompressor=threshold=-20dB:ratio=2:attack=5:release=50:makeup=2, loudnorm=I=-16:TP=-1.5:LRA=11",
    "Announcer":      "highpass=f=100, equalizer=f=150:width_type=h:width=100:g=3, equalizer=f=4000:width_type=h:width=2000:g=4, acompressor=threshold=-15dB:ratio=5:attack=2:release=50:makeup=5, loudnorm=I=-12:TP=-1.0",
    "Interview":      "highpass=f=80, agate=threshold=0.02:attack=10:release=100, dynaudnorm=p=0.9:m=10:s=0, loudnorm=I=-16:TP=-1.5",
    "Narration":      "highpass=f=60, equalizer=f=120:width_type=h:width=50:g=2, equalizer=f=7000:width_type=h:width=4000:g=-3, acompressor=threshold=-25dB:ratio=3:attack=1:release=10:makeup=1, acompressor=threshold=-20dB:ratio=3:attack=15:release=100:makeup=3, loudnorm=I=-18:TP=-2.0",
    "Broadcast":      "equalizer=f=60:width_type=h:width=40:g=2, equalizer=f=8000:width_type=h:width=3000:g=2, acompressor=threshold=-15dB:ratio=4:attack=5:release=50:makeup=4, loudnorm=I=-10:TP=-1.0:LRA=5",
    "Music Master":   "equalizer=f=100:width_type=o:width=1:g=1, equalizer=f=10000:width_type=o:width=1:g=1, acompressor=threshold=-20dB:ratio=2:attack=20:release=200:makeup=2, loudnorm=I=-14:TP=-1.0",
    "Banger":         "equalizer=f=70:width_type=h:width=50:g=5, acompressor=threshold=-18dB:ratio=3:attack=15:release=60:makeup=3, loudnorm=I=-11:TP=-1.0",
    "Normal Voice":   "compand=attacks=0.01:decays=0.2:points=-80/-80|-30/-15|-10/-7|0/-3,dynaudnorm=f=220:g=15:m=15:p=0.95,alimiter=limit=0.98",
    "Smooth Talk":    "compand=attacks=0.02:decays=0.3:points=-70/-70|-30/-12|-10/-6|0/-3,dynaudnorm=f=275:g=12:m=12:p=0.85,alimiter=limit=0.98",
    "Warmth Podcast": "equalizer=f=300:t=q:w=1:g=3,equalizer=f=2500:t=q:w=1:g=4,compand=attacks=0.01:decays=0.2:points=-80/-80|-30/-15|-10/-7|0/-3,dynaudnorm=f=220:g=15:m=15:p=0.95,alimiter=limit=0.98",
    "Custom":         "__custom__",
}

MP3_BITRATES = ["64k", "128k", "192k", "320k"]


class ProcessingThread(QThread):
    finished          = pyqtSignal(str, object, dict)   # path, samples, log_data
    error             = pyqtSignal(str)
    progress          = pyqtSignal(str)
    file_exists_warn  = pyqtSignal(str, str)
    filter_failed     = pyqtSignal(str, str)            # (preset_name, error_msg)

    def __init__(self, processor, input_file, settings):
        super().__init__()
        self.processor            = processor
        self.input_file           = input_file
        self.settings             = settings
        self._allow_overwrite     = False
        self._skip_filter         = False   # set True to retry without filter

    def allow_overwrite(self):  self._allow_overwrite = True
    def skip_filter(self):      self._skip_filter = True

    def _next_num(self, base, save_path, ext, separator="_", start=1, width=3):
        """Compute the next sequence number.

        Combines two behaviors:
          - starts at `start` when no matching files exist yet
          - otherwise continues from (highest existing number + 1),
            never going below `start`
        `separator` may be an empty string (e.g. base "CJ06" + "01" -> "CJ0601").
        """
        pattern  = os.path.join(save_path, f"{glob.escape(base)}{separator}*.{ext}")
        existing = glob.glob(pattern)
        nums = []
        sep_pattern = re.escape(separator) if separator else ""
        rx = re.compile(rf"^{re.escape(base)}{sep_pattern}(\d+)$")
        for fp in existing:
            name = os.path.splitext(os.path.basename(fp))[0]
            m = rx.match(name)
            if m:
                nums.append(int(m.group(1)))
        highest_next = (max(nums) + 1) if nums else start
        return max(start, highest_next)

    def run(self):
        try:
            s    = self.settings
            log  = {}   # collects data for the UI log panel

            self.progress.emit("Trimming silence...")
            try:
                threshold_db = int(s.get('threshold', '-53dB').replace('dB',''))
            except ValueError:
                threshold_db = -53
            log['threshold'] = f"{threshold_db} dB"

            preset = s.get('filter_preset', 'None')
            filter_str = FILTER_PRESETS.get(preset, '')
            if preset == 'Custom':
                filter_str = s.get('custom_filter', '')
            log['filter'] = preset if preset != 'Custom' else f"Custom: {filter_str[:60]}..."
            if preset not in FILTER_PRESETS and preset != 'None':
                log['filter'] = f"{preset} (preset not found - saved without filter)"
                log['filter_error'] = "preset not found"

            self.progress.emit("Applying audio filters...")
            fmt      = s.get('format', 'WAV')
            out_fmt  = 'mp3' if 'MP3' in fmt else 'wav'
            bitrate  = s.get('mp3_bitrate', '320k')
            log['format']  = f"{fmt}" + (f" {bitrate}" if out_fmt == 'mp3' else "")
            log['intro_padding'] = f"{s.get('intro_padding', 50)} ms"
            log['outro_padding'] = f"{s.get('outro_padding', 50)} ms"

            # Get raw duration before processing
            try:
                from pydub import AudioSegment as _AS
                raw_seg = _AS.from_wav(self.input_file)
                log['raw_duration'] = f"{len(raw_seg) / 1000:.2f} s"
            except Exception:
                log['raw_duration'] = "?"

            # If _skip_filter was set (user chose "save without filter"), clear filter
            if self._skip_filter:
                filter_str = ''
                log['filter'] = 'None (skipped after error)'

            processed = self.processor.process_audio(
                self.input_file,
                threshold_db    = threshold_db,
                intro_padding_ms= s.get('intro_padding', 50),
                outro_padding_ms= s.get('outro_padding',  50),
                filter_str      = filter_str,
                output_format   = out_fmt,
                bitrate         = bitrate,
            )

            # Check if filter failed — stop and ask user what to do
            trim_info = getattr(self.processor, '_last_trim_info', {})
            if not trim_info.get('filter_ok', True) and not self._skip_filter:
                err_msg = trim_info.get('filter_error', 'Unknown error')
                log['filter_ok']    = False
                log['filter_error'] = err_msg
                self.filter_failed.emit(preset, err_msg)
                return

            if not processed:
                self.error.emit("Audio processing failed")
                return

            # Get trim info from processor
            trim_info = getattr(self.processor, '_last_trim_info', {})
            log['trimmed_start'] = f"{trim_info.get('start_ms', 0)} ms"
            log['trimmed_end']   = f"{trim_info.get('end_ms',   0)} ms"
            log['filter_ok']     = trim_info.get('filter_ok', True)
            log['filter_error']  = trim_info.get('filter_error', '')

            self.progress.emit("Loading waveform...")
            proc_samples = None
            try:
                from pydub import AudioSegment
                import numpy as np
                seg = AudioSegment.from_file(processed)
                log['processed_duration'] = f"{len(seg) / 1000:.2f} s"
                arr = np.array(seg.get_array_of_samples(), dtype=np.float32)
                if seg.channels == 2:
                    arr = arr.reshape(-1,2).mean(axis=1)
                mv = max(abs(arr.max()), abs(arr.min()), 1)
                proc_samples = arr / mv
            except Exception:
                log['processed_duration'] = "?"

            self.progress.emit("Saving file...")
            ext    = out_fmt
            custom = s.get('custom_filename','').strip()
            spath  = s.get('save_path','')

            if not custom:
                ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"VoiceTrack_{ts}.{ext}"
            else:
                stype = s.get('suffix_type','Date/Time')
                if stype == 'Date/Time':
                    ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
                    filename = f"{custom}_{ts}.{ext}"
                elif stype == 'Number':
                    sep   = s.get('number_separator', '_')
                    start = s.get('number_start', 1)
                    width = s.get('number_width', 3)
                    try: start = int(start)
                    except (TypeError, ValueError): start = 1
                    try: width = int(width)
                    except (TypeError, ValueError): width = 3
                    n        = self._next_num(custom, spath, ext, sep, start, width)
                    filename = f"{custom}{sep}{n:0{width}d}.{ext}"
                else:
                    filename = f"{custom}.{ext}"

            full = os.path.normpath(os.path.join(spath, filename))
            if os.path.exists(full) and not self._allow_overwrite:
                self.file_exists_warn.emit(filename, full)
                return

            final = self.processor.save_to_destination(processed, spath, filename)
            if not final:
                self.error.emit("Failed to save file")
                return

            final = os.path.normpath(final)
            log['filename'] = filename
            self.finished.emit(final, proc_samples, log)

        except Exception as e:
            self.error.emit(f"Processing error: {e}")


# ─── Settings dialog ──────────────────────────────────────────────────────────
TAB_CSS = f"""
    QTabWidget::pane {{
        border: 1px solid {C_BORDER}; border-radius: 6px;
        background: {C_CARD}; margin-top: -1px;
    }}
    QTabBar {{
        background: transparent;
    }}
    QTabBar::tab {{
        background: {C_BG}; color: {C_MUTED};
        border: 1px solid {C_BORDER}; border-bottom: none;
        border-top-left-radius: 5px; border-top-right-radius: 5px;
        padding: 7px 9px; font-size: 12px; font-weight: 600;
        margin-right: 2px; min-width: 84px;
    }}
    QTabBar::tab:selected {{
        background: {C_CARD}; color: {C_ACCENT};
        border-bottom: 2px solid {C_CARD};
    }}
    QTabBar::tab:hover:!selected {{ background: #E2E8F0; }}
"""

CHANNEL_MODES = [
    AudioRecorder.CH_STEREO,
    AudioRecorder.CH_MONO_MIX,
    AudioRecorder.CH_MONO_LEFT,
    AudioRecorder.CH_MONO_RIGHT,
]


class SettingsDialog(QDialog):
    def __init__(self, recorder, config, parent=None):
        super().__init__(parent)
        self.recorder = recorder
        self.config   = config
        self._rdj_conn = None   # live DB connection for dropdown population
        self._song_types    = []   # [(id, name), ...]
        self._subcategories = []   # [(id, parentid, name), ...]
        self._genres        = []   # [(id, name), ...]
        self.setWindowTitle("Settings")
        self.setMinimumWidth(780)
        self.setStyleSheet(f"""
            QDialog {{ background: {C_BG}; font-family: 'Segoe UI', Arial; }}
            QGroupBox {{
                font-size: 12px; font-weight: 700; color: {C_MUTED};
                border: 1px solid {C_BORDER}; border-radius: 6px;
                margin-top: 10px; padding: 10px; background: {C_CARD};
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
            {TAB_CSS}
            {INPUT_CSS}
            QCheckBox {{ font-size: 13px; }}
        """)
        self._build()
        self._load()

    @staticmethod
    def _phonebook_path():
        return os.path.join(PathManager().app_data_dir, 'phonebook.json')

    def _build(self):
        from PyQt5.QtWidgets import QFormLayout, QDialogButtonBox, QCheckBox, QScrollArea
        root = QVBoxLayout(self)
        root.setContentsMargins(16,16,16,16)
        root.setSpacing(12)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        def lbl(t):
            l = QLabel(t); l.setStyleSheet(LABEL_CSS); return l

        # ── Tab 1: Device & Output ───────────────────────────────────────
        t1  = QWidget()
        t1l = QFormLayout(t1)
        t1l.setContentsMargins(16,16,16,16)
        t1l.setSpacing(10)

        self.device_combo = QComboBox()
        self._fill_devices()
        t1l.addRow(lbl("Input device:"), self.device_combo)

        self.output_device_combo = QComboBox()
        self._fill_output_devices()
        t1l.addRow(lbl("Output device:"), self.output_device_combo)

        self.channel_combo = QComboBox()
        self.channel_combo.addItems(CHANNEL_MODES)
        t1l.addRow(lbl("Channel mode:"), self.channel_combo)

        fmt_row = QHBoxLayout(); fmt_row.setSpacing(8)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["WAV", "MP3"])
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(MP3_BITRATES)
        self.bitrate_combo.setCurrentText("320k")
        fmt_row.addWidget(self.format_combo)
        fmt_row.addWidget(QLabel("Bitrate:")); fmt_row.addWidget(self.bitrate_combo)
        t1l.addRow(lbl("Output format:"), fmt_row)

        save_row = QHBoxLayout(); save_row.setSpacing(6)
        self.save_path_input = QLineEdit()
        self.save_path_input.setPlaceholderText("Save folder path...")
        browse = QPushButton("…"); browse.setFixedWidth(34)
        browse.setStyleSheet(btn_style(C_ACCENT, C_ACCENT_H, extra="font-size:13px;padding:7px 4px;"))
        browse.clicked.connect(self._browse)
        save_row.addWidget(self.save_path_input); save_row.addWidget(browse)
        t1l.addRow(lbl("Save folder:"), save_row)

        # Silence / padding
        from PyQt5.QtWidgets import QFormLayout as _FL
        self.threshold_combo = QComboBox()
        self.threshold_combo.addItems([f"{v}dB" for v in range(-80,-20,3)])
        self.threshold_combo.setCurrentText("-53dB")
        t1l.addRow(lbl("Silence threshold:"), self.threshold_combo)

        pad_row = QHBoxLayout(); pad_row.setSpacing(8)
        self.intro_input = QLineEdit("50"); self.intro_input.setFixedWidth(70)
        self.outro_input = QLineEdit("50"); self.outro_input.setFixedWidth(70)
        pad_row.addWidget(QLabel("Intro:")); pad_row.addWidget(self.intro_input)
        pad_row.addWidget(QLabel("Outro:")); pad_row.addWidget(self.outro_input)
        pad_row.addStretch()
        t1l.addRow(lbl("Padding (ms):"), pad_row)

        self._tabs.addTab(t1, "Device && Output")

        # ── Tab 2: Audio Filter ──────────────────────────────────────────
        t2  = QWidget()
        t2l = QFormLayout(t2)
        t2l.setContentsMargins(16,16,16,16)
        t2l.setSpacing(10)

        self.filter_combo = QComboBox()
        self.filter_combo.addItems(list(FILTER_PRESETS.keys()))
        self.filter_combo.currentTextChanged.connect(self._on_filter_changed)
        t2l.addRow(lbl("Filter preset:"), self.filter_combo)

        self.custom_filter = QLineEdit()
        self.custom_filter.setPlaceholderText("FFmpeg -af filter string...")
        t2l.addRow(lbl("Custom filter:"), self.custom_filter)

        note = QLabel("Custom filter is passed directly to FFmpeg as -af argument.")
        note.setStyleSheet(f"font-size:11px; color:{C_MUTED};")
        t2l.addRow(note)

        self._tabs.addTab(t2, "Audio Filter")

        # ── Tab 3: RadioDJ ───────────────────────────────────────────────
        t3_outer = QWidget()
        t3_vbox  = QVBoxLayout(t3_outer)
        t3_vbox.setContentsMargins(12,12,12,12)
        t3_vbox.setSpacing(8)

        # Enable toggle
        self.rdj_enable_cb = QCheckBox("Enable RadioDJ integration")
        self.rdj_enable_cb.setStyleSheet(f"font-size:13px; font-weight:700; color:{C_ACCENT};")
        self.rdj_enable_cb.toggled.connect(self._on_rdj_toggle)
        t3_vbox.addWidget(self.rdj_enable_cb)

        # Container that gets enabled/disabled
        self._rdj_container = QWidget()
        rdj_form = QFormLayout(self._rdj_container)
        rdj_form.setContentsMargins(4,4,4,4)
        rdj_form.setSpacing(8)

        def rdj_lbl(t):
            l = QLabel(t); l.setStyleSheet(LABEL_CSS); return l

        self.rdj_host  = QLineEdit(); self.rdj_host.setPlaceholderText("localhost")
        self.rdj_port  = QLineEdit(); self.rdj_port.setPlaceholderText("3306")
        self.rdj_user  = QLineEdit(); self.rdj_user.setPlaceholderText("root")
        self.rdj_pass  = QLineEdit(); self.rdj_pass.setEchoMode(QLineEdit.Password)
        self.rdj_pass.setPlaceholderText("password")
        self.rdj_db    = QLineEdit(); self.rdj_db.setPlaceholderText("radiodj")
        self.rdj_radio_name = QLineEdit(); self.rdj_radio_name.setPlaceholderText("My Radio Station")

        rdj_form.addRow(rdj_lbl("Host:"),       self.rdj_host)
        rdj_form.addRow(rdj_lbl("Port:"),       self.rdj_port)
        rdj_form.addRow(rdj_lbl("User:"),       self.rdj_user)
        rdj_form.addRow(rdj_lbl("Password:"),   self.rdj_pass)
        rdj_form.addRow(rdj_lbl("Database:"),   self.rdj_db)
        rdj_form.addRow(rdj_lbl("Radio Name:"), self.rdj_radio_name)

        # Connect button
        self.rdj_connect_btn = QPushButton("Connect & Load Categories")
        self.rdj_connect_btn.setStyleSheet(btn_style(C_ACCENT, C_ACCENT_H,
                                                      extra="font-size:12px;padding:7px;"))
        self.rdj_connect_btn.clicked.connect(self._rdj_connect)
        rdj_form.addRow(self.rdj_connect_btn)

        self.rdj_conn_status = QLabel("Not connected")
        self.rdj_conn_status.setStyleSheet(f"font-size:11px; color:{C_MUTED};")
        rdj_form.addRow(self.rdj_conn_status)

        # Category dropdowns
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{C_BORDER};")
        rdj_form.addRow(sep)

        self.rdj_songtype_combo = QComboBox()
        self.rdj_songtype_combo.addItem("— connect first —")
        self.rdj_songtype_combo.currentIndexChanged.connect(self._on_songtype_changed)
        rdj_form.addRow(rdj_lbl("Song Type:"), self.rdj_songtype_combo)

        self.rdj_category_combo = QComboBox()
        self.rdj_category_combo.addItem("— connect first —")
        rdj_form.addRow(rdj_lbl("Category:"), self.rdj_category_combo)

        self.rdj_genre_combo = QComboBox()
        self.rdj_genre_combo.addItem("— connect first —")
        rdj_form.addRow(rdj_lbl("Genre:"), self.rdj_genre_combo)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(f"color:{C_BORDER};")
        rdj_form.addRow(sep2)

        self.rdj_label     = QLineEdit()
        self.rdj_publisher = QLineEdit()
        self.rdj_copyright = QLineEdit()
        self.rdj_comments  = QLineEdit()
        rdj_form.addRow(rdj_lbl("Label:"),     self.rdj_label)
        rdj_form.addRow(rdj_lbl("Publisher:"), self.rdj_publisher)
        rdj_form.addRow(rdj_lbl("Copyright:"), self.rdj_copyright)
        rdj_form.addRow(rdj_lbl("Comments:"),  self.rdj_comments)

        self.rdj_overlay_cb = QCheckBox("Overlay")
        rdj_form.addRow(self.rdj_overlay_cb)

        t3_vbox.addWidget(self._rdj_container)
        t3_vbox.addStretch()

        self._tabs.addTab(t3_outer, "RadioDJ")

        # ── SIP phone ────────────────────────────────────────────────────
        self.sip_tab = PhoneSettingsTab(self.recorder, self._phonebook_path())
        sip_scroll = QScrollArea()
        sip_scroll.setWidgetResizable(True)
        sip_scroll.setFrameShape(QFrame.NoFrame)
        sip_scroll.setWidget(self.sip_tab)
        self._tabs.addTab(sip_scroll, "SIP")

        # ── Tab 4: Preset Editor ─────────────────────────────────────────
        t4 = QWidget()
        t4l = QVBoxLayout(t4)
        t4l.setContentsMargins(12,12,12,12)
        t4l.setSpacing(8)

        pe_top = QHBoxLayout()
        pe_lbl = QLabel("Filter Presets")
        pe_lbl.setStyleSheet(f"font-size:13px; font-weight:700; color:{C_TEXT};")
        pe_top.addWidget(pe_lbl); pe_top.addStretch()
        pe_add = QPushButton("＋ Add")
        pe_del = QPushButton("✖ Delete")
        pe_add.setStyleSheet(btn_style(C_GREEN, C_GREEN_H, extra="font-size:12px;padding:5px 10px;"))
        pe_del.setStyleSheet(btn_style(C_RED, C_RED_H, extra="font-size:12px;padding:5px 10px;"))
        pe_top.addWidget(pe_add); pe_top.addWidget(pe_del)
        t4l.addLayout(pe_top)

        self.preset_list = QListWidget()
        self.preset_list.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid {C_BORDER}; border-radius:5px;
                background:{C_CARD}; font-size:13px;
            }}
            QListWidget::item:selected {{ background:{C_ACCENT}; color:white; }}
            QListWidget::item {{ padding: 6px 10px; }}
        """)
        self.preset_list.currentRowChanged.connect(self._on_preset_selected)
        self.preset_list.itemDoubleClicked.connect(self._use_preset)
        t4l.addWidget(self.preset_list)

        use_btn = QPushButton("✔  Use this preset (make active)")
        use_btn.setToolTip("Double-click a preset in the list does the same")
        use_btn.setStyleSheet(btn_style(C_GREEN, C_GREEN_H, extra="font-size:12px;padding:7px;"))
        use_btn.clicked.connect(self._use_preset)
        t4l.addWidget(use_btn)

        pe_form_widget = QWidget()
        pe_form = QVBoxLayout(pe_form_widget)
        pe_form.setContentsMargins(0,0,0,0); pe_form.setSpacing(6)

        self.pe_name = QLineEdit()
        self.pe_name.setPlaceholderText("Preset name...")
        self.pe_filter = QLineEdit()
        self.pe_filter.setPlaceholderText("FFmpeg -af filter string...")

        pe_form.addWidget(QLabel("Name:")); pe_form.addWidget(self.pe_name)
        pe_form.addWidget(QLabel("Filter string:")); pe_form.addWidget(self.pe_filter)

        pe_save_btn = QPushButton("💾  Save preset")
        pe_save_btn.setStyleSheet(btn_style(C_ACCENT, C_ACCENT_H, extra="font-size:12px;padding:7px;"))
        pe_save_btn.clicked.connect(self._pe_save_preset)
        pe_form.addWidget(pe_save_btn)

        t4l.addWidget(pe_form_widget)
        self._populate_preset_list()

        pe_add.clicked.connect(self._pe_add_preset)
        pe_del.clicked.connect(self._pe_delete_preset)

        self._tabs.addTab(t4, "Preset Editor")

        # ── Tab 5: Buy Me a Coffee ───────────────────────────────────────
        t5 = QWidget()
        t5l = QVBoxLayout(t5)
        t5l.setContentsMargins(24,32,24,24)
        t5l.setSpacing(16)
        t5l.setAlignment(Qt.AlignTop)

        coffee_title = QLabel("☕  Enjoying VoiceTrack Magic?")
        coffee_title.setStyleSheet(f"font-size:16px; font-weight:800; color:{C_TEXT};")
        coffee_title.setAlignment(Qt.AlignCenter)
        t5l.addWidget(coffee_title)

        coffee_text = QLabel(
            "If this little tool makes your radio work easier and saves you time,\n"
            "I'd really appreciate it if you bought me a coffee every now and then! 🎙"
        )
        coffee_text.setStyleSheet(f"font-size:13px; color:{C_MUTED}; line-height:1.5;")
        coffee_text.setAlignment(Qt.AlignCenter)
        coffee_text.setWordWrap(True)
        t5l.addWidget(coffee_text)

        coffee_btn = QPushButton("☕  Buy me a coffee")
        coffee_btn.setStyleSheet(f"""
            QPushButton {{
                background: #FFDD00; color: #000000;
                font-size: 14px; font-weight: 800;
                border: none; border-radius: 8px;
                padding: 12px 28px;
            }}
            QPushButton:hover {{ background: #F0CD00; }}
        """)
        coffee_btn.setCursor(Qt.PointingHandCursor)
        coffee_btn.clicked.connect(lambda: __import__('webbrowser').open(
            'https://buymeacoffee.com/molnarb83v'))
        t5l.addWidget(coffee_btn, alignment=Qt.AlignCenter)

        self._tabs.addTab(t5, "☕ Support")

        # ── Dialog buttons ───────────────────────────────────────────────
        from PyQt5.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save_accept)
        bb.rejected.connect(self.reject)
        bb.setStyleSheet(f"""
            QPushButton {{ padding: 8px 22px; font-size: 13px; font-weight: 600;
                          border-radius: 5px; border: none; color: white; }}
            QPushButton[text="OK"] {{ background: {C_ACCENT}; }}
            QPushButton[text="OK"]:hover {{ background: {C_ACCENT_H}; }}
            QPushButton[text="Cancel"] {{ background: {C_MUTED}; }}
        """)
        root.addWidget(bb)

    # ── Device helpers ────────────────────────────────────────────────────────
    def _fill_devices(self):
        self.device_combo.clear()
        try:
            for d in self.recorder.get_input_devices():
                suffix = " (Stereo)" if d['channels'] > 1 else " (Mono)"
                self.device_combo.addItem(d['name']+suffix, userData=d)
        except Exception:
            self.device_combo.addItem("No devices found")

    def _fill_output_devices(self):
        self.output_device_combo.clear()
        self.output_device_combo.addItem("System default", userData=None)
        try:
            for d in self.recorder.get_output_devices():
                self.output_device_combo.addItem(d['name'], userData=d)
        except Exception:
            pass

    def _on_format_changed(self, text):
        self.bitrate_combo.setEnabled(text == "MP3")

    def _on_filter_changed(self, text):
        self.custom_filter.setEnabled(text == "Custom")
        if hasattr(self, 'preset_list'):
            self._populate_preset_list()

    def _browse(self):
        f = QFileDialog.getExistingDirectory(self, "Select Save Folder")
        if f: self.save_path_input.setText(f)

    # ── RadioDJ helpers ───────────────────────────────────────────────────────
    def _on_rdj_toggle(self, enabled):
        self._rdj_container.setEnabled(enabled)

    def _rdj_connect(self):
        from modules.radiodj import (get_connection, fetch_song_types,
                                      fetch_subcategories, fetch_genres)
        try:
            if self._rdj_conn:
                try: self._rdj_conn.close()
                except Exception: pass
            self._rdj_conn = get_connection(
                self.rdj_host.text().strip(),
                self.rdj_port.text().strip() or '3306',
                self.rdj_user.text().strip(),
                self.rdj_pass.text(),
                self.rdj_db.text().strip(),
            )
            self._song_types    = fetch_song_types(self._rdj_conn)
            self._subcategories = fetch_subcategories(self._rdj_conn)
            self._genres        = fetch_genres(self._rdj_conn)
            self.rdj_conn_status.setText(
                f"✅ Connected — {len(self._song_types)} song types, "
                f"{len(self._subcategories)} categories, {len(self._genres)} genres")
            self.rdj_conn_status.setStyleSheet(f"font-size:11px; color:{C_GREEN};")
            self._populate_dropdowns()
        except Exception as e:
            self.rdj_conn_status.setText(f"❌ {e}")
            self.rdj_conn_status.setStyleSheet(f"font-size:11px; color:{C_RED};")

    def _populate_dropdowns(self, saved_st=None, saved_cat=None, saved_genre=None):
        self.rdj_songtype_combo.blockSignals(True)
        self.rdj_songtype_combo.clear()
        for sid, sname in self._song_types:
            self.rdj_songtype_combo.addItem(sname, userData=sid)
        self.rdj_songtype_combo.blockSignals(False)

        # Restore saved song type
        if saved_st is not None:
            for i in range(self.rdj_songtype_combo.count()):
                if self.rdj_songtype_combo.itemData(i) == saved_st:
                    self.rdj_songtype_combo.setCurrentIndex(i); break

        self._on_songtype_changed(self.rdj_songtype_combo.currentIndex(),
                                   saved_cat=saved_cat)

        # Genre
        self.rdj_genre_combo.clear()
        for gid, gname in self._genres:
            self.rdj_genre_combo.addItem(gname, userData=gid)
        if saved_genre is not None:
            for i in range(self.rdj_genre_combo.count()):
                if self.rdj_genre_combo.itemData(i) == saved_genre:
                    self.rdj_genre_combo.setCurrentIndex(i); break

    def _on_songtype_changed(self, index, saved_cat=None):
        st_id = self.rdj_songtype_combo.itemData(index)
        self.rdj_category_combo.clear()
        for cid, parentid, cname in self._subcategories:
            if parentid == st_id:
                self.rdj_category_combo.addItem(cname, userData=cid)
        if saved_cat is not None:
            for i in range(self.rdj_category_combo.count()):
                if self.rdj_category_combo.itemData(i) == saved_cat:
                    self.rdj_category_combo.setCurrentIndex(i); break

    # ── Load / Save ───────────────────────────────────────────────────────────
    def _load(self):
        s = self.config.load_settings()
        self.save_path_input.setText(s.get('save_path',''))
        fmt = s.get('format','WAV')
        self.format_combo.setCurrentText("MP3" if fmt == "MP3" else "WAV")
        self.bitrate_combo.setCurrentText(s.get('mp3_bitrate','320k'))
        self._on_format_changed(self.format_combo.currentText())

        self.threshold_combo.setCurrentText(s.get('threshold','-53dB'))
        self.intro_input.setText(str(s.get('intro_padding', 50)))
        self.outro_input.setText(str(s.get('outro_padding', 50)))

        preset = s.get('filter_preset','None')
        idx = self.filter_combo.findText(preset)
        if idx >= 0:
            self.filter_combo.setCurrentIndex(idx)
        elif preset != 'None':
            QMessageBox.warning(self, "Preset not found",
                f'The saved filter preset "{preset}" no longer exists.\n'
                'Please choose a preset in the Audio Filter tab.')
        self.custom_filter.setText(s.get('custom_filter',''))
        self._on_filter_changed(self.filter_combo.currentText())

        ch = s.get('channel_mode', AudioRecorder.CH_MONO_LEFT)
        if ch not in CHANNEL_MODES: ch = AudioRecorder.CH_MONO_LEFT
        idx = self.channel_combo.findText(ch)
        if idx >= 0: self.channel_combo.setCurrentIndex(idx)

        saved = s.get('selected_device','')
        for i in range(self.device_combo.count()):
            if saved in self.device_combo.itemText(i):
                self.device_combo.setCurrentIndex(i); break

        saved_out = s.get('selected_output_device','')
        for i in range(self.output_device_combo.count()):
            d = self.output_device_combo.itemData(i)
            if d and saved_out and saved_out in self.output_device_combo.itemText(i):
                self.output_device_combo.setCurrentIndex(i); break

        # RadioDJ
        rdj_on = s.get('radiodj_enabled', False)
        self.rdj_enable_cb.setChecked(rdj_on)
        self._rdj_container.setEnabled(rdj_on)
        self.rdj_host.setText(s.get('radiodj_host','localhost'))
        self.rdj_port.setText(str(s.get('radiodj_port','3306')))
        self.rdj_user.setText(s.get('radiodj_user','root'))
        self.rdj_pass.setText(s.get('radiodj_pass',''))
        self.rdj_db.setText(s.get('radiodj_database','radiodj'))
        self.rdj_radio_name.setText(s.get('radio_name',''))
        self.rdj_label.setText(s.get('rdj_label',''))
        self.rdj_publisher.setText(s.get('rdj_publisher',''))
        self.rdj_copyright.setText(s.get('rdj_copyright',''))
        self.rdj_comments.setText(s.get('rdj_comments',''))
        self.rdj_overlay_cb.setChecked(s.get('rdj_overlay', False))
        self.sip_tab.load(s)

        if rdj_on and s.get('radiodj_host') and s.get('radiodj_database'):
            try:
                self._rdj_connect()
                self._populate_dropdowns(
                    saved_st    = s.get('rdj_song_type_id'),
                    saved_cat   = s.get('rdj_subcategory_id'),
                    saved_genre = s.get('rdj_genre_id'),
                )
            except Exception:
                pass

    def _save_accept(self):
        s = self.config.load_settings()
        s['format']        = self.format_combo.currentText()
        s['mp3_bitrate']   = self.bitrate_combo.currentText()
        s['save_path']     = self.save_path_input.text().strip()
        s['filter_preset'] = self.filter_combo.currentText()
        s['custom_filter'] = self.custom_filter.text().strip()
        s['channel_mode']  = self.channel_combo.currentText()
        s['threshold']     = self.threshold_combo.currentText()
        try:    s['intro_padding'] = max(0, min(5000, int(self.intro_input.text())))
        except Exception: s['intro_padding'] = 50
        try:    s['outro_padding'] = max(0, min(5000, int(self.outro_input.text())))
        except Exception: s['outro_padding'] = 50
        d = self.device_combo.currentData()
        if d:
            s['selected_device'] = d['name']
            s['device_index']    = d['index']
        od = self.output_device_combo.currentData()
        if od:
            s['selected_output_device'] = od['name']
            s['output_device_index']    = od['index']
        else:
            s['selected_output_device'] = ''
            s['output_device_index']    = None

        # RadioDJ
        s['radiodj_enabled']  = self.rdj_enable_cb.isChecked()
        s['radiodj_host']     = self.rdj_host.text().strip()
        s['radiodj_port']     = self.rdj_port.text().strip() or '3306'
        s['radiodj_user']     = self.rdj_user.text().strip()
        s['radiodj_pass']     = self.rdj_pass.text()
        s['radiodj_database'] = self.rdj_db.text().strip()
        s['radio_name']       = self.rdj_radio_name.text().strip()
        s['rdj_label']        = self.rdj_label.text().strip()
        s['rdj_publisher']    = self.rdj_publisher.text().strip()
        s['rdj_copyright']    = self.rdj_copyright.text().strip()
        s['rdj_comments']     = self.rdj_comments.text().strip()
        s['rdj_overlay']      = self.rdj_overlay_cb.isChecked()

        st_id  = self.rdj_songtype_combo.currentData()
        cat_id = self.rdj_category_combo.currentData()
        gen_id = self.rdj_genre_combo.currentData()
        if st_id  is not None: s['rdj_song_type_id']   = st_id
        if cat_id is not None: s['rdj_subcategory_id'] = cat_id
        if gen_id is not None: s['rdj_genre_id']       = gen_id

        self.sip_tab.collect(s)
        self.config.save_settings(s)
        self.accept()

    def radiodj_enabled(self):
        return self.rdj_enable_cb.isChecked()

    def done(self, result):
        """Close the RadioDJ DB connection when the dialog closes (OK or Cancel)."""
        if self._rdj_conn:
            try:
                self._rdj_conn.close()
            except Exception:
                pass
            self._rdj_conn = None
        super().done(result)

    # ── Preset Editor helpers ─────────────────────────────────────────────────
    def _get_custom_presets_path(self):
        appdata = os.path.expandvars('%APPDATA%')
        d = os.path.join(appdata, 'VoiceTrackMagic')
        os.makedirs(d, exist_ok=True)
        new_path = os.path.join(d, 'custom_presets.json')
        # Migrate from legacy EasyTracker folder if present
        if not os.path.exists(new_path):
            legacy = os.path.join(appdata, 'EasyTracker', 'custom_presets.json')
            if os.path.exists(legacy):
                try:
                    import shutil
                    shutil.copy2(legacy, new_path)
                except Exception as e:
                    print(f"Could not migrate custom presets: {e}")
        return new_path

    def _load_all_presets(self):
        """Return merged dict: builtin + custom presets."""
        import json
        presets = dict(FILTER_PRESETS)  # copy builtins
        path = self._get_custom_presets_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    custom = json.load(f)
                presets.update(custom)
            except Exception:
                pass
        return presets

    def _save_custom_presets(self, custom_dict):
        import json
        path = self._get_custom_presets_path()
        with open(path, 'w') as f:
            json.dump(custom_dict, f, indent=2)

    def _get_custom_presets(self):
        import json
        path = self._get_custom_presets_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _populate_preset_list(self):
        self.preset_list.clear()
        all_presets = self._load_all_presets()
        custom = self._get_custom_presets()
        active = self.filter_combo.currentText()
        sel_row = -1
        for name in all_presets:
            if name == 'Custom': continue
            item_text = name
            if name in custom:
                item_text = f"✎ {name}"  # mark custom/edited
            if name == active:
                item_text = f"★ {item_text}  (active)"
            it = QListWidgetItem(item_text)
            it.setData(Qt.UserRole, name)
            if name == active:
                f = it.font(); f.setBold(True); it.setFont(f)
                sel_row = self.preset_list.count()
            self.preset_list.addItem(it)
        if sel_row >= 0:
            self.preset_list.setCurrentRow(sel_row)

    def _use_preset(self, *_):
        row = self.preset_list.currentRow()
        if row < 0: return
        name = self.preset_list.item(row).data(Qt.UserRole)
        idx = self.filter_combo.findText(name)
        if idx < 0: return
        self.filter_combo.setCurrentIndex(idx)
        s = self.config.load_settings()
        s['filter_preset'] = name
        self.config.save_settings(s)
        self._populate_preset_list()

    def _on_preset_selected(self, row):
        if row < 0: return
        raw_name = self.preset_list.item(row).data(Qt.UserRole)
        all_presets = self._load_all_presets()
        self.pe_name.setText(raw_name)
        self.pe_filter.setText(all_presets.get(raw_name, ''))

    def _pe_add_preset(self):
        self.pe_name.clear()
        self.pe_filter.clear()
        self.pe_name.setFocus()

    def _pe_save_preset(self):
        name   = self.pe_name.text().strip()
        fstr   = self.pe_filter.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Preset name cannot be empty.")
            return
        if name in ('None', 'Custom'):
            QMessageBox.warning(self, "Error", f'Cannot overwrite built-in preset "{name}".')
            return
        custom = self._get_custom_presets()
        custom[name] = fstr
        self._save_custom_presets(custom)
        # Also update runtime FILTER_PRESETS and the filter combo
        FILTER_PRESETS[name] = fstr
        if self.filter_combo.findText(name) < 0:
            self.filter_combo.insertItem(self.filter_combo.count()-1, name)
        self._populate_preset_list()
        QMessageBox.information(self, "Saved", f'Preset "{name}" saved.')

    def _pe_delete_preset(self):
        row = self.preset_list.currentRow()
        if row < 0: return
        name = self.preset_list.item(row).data(Qt.UserRole)
        if name in FILTER_PRESETS and name not in self._get_custom_presets():
            QMessageBox.warning(self, "Cannot delete",
                                f'"{name}" is a built-in preset and cannot be deleted.')
            return
        reply = QMessageBox.question(self, "Delete preset",
                                     f'Delete preset "{name}"?',
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes: return
        custom = self._get_custom_presets()
        custom.pop(name, None)
        self._save_custom_presets(custom)
        FILTER_PRESETS.pop(name, None)
        idx = self.filter_combo.findText(name)
        if idx >= 0: self.filter_combo.removeItem(idx)
        self._populate_preset_list()



def load_custom_presets_into_global():
    """Load user-saved presets from JSON into FILTER_PRESETS at startup."""
    import json
    appdata = os.path.expandvars('%APPDATA%')
    path = os.path.join(appdata, 'VoiceTrackMagic', 'custom_presets.json')
    if not os.path.exists(path):
        # Fall back to legacy EasyTracker folder
        legacy = os.path.join(appdata, 'EasyTracker', 'custom_presets.json')
        if os.path.exists(legacy):
            path = legacy
    if os.path.exists(path):
        try:
            with open(path) as f:
                custom = json.load(f)
            for name, fstr in custom.items():
                if name not in ('None', 'Custom'):
                    FILTER_PRESETS[name] = fstr
        except Exception as e:
            print(f"Could not load custom presets: {e}")


class VoiceTrackMagic(QMainWindow):
    def __init__(self):
        super().__init__()
        self.path_manager = PathManager()
        self.recorder     = AudioRecorder()
        self.processor    = AudioProcessor()
        self.config       = ConfigManager()

        self.processing_thread = None
        self._rec_blink_on     = False      # current REC indicator color state
        self.recording_start   = None

        self._raw_samples  = None
        self._proc_samples = None

        # Timers
        self._blink_timer = QTimer(); self._blink_timer.timeout.connect(self._blink_rec)
        self._rec_timer   = QTimer(); self._rec_timer.timeout.connect(self._update_rec_time)
        self._level_timer = QTimer(); self._level_timer.timeout.connect(self._update_meter)
        self._level_timer.start(50)
        self._playhead_timer = QTimer(); self._playhead_timer.timeout.connect(self._update_playhead)
        self._playhead_timer.start(80)   # ~12 fps playhead update

        self.recorder.on_playback_finished = self._on_playback_finished

        # SIP phone (created in _apply_phone_settings when enabled)
        self.phone         = None
        self._call_dlg     = None
        self._phone_mon    = False       # True while WE started the mic monitor for a call
        self._phone_signals = PhoneSignals()
        self._phone_signals.event.connect(self._on_phone_event)
        self._phone_timer  = QTimer()
        self._phone_timer.timeout.connect(self._phone_tick)

        self._build_ui()
        self._setup_shortcuts()
        self._load_settings()
        # FFmpeg check runs shortly AFTER the window is shown so a slow
        # AV scan of ffmpeg.exe never blocks an invisible startup
        QTimer.singleShot(300, self._check_ffmpeg_on_startup)
        QTimer.singleShot(1200, self._maybe_show_coffee_reminder)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.setWindowTitle("VoiceTrack Magic")
        self.setWindowIcon(get_app_icon())
        self.setMinimumWidth(460)
        self.resize(500, 680)
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:{C_BG}; font-family:'Segoe UI',Arial; color:{C_TEXT}; }}
            QGroupBox {{
                font-size:12px; font-weight:700; color:{C_MUTED};
                border:1px solid {C_BORDER}; border-radius:8px;
                margin-top:10px; padding:10px 10px 6px 10px; background:{C_CARD};
            }}
            QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 4px; }}
            QLabel {{ font-size:13px; }}
            {INPUT_CSS}
            QProgressBar {{
                border:1px solid {C_BORDER}; border-radius:5px;
                background:#E2E8F0; min-height:14px;
            }}
            QProgressBar::chunk {{ background:{C_GREEN}; border-radius:4px; }}
        """)

        cw   = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setContentsMargins(14,14,14,14)
        root.setSpacing(9)

        # Title row
        tr = QHBoxLayout()
        # App icon in title
        icon_lbl = QLabel()
        app_icon = get_app_icon()
        if not app_icon.isNull():
            icon_lbl.setPixmap(app_icon.pixmap(28, 28))
        else:
            icon_lbl.setText("🎙")
            icon_lbl.setStyleSheet("font-size:22px;")
        tr.addWidget(icon_lbl)
        tl = QLabel("  VoiceTrack Magic")
        tl.setStyleSheet(f"font-size:19px;font-weight:800;color:{C_ACCENT};")
        tr.addWidget(tl); tr.addStretch()
        sb = QPushButton("⚙  Settings")
        sb.setStyleSheet(btn_style(C_MUTED,"#475569",extra="font-size:12px;padding:6px 12px;"))
        sb.clicked.connect(self._open_settings)
        tr.addWidget(sb)
        root.addLayout(tr)

        # Meter
        mg  = QGroupBox("Level meter")
        ml  = QVBoxLayout(mg); ml.setSpacing(4)
        self.audio_meter = QProgressBar()
        self.audio_meter.setMaximum(100)
        self.audio_meter.setTextVisible(False)
        self.audio_meter.setFixedHeight(16)
        ml.addWidget(self.audio_meter)
        root.addWidget(mg)

        # Waveform
        wg = QGroupBox("Waveform")
        wl = QVBoxLayout(wg); wl.setContentsMargins(6,6,6,6)
        self.waveform = WaveformWidget()
        self.waveform.seek_requested.connect(self._on_waveform_seek)
        wl.addWidget(self.waveform)
        root.addWidget(wg)

        # Recording controls
        rg  = QGroupBox("Recording")
        rl  = QVBoxLayout(rg); rl.setSpacing(8)

        # --- Fixed-height status row so REC blink doesn't shift buttons ---
        status_container = QWidget()
        status_container.setFixedHeight(36)
        scl = QHBoxLayout(status_container)
        scl.setContentsMargins(0,0,0,0); scl.setSpacing(8)

        self.rec_indicator = QLabel("● REC")
        self.rec_indicator.setFixedSize(70, 28)
        self.rec_indicator.setAlignment(Qt.AlignCenter)
        self._set_rec_indicator(False, lit=False)   # hidden but takes space

        self.rec_timer_lbl = QLabel("00:00")
        self.rec_timer_lbl.setFixedSize(60, 28)
        self.rec_timer_lbl.setAlignment(Qt.AlignCenter)
        self.rec_timer_lbl.setStyleSheet(
            f"color:{C_MUTED};background:#F1F5F9;border:1px solid {C_BORDER};"
            f"border-radius:4px;font-size:12px;font-weight:700;")
        self.rec_timer_lbl.setVisible(False)

        scl.addWidget(self.rec_indicator)
        scl.addWidget(self.rec_timer_lbl)
        scl.addStretch()
        rl.addWidget(status_container)

        # Button row
        br = QHBoxLayout(); br.setSpacing(8)
        self.record_btn = QPushButton("● Record")
        self.play_btn   = QPushButton("▶ Play")
        self.delete_btn = QPushButton("✖ Delete")
        self.record_btn.setStyleSheet(btn_style(C_RED,   C_RED_H))
        self.play_btn  .setStyleSheet(btn_style(C_GREEN, C_GREEN_H))
        self.delete_btn.setStyleSheet(btn_style(C_MUTED, "#475569"))
        self.record_btn.setToolTip("Start / Stop recording  [Space]")
        self.play_btn  .setToolTip("Play / Stop playback  [P]")
        self.delete_btn.setToolTip("Delete current recording  [Delete]")
        self.record_btn.clicked.connect(self._toggle_record)
        self.play_btn  .clicked.connect(self._toggle_play)
        self.delete_btn.clicked.connect(self._delete_recording)
        br.addWidget(self.record_btn)
        br.addWidget(self.play_btn)
        br.addWidget(self.delete_btn)
        rl.addLayout(br)
        root.addWidget(rg)

        # SIP phone strip (only visible when the phone is enabled in Settings)
        self.phone_strip = PhoneStrip()
        self.phone_strip.open_requested.connect(self._show_call_window)
        self.phone_strip.setVisible(False)
        root.addWidget(self.phone_strip)

        # Active post-processing filter (always visible)
        fg  = QGroupBox("Post-processing filter (applied on save)")
        fgl = QHBoxLayout(fg); fgl.setSpacing(8)
        self.active_filter_combo = QComboBox()
        self.active_filter_combo.currentTextChanged.connect(self._on_active_filter_changed)
        self.active_filter_lbl = QLabel("")
        self.active_filter_lbl.setStyleSheet(f"color:{C_MUTED};font-size:11px;")
        fgl.addWidget(self.active_filter_combo, 1)
        fgl.addWidget(self.active_filter_lbl)
        root.addWidget(fg)

        # Filename
        ng  = QGroupBox("Filename")
        ngv = QVBoxLayout(ng); ngv.setSpacing(6)

        ngl = QHBoxLayout(); ngl.setSpacing(8)
        self.filename_input = QLineEdit()
        self.filename_input.setPlaceholderText("Empty = VoiceTrack_date_time")
        self.filename_input.textChanged.connect(self._on_filename_changed)
        self.suffix_combo = QComboBox()
        self.suffix_combo.addItems(["Date/Time", "Number", "None"])
        self.suffix_combo.setFixedWidth(110)
        self.suffix_combo.currentTextChanged.connect(self._on_suffix_type_changed)
        ngl.addWidget(self.filename_input)
        ngl.addWidget(self.suffix_combo)
        ngv.addLayout(ngl)

        # Number options row — only shown when suffix type is "Number"
        # Result: {name}{separator}{number:0width} e.g. CJ06 + "" + 01 -> CJ0601
        self.number_opts_row = QWidget()
        nor = QHBoxLayout(self.number_opts_row)
        nor.setContentsMargins(0,0,0,0); nor.setSpacing(6)

        def small_lbl(t):
            l = QLabel(t); l.setStyleSheet(LABEL_CSS); return l

        nor.addWidget(small_lbl("Separator:"))
        self.number_sep_input = QLineEdit("_")
        self.number_sep_input.setFixedWidth(50)
        self.number_sep_input.setPlaceholderText("(none)")
        self.number_sep_input.setToolTip(
            'Text between the filename and the number.\nLeave empty for no separator (e.g. "CJ06" + "01" = "CJ0601").')
        nor.addWidget(self.number_sep_input)

        nor.addWidget(small_lbl("Start:"))
        self.number_start_input = QLineEdit("1")
        self.number_start_input.setFixedWidth(50)
        self.number_start_input.setToolTip(
            "The sequence starts here, and continues from the highest\n"
            "existing number + 1 if higher matching files are already present.")
        nor.addWidget(self.number_start_input)

        nor.addWidget(small_lbl("Digits:"))
        self.number_width_input = QLineEdit("2")
        self.number_width_input.setFixedWidth(36)
        self.number_width_input.setToolTip("Zero-padding width, e.g. 2 -> 01, 3 -> 001.")
        nor.addWidget(self.number_width_input)
        nor.addStretch()

        self.number_opts_row.setVisible(False)   # shown only for "Number" suffix
        ngv.addWidget(self.number_opts_row)

        root.addWidget(ng)

        # Export to RadioDJ toggle (only visible when RadioDJ is enabled)
        self.rdj_export_row = QWidget()
        rdj_row_layout = QHBoxLayout(self.rdj_export_row)
        rdj_row_layout.setContentsMargins(0,0,0,0)
        from PyQt5.QtWidgets import QCheckBox
        self.rdj_export_cb = QCheckBox("Export to RadioDJ")
        self.rdj_export_cb.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{C_ACCENT};")
        rdj_row_layout.addWidget(self.rdj_export_cb)
        rdj_row_layout.addStretch()
        self.rdj_export_row.setVisible(False)
        root.addWidget(self.rdj_export_row)

        # Bottom buttons row: Save + Batch
        bottom_row = QHBoxLayout(); bottom_row.setSpacing(8)
        self.process_btn = QPushButton("💾  Save")
        self.process_btn.setStyleSheet(
            btn_style(C_PURPLE, C_PURPLE_H, extra="font-size:15px;padding:13px;"))
        self.process_btn.setToolTip("Process and save recording  [Ctrl+S]")
        self.process_btn.clicked.connect(self._process_and_save)
        self.batch_btn = QPushButton("📂  Batch")
        self.batch_btn.setStyleSheet(
            btn_style(C_ORANGE, "#C2410C", extra="font-size:15px;padding:13px;"))
        self.batch_btn.setToolTip("Process multiple audio files from a folder")
        self.batch_btn.clicked.connect(self._open_batch)
        self.batch_btn.setFixedWidth(110)
        bottom_row.addWidget(self.process_btn)
        bottom_row.addWidget(self.batch_btn)
        root.addLayout(bottom_row)

        # Status
        self.status_lbl = QLabel("Ready.")
        self.status_lbl.setStyleSheet(f"font-size:12px;color:{C_MUTED};padding:2px 0;")
        root.addWidget(self.status_lbl)

        # Processing log panel
        from PyQt5.QtWidgets import QTextEdit
        log_group = QGroupBox("Processing log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(6,6,6,6)
        self.log_panel = QTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setFixedHeight(110)
        self.log_panel.setStyleSheet(f"""
            QTextEdit {{
                background: #0F172A; color: #94A3B8;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px; border: none; border-radius: 4px;
                padding: 6px;
            }}
        """)
        self.log_panel.setPlaceholderText("Processing summary will appear here after saving...")
        log_layout.addWidget(self.log_panel)
        root.addWidget(log_group)

        self._set_playback_controls(False)

    # ── REC indicator (no layout shift) ───────────────────────────────────────
    def _set_rec_indicator(self, visible, lit=True):
        """Show/hide REC label by colour, not by visibility — size stays fixed."""
        if not visible:
            self.rec_indicator.setStyleSheet(
                "background:transparent;border:none;color:transparent;")
        elif lit:
            self.rec_indicator.setStyleSheet(
                f"color:{C_RED};background:#FEE2E2;border:2px solid {C_RED};"
                f"border-radius:5px;font-size:12px;font-weight:700;")
        else:
            self.rec_indicator.setStyleSheet(
                f"color:#FCA5A5;background:#FFF5F5;border:2px solid #FCA5A5;"
                f"border-radius:5px;font-size:12px;font-weight:700;")

    def _blink_rec(self):
        self._rec_blink_on = not self._rec_blink_on
        self._set_rec_indicator(True, lit=self._rec_blink_on)

    def _update_rec_time(self):
        if self.recording_start:
            e = (datetime.now()-self.recording_start).total_seconds()
            m, s = divmod(int(e), 60)
            self.rec_timer_lbl.setText(f"{m:02d}:{s:02d}")
        # Auto-stop if the recorder hit its safety limit
        if getattr(self.recorder, 'recording_limit_hit', False) and not self.recorder.is_recording():
            self._stop_recording()
            limit = self.recorder.max_recording_minutes
            QMessageBox.information(
                self, "Recording limit reached",
                f"Recording automatically stopped after {limit} minutes "
                "(safety limit). Your recording has been saved.")
            self.recorder.recording_limit_hit = False

    # ── Shortcuts ─────────────────────────────────────────────────────────────
    def _guarded(self, fn):
        """Run a shortcut action only when focus is NOT in a text field.

        Without this guard, typing a space or pressing Delete inside the
        filename box would start recording / delete the take — a nasty trap.
        """
        def wrapper():
            from PyQt5.QtWidgets import QLineEdit, QTextEdit, QPlainTextEdit
            w = QApplication.focusWidget()
            if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit)):
                return
            fn()
        return wrapper

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"),  self, self._guarded(self._toggle_record))
        QShortcut(QKeySequence("P"),      self, self._guarded(self._toggle_play))
        QShortcut(QKeySequence("Delete"), self, self._guarded(self._delete_recording))
        QShortcut(QKeySequence("Ctrl+S"), self, self._process_and_save)

    # ── Settings ──────────────────────────────────────────────────────────────
    def _refresh_filter_combo(self):
        s = self.config.load_settings()
        cur = s.get('filter_preset', 'None')
        cb = self.active_filter_combo
        cb.blockSignals(True)
        cb.clear()
        cb.addItems(list(FILTER_PRESETS.keys()))
        idx = cb.findText(cur)
        if idx < 0 and cur != 'None':
            cb.insertItem(0, f"⚠ {cur} (missing)")
            idx = 0
        cb.setCurrentIndex(max(idx, 0))
        cb.blockSignals(False)
        self._update_filter_label()

    def _update_filter_label(self):
        name = self.active_filter_combo.currentText()
        if name.startswith("⚠"):
            self.active_filter_lbl.setText("no filter!")
            self.active_filter_lbl.setStyleSheet("color:#DC2626;font-size:11px;font-weight:700;")
            return
        self.active_filter_lbl.setStyleSheet(f"color:{C_MUTED};font-size:11px;")
        if name == 'None':
            self.active_filter_lbl.setText("no processing")
        elif name == 'Custom':
            self.active_filter_lbl.setText("custom string")
        else:
            self.active_filter_lbl.setText("active ✓")
        fs = FILTER_PRESETS.get(name, '')
        self.active_filter_combo.setToolTip(fs if fs else "No filter")

    def _on_active_filter_changed(self, text):
        if text.startswith("⚠"): return
        s = self.config.load_settings()
        s['filter_preset'] = text
        self.config.save_settings(s)
        self._update_filter_label()
        self._log_append(f"Active filter: {text}", "#60A5FA")

    def _open_settings(self):
        dlg = SettingsDialog(self.recorder, self.config, self)
        accepted = dlg.exec_()
        self._refresh_filter_combo()
        if accepted:
            self._apply_phone_settings()
            s = self.config.load_settings()
            self.recorder.channel_mode = s.get('channel_mode', AudioRecorder.CH_STEREO)
            rdj_on = s.get('radiodj_enabled', False)
            self.rdj_export_row.setVisible(rdj_on)
            if not rdj_on:
                self.rdj_export_cb.setChecked(False)

    def _load_settings(self):
        s = self.config.load_settings()
        self.filename_input.setText(s.get('custom_filename',''))
        st = s.get('suffix_type','Date/Time')
        st = st if st in ["Date/Time","Number","None"] else "Date/Time"
        self.suffix_combo.setCurrentText(st)
        self.number_sep_input.setText(str(s.get('number_separator', '_')))
        self.number_start_input.setText(str(s.get('number_start', 1)))
        self.number_width_input.setText(str(s.get('number_width', 2)))
        self.number_opts_row.setVisible(st == "Number")
        self.recorder.channel_mode = s.get('channel_mode', AudioRecorder.CH_STEREO)
        self._on_filename_changed(self.filename_input.text())
        self.rdj_export_row.setVisible(s.get('radiodj_enabled', False))
        self._refresh_filter_combo()
        self._apply_phone_settings()

    def _save_settings(self):
        s = self.config.load_settings()
        s['custom_filename']  = self.filename_input.text().strip()
        s['suffix_type']      = self.suffix_combo.currentText()
        s['number_separator'] = self.number_sep_input.text()
        s['number_start']     = self._safe_int(self.number_start_input.text(), 1)
        s['number_width']     = max(1, min(6, self._safe_int(self.number_width_input.text(), 2)))
        self.config.save_settings(s)

    @staticmethod
    def _safe_int(text, default):
        try:    return max(0, min(5000, int(text)))
        except Exception: return default

    # ── Device ───────────────────────────────────────────────────────────────
    def _device_index(self):
        return self.config.load_settings().get('device_index', None)

    def _output_device_index(self):
        return self.config.load_settings().get('output_device_index', None)

    # ── Coffee reminder (once a month, gentle) ────────────────────────────────
    COFFEE_URL = 'https://buymeacoffee.com/molnarb83v'

    def _maybe_show_coffee_reminder(self):
        """Show a friendly coffee nudge at most once per calendar month.

        No opt-out — it's a free app, the least you can do is see the
        reminder once a month :)
        """
        from datetime import datetime as _dt

        s = self.config.load_settings()
        current_month = _dt.now().strftime('%Y-%m')
        if s.get('coffee_reminder_month', '') == current_month:
            return   # already shown this month

        msg = QMessageBox(self)
        msg.setWindowTitle("👋 Quick hello")
        msg.setIcon(QMessageBox.NoIcon)
        msg.setText("Still enjoying VoiceTrack Magic?")
        msg.setInformativeText(
            "Just a friendly once-a-month nudge — no pressure at all.\n\n"
            "If this little app is saving you time on voice tracking,\n"
            "a coffee is always appreciated (but never required)! ☕"
        )
        coffee_btn = msg.addButton("☕ Buy me a coffee", QMessageBox.AcceptRole)
        later_btn  = msg.addButton("Maybe later", QMessageBox.RejectRole)
        msg.setDefaultButton(later_btn)
        msg.exec_()

        # Record that it was shown this month regardless of the choice made,
        # so it never appears twice in the same month.
        s['coffee_reminder_month'] = current_month
        self.config.save_settings(s)

        if msg.clickedButton() == coffee_btn:
            import webbrowser
            webbrowser.open(self.COFFEE_URL)

    # ── FFmpeg check ──────────────────────────────────────────────────────────
    def _check_ffmpeg_on_startup(self):
        ok, msg = self.processor.test_ffmpeg()
        if ok:
            print(f"FFmpeg OK: {msg}")
            self._log_append(f"FFmpeg: {msg}", "#4ADE80")
        else:
            print(f"FFmpeg NOT found: {msg}")
            self._log_append(f"⚠ FFmpeg not found: {msg}", "#F87171")
            self._log_append("  Filters will not work. Place ffmpeg.exe next to the program.", "#F87171")
            # Warn user once at startup
            QMessageBox.warning(
                self, "FFmpeg not found",
                f"FFmpeg was not found:\n{msg}\n\n"
                "Audio filters will not work.\n"
                "Place ffmpeg.exe in the same folder as the program, or install it to PATH."
            )

    # ── Filter failed handler ─────────────────────────────────────────────────
    def _on_filter_failed(self, preset_name, error_msg):
        """Called when FFmpeg filter fails — ask user what to do."""
        self._set_all_controls(True)
        self.status_lbl.setText(f"⚠ Filter failed: {preset_name}")
        self._log_append(f"⚠ Filter failed ({preset_name}): {error_msg}", "#F87171")

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Filter Failed")
        msg.setText(f'The filter "{preset_name}" could not be applied.')
        msg.setInformativeText("Save without filter, or cancel?")
        msg.setDetailedText(f"FFmpeg error:\n{error_msg}")
        save_btn   = msg.addButton("Save without filter", QMessageBox.AcceptRole)
        cancel_btn = msg.addButton("Cancel",              QMessageBox.RejectRole)
        msg.setDefaultButton(cancel_btn)
        msg.exec_()

        if msg.clickedButton() == save_btn:
            self._set_all_controls(False)
            self.status_lbl.setText("Retrying without filter...")
            self.processing_thread.skip_filter()
            self.processing_thread.start()
        else:
            self.status_lbl.setText("Cancelled.")
    def _update_playhead(self):
        if self.recorder.is_playing():
            self.waveform.set_playhead(self.recorder.playback_position)

    def _on_waveform_seek(self, frac):
        """Called when user clicks/drags on waveform."""
        if self.recorder.is_playing():
            # Seek during playback — recorder handles it
            self.recorder.seek(frac)
        else:
            # Not playing — store position, start play from there
            self.recorder.seek(frac)
            if self.recorder.has_recording():
                self._start_play_from(frac)

    def _start_play_from(self, pos):
        """Start playback from a given position (0.0–1.0)."""
        was_playing = self.recorder.is_playing()
        if was_playing:
            self.recorder.stop_playback()
        ok = self.recorder.play_recording(start_pos=pos, output_device=self._output_device_index())
        if ok and not was_playing:
            self.play_btn.setText("⏸  Stop")
            self.play_btn.setStyleSheet(btn_style(C_RED, C_RED_H))
            self.status_lbl.setText("Playing...")
    def _update_meter(self):
        pct = int(self.recorder.get_audio_level() * 100)
        self.audio_meter.setValue(pct)
        color = C_RED if pct > 80 else C_ORANGE if pct > 50 else C_GREEN
        self.audio_meter.setStyleSheet(
            f"QProgressBar::chunk{{background:{color};border-radius:4px;}}")

    # ── Record ────────────────────────────────────────────────────────────────
    def _toggle_record(self):
        if self.recorder.is_recording(): self._stop_recording()
        else:                            self._start_recording()

    def _start_recording(self):
        if self.recorder.is_playing():
            self.recorder.stop_playback()
            self._reset_play_btn()
        idx = self._device_index()
        s   = self.config.load_settings()
        sp  = s.get('save_path','')
        if sp and not os.path.exists(sp):
            try: os.makedirs(sp, exist_ok=True)
            except Exception: pass
        if self.recorder.is_monitoring():
            self.recorder.stop_monitoring()

        ok = self.recorder.start_recording(idx)
        if not ok:
            QMessageBox.critical(self,"Error","Failed to start recording.\nCheck microphone settings.")
            return

        self._raw_samples = self._proc_samples = None
        self.waveform.clear()
        self.record_btn.setText("⏸  Stop")
        self.record_btn.setStyleSheet(btn_style(C_RED_H,"#991B1B"))
        self._set_playback_controls(False)
        self.process_btn.setEnabled(False)
        self.status_lbl.setText("Recording... Press Stop when finished.")
        self.recording_start = datetime.now()
        self.rec_timer_lbl.setText("00:00")
        self.rec_timer_lbl.setVisible(True)
        self._rec_blink_on = True
        self._set_rec_indicator(True, lit=True)
        self._blink_timer.start(600)
        self._rec_timer.start(500)

    def _stop_recording(self):
        self._blink_timer.stop()
        self._rec_timer.stop()
        self._set_rec_indicator(False)
        self.rec_timer_lbl.setVisible(False)

        ok = self.recorder.stop_recording()
        self.record_btn.setText("● Record")
        self.record_btn.setStyleSheet(btn_style(C_RED, C_RED_H))
        self.process_btn.setEnabled(True)
        if ok:
            self._set_playback_controls(True)
            self.status_lbl.setText("Recording done. Play, process or delete.")
            self._load_raw_waveform()
        else:
            self.status_lbl.setText("Error: could not save recording.")

    def _load_raw_waveform(self):
        try:
            from pydub import AudioSegment
            import numpy as np
            seg = AudioSegment.from_wav(self.recorder.temp_file)
            arr = np.array(seg.get_array_of_samples(), dtype=np.float32)
            if seg.channels == 2: arr = arr.reshape(-1,2).mean(axis=1)
            mv = max(abs(arr.max()),abs(arr.min()),1)
            self._raw_samples = arr/mv
            self.waveform.set_raw(self._raw_samples)
        except Exception as e:
            print(f"Waveform load error: {e}")

    # ── Play ──────────────────────────────────────────────────────────────────
    def _toggle_play(self):
        if self.recorder.is_playing():
            self.recorder.stop_playback()
            self._reset_play_btn()
            self.status_lbl.setText("Playback stopped.")
            return
        if self.recorder.is_recording():
            self._stop_recording()
        if not self.recorder.has_recording():
            QMessageBox.critical(self,"Error","No recording to play.")
            return
        ok = self.recorder.play_recording(output_device=self._output_device_index())
        if ok:
            self.play_btn.setText("⏸  Stop")
            self.play_btn.setStyleSheet(btn_style(C_RED, C_RED_H))
            self.status_lbl.setText("Playing...")
        else:
            QMessageBox.critical(self,"Error","Failed to start playback.")

    def _on_playback_finished(self):
        QTimer.singleShot(0, self._reset_play_btn)

    def _reset_play_btn(self):
        self.play_btn.setText("▶ Play")
        self.play_btn.setStyleSheet(btn_style(C_GREEN, C_GREEN_H))
        self.status_lbl.setText("Playback finished.")
        self.waveform.set_playhead(None)

    # ── Delete ────────────────────────────────────────────────────────────────
    def _delete_recording(self):
        if not self.recorder.has_recording() and self._raw_samples is None: return
        if self.recorder.is_playing():
            self.recorder.stop_playback()
            self._reset_play_btn()
        if self.recorder.delete_recording():
            self._raw_samples = self._proc_samples = None
            self.waveform.clear()
            self._set_playback_controls(False)
            self.process_btn.setEnabled(True)
            self.status_lbl.setText("Recording deleted.")
        else:
            QMessageBox.critical(self,"Error","Failed to delete recording.")

    # ── Process & Save ────────────────────────────────────────────────────────
    def _process_and_save(self):
        if not self.recorder.has_recording():
            QMessageBox.warning(self,"Warning","No recording to process.")
            return
        s  = self.config.load_settings()
        sp = s.get('save_path','').strip()
        if not sp:
            sp = self.path_manager.get_default_save_dir()
            s['save_path'] = sp
        if not os.path.exists(sp):
            try: os.makedirs(sp, exist_ok=True)
            except Exception:
                QMessageBox.critical(self,"Error",f"Cannot create save folder:\n{sp}"); return

        s['custom_filename']  = self.filename_input.text().strip()
        s['suffix_type']      = self.suffix_combo.currentText()
        s['number_separator'] = self.number_sep_input.text()
        s['number_start']     = self._safe_int(self.number_start_input.text(), 1)
        s['number_width']     = max(1, min(6, self._safe_int(self.number_width_input.text(), 2)))
        # threshold/padding come from settings (already stored there)

        self._set_all_controls(False)
        self.status_lbl.setText("Processing...")
        self.processing_thread = ProcessingThread(
            self.processor, self.recorder.get_recording_path(), s)
        self.processing_thread.finished.connect(self._on_proc_done)
        self.processing_thread.error.connect(self._on_proc_error)
        self.processing_thread.progress.connect(lambda m: self.status_lbl.setText(m))
        self.processing_thread.file_exists_warn.connect(self._on_file_exists)
        self.processing_thread.filter_failed.connect(self._on_filter_failed)
        self.processing_thread.start()

    def _log_append(self, text, color=None):
        """Append a line to the log panel with optional colour."""
        if color:
            self.log_panel.append(f'<span style="color:{color};">{text}</span>')
        else:
            self.log_panel.append(text)

    def _on_proc_done(self, path, proc_samples, log):
        self._set_all_controls(True)
        self.status_lbl.setText(f"✅ Saved: {os.path.basename(path)}")
        if proc_samples is not None:
            self._proc_samples = proc_samples
            self.waveform.set_processed(proc_samples)

        # Build log panel
        from datetime import datetime as _dt
        self.log_panel.clear()
        ts = _dt.now().strftime("%H:%M:%S")
        self._log_append(f"── {ts} ─────────────────────────────", "#475569")
        self._log_append(f"  File       : {log.get('filename','?')}", "#E2E8F0")
        self._log_append(f"  Raw        : {log.get('raw_duration','?')}  →  "
                         f"Processed: {log.get('processed_duration','?')}", "#E2E8F0")
        self._log_append(f"  Trimmed    : start {log.get('trimmed_start','?')}  |  "
                         f"end {log.get('trimmed_end','?')}", "#60A5FA")
        self._log_append(f"  Padding    : intro {log.get('intro_padding','?')}  |  "
                         f"outro {log.get('outro_padding','?')}", "#94A3B8")
        self._log_append(f"  Threshold  : {log.get('threshold','?')}", "#94A3B8")
        filter_name = log.get('filter','None')
        if log.get('filter_error'):
            self._log_append(f"  Filter     : {filter_name}  ⚠ {log['filter_error']}", "#F87171")
        elif filter_name and filter_name != 'None':
            self._log_append(f"  Filter     : {filter_name}  ✓", "#4ADE80")
        else:
            self._log_append(f"  Filter     : None", "#94A3B8")
        self._log_append(f"  Format     : {log.get('format','?')}", "#94A3B8")
        self._log_append(f"  Saved to   : {path}", "#A78BFA")

        # RadioDJ export
        if self.rdj_export_cb.isChecked():
            self._do_radiodj_export(path, log.get('filename', os.path.basename(path)))

        QMessageBox.information(self,"Done",f"Voice track saved to:\n{path}")
        self.processor.cleanup_temp_files()

    def _do_radiodj_export(self, file_path, filename_title):
        from modules.radiodj import get_connection, export_to_radiodj
        s = self.config.load_settings()
        try:
            conn = get_connection(
                s.get('radiodj_host','localhost'),
                s.get('radiodj_port','3306'),
                s.get('radiodj_user','root'),
                s.get('radiodj_pass',''),
                s.get('radiodj_database','radiodj'),
            )
            rdj_settings = {
                'radio_name':      s.get('radio_name',''),
                'song_type_id':    s.get('rdj_song_type_id', 0),
                'subcategory_id':  s.get('rdj_subcategory_id', 0),
                'genre_id':        s.get('rdj_genre_id', 0),
                'label':           s.get('rdj_label',''),
                'publisher':       s.get('rdj_publisher',''),
                'copyright':       s.get('rdj_copyright',''),
                'comments':        s.get('rdj_comments',''),
                'overlay':         s.get('rdj_overlay', False),
                'filename_title':  os.path.splitext(filename_title)[0],
            }
            export_to_radiodj(conn, file_path, rdj_settings)
            conn.close()
            self._log_append(f"  RadioDJ    : ✓ exported to database", "#4ADE80")
        except Exception as e:
            self._log_append(f"  RadioDJ    : ❌ {e}", "#F87171")
            QMessageBox.warning(self, "RadioDJ Export Failed", str(e))

    def _on_proc_error(self, msg):
        self._set_all_controls(True)
        self.status_lbl.setText(f"❌ Error: {msg}")
        QMessageBox.critical(self,"Processing Error", msg)

    def _on_file_exists(self, filename, full_path):
        self._set_all_controls(True)
        self.status_lbl.setText("File exists — waiting for decision.")
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("File Already Exists")
        msg.setText(f"'{filename}' already exists.")
        msg.setInformativeText("Overwrite or cancel?")
        msg.setDetailedText(f"Full path: {full_path}")
        ow = msg.addButton("Overwrite", QMessageBox.AcceptRole)
        msg.addButton("Cancel",    QMessageBox.RejectRole)
        msg.setDefaultButton(ow)
        msg.exec_()
        if msg.clickedButton() == ow:
            self._set_all_controls(False)          # prevent double-start
            self.status_lbl.setText("Overwriting...")
            self.processing_thread.allow_overwrite()
            self.processing_thread.start()
        else:
            self.status_lbl.setText("Save cancelled.")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _on_suffix_type_changed(self, text):
        self.number_opts_row.setVisible(text == "Number")

    def _on_filename_changed(self, text):
        cleaned = re.sub(r'[^a-zA-Z0-9\-_\s]','',text)
        if cleaned != text:
            self.filename_input.blockSignals(True)
            pos = self.filename_input.cursorPosition()
            self.filename_input.setText(cleaned)
            self.filename_input.setCursorPosition(min(pos,len(cleaned)))
            self.filename_input.blockSignals(False)
        self.suffix_combo.setEnabled(bool(cleaned.strip()))

    def _set_playback_controls(self, on):
        self.play_btn  .setEnabled(on)
        self.delete_btn.setEnabled(on)

    def _set_all_controls(self, on):
        self.record_btn .setEnabled(on)
        self.play_btn   .setEnabled(on)
        self.delete_btn .setEnabled(on)
        self.process_btn.setEnabled(on)

    def _open_batch(self):
        dlg = BatchDialog(self.processor, self.config, self)
        dlg.exec_()

    # ── SIP phone ─────────────────────────────────────────────────────────────
    def _stop_phone(self):
        self._phone_timer.stop()
        if self.phone:
            try:
                self.phone.stop()
            except Exception as e:
                print(f"phone stop error: {e}")
            self.phone = None
        if self._phone_mon and self.recorder.is_monitoring():
            self.recorder.stop_monitoring()
        self._phone_mon = False
        if self._call_dlg:
            self._call_dlg.set_controller(None)
        self.phone_strip.update_state(None)

    def _apply_phone_settings(self):
        """(Re)start the SIP phone from the saved settings."""
        from sipphone.controller import PhoneController
        s = self.config.load_settings()
        self._stop_phone()
        self.phone_strip.setVisible(bool(s.get('sip_enabled')))
        if not (s.get('sip_enabled') and s.get('sip_user') and s.get('sip_domain')):
            return
        try:
            ins  = self.recorder.get_input_devices()
            outs = self.recorder.get_output_devices()
            in_dev   = resolve_device(ins,  s.get('sip_input_name'))
            out_dev  = resolve_device(outs, s.get('sip_output_name'))
            if out_dev is None:
                out_dev = self._output_device_index()
            ring_dev = resolve_device(outs, s.get('sip_ring_name'))
            pb_path  = os.path.join(self.path_manager.app_data_dir, 'phonebook.json')
            self.phone = PhoneController(
                s, self.recorder, pb_path, in_device=in_dev, out_device=out_dev, ring_device=ring_dev,
                on_event=self._phone_signals.event.emit, log=print)
            self.phone.start()
        except Exception as e:
            self.phone = None
            self._log_append(f"SIP phone could not start: {e}", "#F87171")
            self.phone_strip.update_state(None)
            return
        if self._call_dlg:
            self._call_dlg.set_controller(self.phone)
        self._phone_timer.start(500)
        self._log_append(f"SIP phone starting ({s.get('sip_user')}@{s.get('sip_domain')})...", "#60A5FA")

    def _show_call_window(self):
        if not self._call_dlg:
            self._call_dlg = CallManagerDialog(None)
            self._call_dlg.set_controller(self.phone)
        self._call_dlg.show()
        self._call_dlg.raise_()
        self._call_dlg.activateWindow()

    def _on_phone_event(self, name, call, info):
        who = (call.label if call is not None and getattr(call, 'label', '') else '')
        if self.phone and call is not None:
            who = self.phone.phonebook.lookup(call.number) or who
        if name == 'registered':
            self._log_append(f"SIP registered ({info})", "#4ADE80")
        elif name == 'registration_failed':
            self._log_append(f"SIP registration failed: {info}", "#F87171")
        elif name == 'incoming':
            self._log_append(f"Incoming call: {who}", "#FBBF24")
            if self.config.load_settings().get('sip_auto_open', True):
                self._show_call_window()
        elif name == 'connected':
            self._log_append(f"Call connected: {who}", "#4ADE80")
        elif name == 'ended':
            self._log_append(f"Call ended: {who} ({info})", "#94A3B8")
        elif name == 'call_failed':
            self._log_append(f"Call failed: {who} ({info})", "#F87171")
        elif name == 'error':
            self._log_append(f"SIP audio error: {info}", "#F87171")
        if self._call_dlg and self._call_dlg.isVisible():
            self._call_dlg.refresh()
        self.phone_strip.update_state(self.phone)

    def _phone_tick(self):
        """Every 500 ms: refresh the strip and keep the mic stream open while a call needs it."""
        ph = self.phone
        if not ph:
            return
        self.phone_strip.update_state(ph)
        if ph.needs_mic:
            if not self.recorder.is_recording() and not self.recorder.is_monitoring():
                if self.recorder.start_monitoring(self._device_index()):
                    self._phone_mon = True
        elif self._phone_mon:
            if self.recorder.is_monitoring():
                self.recorder.stop_monitoring()
            self._phone_mon = False

    # ── Window events ─────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._save_settings()
        self._stop_phone()
        if self._call_dlg:
            self._call_dlg.close()
        if self.recorder.is_recording():  self.recorder.stop_recording()
        if self.recorder.is_monitoring(): self.recorder.stop_monitoring()
        if self.recorder.is_playing():    self.recorder.stop_playback()
        if self.processing_thread and self.processing_thread.isRunning():
            self.processing_thread.wait(5000)   # max 5s, then let OS clean up
        event.accept()


# ─── Batch processing dialog ──────────────────────────────────────────────────
class BatchWorker(QThread):
    progress  = pyqtSignal(str)        # status message
    file_done = pyqtSignal(str, bool)  # (filename, success)
    finished  = pyqtSignal(int, int)   # (ok_count, fail_count)

    def __init__(self, processor, files, settings):
        super().__init__()
        self.processor = processor
        self.files     = files
        self.settings  = settings
        self._stop     = False

    def stop(self): self._stop = True

    def run(self):
        ok = fail = 0
        s  = self.settings
        try:
            threshold_db = int(s.get('threshold', '-53dB').replace('dB',''))
        except ValueError:
            threshold_db = -53
        preset       = s.get('filter_preset', 'None')
        filter_str   = FILTER_PRESETS.get(preset, '')
        if preset == 'Custom':
            filter_str = s.get('custom_filter', '')
        out_fmt  = 'mp3' if 'MP3' in s.get('format','WAV') else 'wav'
        bitrate  = s.get('mp3_bitrate', '320k')
        save_path = s.get('save_path', '')

        # Open ONE RadioDJ connection for the whole batch (if export enabled)
        rdj_conn = None
        if s.get('radiodj_enabled') and s.get('_rdj_export', False):
            try:
                from modules.radiodj import get_connection
                rdj_conn = get_connection(
                    s.get('radiodj_host','localhost'), s.get('radiodj_port','3306'),
                    s.get('radiodj_user','root'), s.get('radiodj_pass',''),
                    s.get('radiodj_database','radiodj'))
            except Exception as e:
                self.progress.emit(f"⚠ RadioDJ connection failed — exports skipped: {e}")

        for i, fpath in enumerate(self.files):
            if self._stop:
                break
            fname = os.path.basename(fpath)
            self.progress.emit(f"[{i+1}/{len(self.files)}] {fname}")
            try:
                # Convert non-WAV input to temp WAV first
                ext = os.path.splitext(fpath)[1].lower()
                if ext != '.wav':
                    from pydub import AudioSegment
                    tmp_wav = os.path.join(
                        self.processor.path_manager.get_temp_dir(), '_batch_input.wav')
                    AudioSegment.from_file(fpath).export(tmp_wav, format='wav')
                    input_path = tmp_wav
                else:
                    input_path = fpath
                # NOTE: process_audio adapts the filter to the actual
                # channel count internally — do NOT pre-adapt here, that
                # would break aeval filters for stereo input files.
                processed = self.processor.process_audio(
                    input_path,
                    threshold_db     = threshold_db,
                    intro_padding_ms = s.get('intro_padding', 50),
                    outro_padding_ms = s.get('outro_padding', 50),
                    filter_str       = adapted,
                    output_format    = out_fmt,
                    bitrate          = bitrate,
                )

                if not processed:
                    raise RuntimeError("Processing returned None")

                stem = os.path.splitext(fname)[0]
                out_name = f"{stem}.{out_fmt}"
                result = self.processor.save_to_destination(processed, save_path, out_name)
                if not result:
                    raise RuntimeError("Save failed")
                result = os.path.normpath(result)

                # RadioDJ export (reuses the single batch connection)
                if rdj_conn is not None:
                    try:
                        from modules.radiodj import export_to_radiodj
                        rdj_s = {
                            'radio_name':     s.get('radio_name',''),
                            'song_type_id':   s.get('rdj_song_type_id',0),
                            'subcategory_id': s.get('rdj_subcategory_id',0),
                            'genre_id':       s.get('rdj_genre_id',0),
                            'label':          s.get('rdj_label',''),
                            'publisher':      s.get('rdj_publisher',''),
                            'copyright':      s.get('rdj_copyright',''),
                            'comments':       s.get('rdj_comments',''),
                            'overlay':        s.get('rdj_overlay',False),
                            'filename_title': stem,
                        }
                        export_to_radiodj(rdj_conn, result, rdj_s)
                    except Exception as e:
                        self.progress.emit(f"  ⚠ RadioDJ export failed: {e}")

                self.processor.cleanup_temp_files()
                self.file_done.emit(fname, True)
                ok += 1
            except Exception as e:
                self.progress.emit(f"  ❌ Error: {e}")
                self.file_done.emit(fname, False)
                fail += 1

        if rdj_conn is not None:
            try:
                rdj_conn.close()
            except Exception:
                pass

        self.finished.emit(ok, fail)


class BatchDialog(QDialog):
    def __init__(self, processor, config, parent=None):
        super().__init__(parent)
        self.processor = processor
        self.config    = config
        self._worker   = None
        self._files    = []
        self.setWindowTitle("Batch Processing")
        self.setWindowIcon(get_app_icon())
        self.setMinimumSize(560, 480)
        self.setStyleSheet(f"""
            QDialog {{ background:{C_BG}; font-family:'Segoe UI',Arial; color:{C_TEXT}; }}
            QGroupBox {{
                font-size:12px; font-weight:700; color:{C_MUTED};
                border:1px solid {C_BORDER}; border-radius:8px;
                margin-top:10px; padding:10px; background:{C_CARD};
            }}
            {INPUT_CSS}
            QListWidget {{
                border:1px solid {C_BORDER}; border-radius:5px;
                background:{C_CARD}; font-size:12px;
            }}
            QListWidget::item {{ padding:4px 8px; }}
            QListWidget::item:selected {{ background:{C_ACCENT}; color:white; }}
        """)
        self._build()

    def _build(self):
        from PyQt5.QtWidgets import QTextEdit, QListWidget
        root = QVBoxLayout(self)
        root.setContentsMargins(14,14,14,14)
        root.setSpacing(10)

        # Title
        tl = QLabel("📂  Batch Processing")
        tl.setStyleSheet(f"font-size:16px;font-weight:800;color:{C_ACCENT};")
        root.addWidget(tl)

        note = QLabel("Processes multiple audio files (WAV, MP3, FLAC, OGG, M4A) using current Settings "
                      "(threshold, padding, filter, format, save folder).")
        note.setStyleSheet(f"font-size:12px;color:{C_MUTED};")
        note.setWordWrap(True)
        root.addWidget(note)

        # File list
        fg = QGroupBox("Input files")
        fl = QVBoxLayout(fg); fl.setSpacing(6)

        fbtn_row = QHBoxLayout()
        add_files_btn = QPushButton("Add audio files…")
        add_folder_btn = QPushButton("Add folder…")
        clear_btn = QPushButton("Clear")
        add_files_btn.setStyleSheet(btn_style(C_ACCENT, C_ACCENT_H, extra="font-size:12px;padding:6px 10px;"))
        add_folder_btn.setStyleSheet(btn_style(C_ACCENT, C_ACCENT_H, extra="font-size:12px;padding:6px 10px;"))
        clear_btn.setStyleSheet(btn_style(C_MUTED, "#475569", extra="font-size:12px;padding:6px 10px;"))
        add_files_btn.clicked.connect(self._add_files)
        add_folder_btn.clicked.connect(self._add_folder)
        clear_btn.clicked.connect(self._clear_files)
        fbtn_row.addWidget(add_files_btn); fbtn_row.addWidget(add_folder_btn)
        fbtn_row.addStretch(); fbtn_row.addWidget(clear_btn)
        fl.addLayout(fbtn_row)

        self.file_list = QListWidget()
        self.file_list.setFixedHeight(150)
        fl.addWidget(self.file_list)

        self.file_count_lbl = QLabel("No files selected.")
        self.file_count_lbl.setStyleSheet(f"font-size:11px;color:{C_MUTED};")
        fl.addWidget(self.file_count_lbl)
        root.addWidget(fg)

        # RadioDJ export option
        s = self.config.load_settings()
        self.rdj_row = QWidget()
        rdj_rl = QHBoxLayout(self.rdj_row); rdj_rl.setContentsMargins(0,0,0,0)
        from PyQt5.QtWidgets import QCheckBox
        self.rdj_cb = QCheckBox("Export each file to RadioDJ after processing")
        self.rdj_cb.setStyleSheet(f"font-size:13px;font-weight:700;color:{C_ACCENT};")
        rdj_rl.addWidget(self.rdj_cb); rdj_rl.addStretch()
        self.rdj_row.setVisible(s.get('radiodj_enabled', False))
        root.addWidget(self.rdj_row)

        # Progress log
        lg = QGroupBox("Progress")
        ll = QVBoxLayout(lg); ll.setContentsMargins(6,6,6,6)
        self.batch_log = QTextEdit()
        self.batch_log.setReadOnly(True)
        self.batch_log.setFixedHeight(130)
        self.batch_log.setStyleSheet(f"""
            QTextEdit {{
                background:#0F172A; color:#94A3B8;
                font-family:'Consolas','Courier New',monospace;
                font-size:11px; border:none; border-radius:4px; padding:6px;
            }}
        """)
        ll.addWidget(self.batch_log)
        root.addWidget(lg)

        # Progress bar
        self.batch_progress = QProgressBar()
        self.batch_progress.setTextVisible(True)
        self.batch_progress.setStyleSheet(f"""
            QProgressBar {{ border:1px solid {C_BORDER}; border-radius:5px;
                           background:#E2E8F0; min-height:18px; font-size:11px; }}
            QProgressBar::chunk {{ background:{C_ACCENT}; border-radius:4px; }}
        """)
        root.addWidget(self.batch_progress)

        # Buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶  Start Batch")
        self.stop_btn  = QPushButton("⏹  Stop")
        self.close_btn = QPushButton("Close")
        self.start_btn.setStyleSheet(btn_style(C_GREEN, C_GREEN_H, extra="font-size:13px;padding:10px;"))
        self.stop_btn .setStyleSheet(btn_style(C_RED, C_RED_H, extra="font-size:13px;padding:10px;"))
        self.close_btn.setStyleSheet(btn_style(C_MUTED, "#475569", extra="font-size:13px;padding:10px;"))
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_batch)
        self.stop_btn .clicked.connect(self._stop_batch)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.start_btn); btn_row.addWidget(self.stop_btn)
        btn_row.addStretch(); btn_row.addWidget(self.close_btn)
        root.addLayout(btn_row)

    SUPPORTED_EXTS = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wma'}

    def _add_files(self):
        ext_filter = "Audio files (*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.wma);;All files (*.*)"
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select audio files", "", ext_filter)
        for f in files:
            if f not in self._files:
                self._files.append(f)
                self.file_list.addItem(os.path.basename(f))
        self._update_count()

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder with audio files")
        if not folder: return
        for fn in sorted(os.listdir(folder)):
            if os.path.splitext(fn)[1].lower() in self.SUPPORTED_EXTS:
                fp = os.path.join(folder, fn)
                if fp not in self._files:
                    self._files.append(fp)
                    self.file_list.addItem(fn)
        self._update_count()

    def _clear_files(self):
        self._files.clear()
        self.file_list.clear()
        self._update_count()

    def _update_count(self):
        n = len(self._files)
        self.file_count_lbl.setText(
            f"{n} file{'s' if n != 1 else ''} selected." if n else "No files selected.")
        self.batch_progress.setMaximum(max(n, 1))
        self.batch_progress.setValue(0)

    def _log(self, text, color=None):
        if color:
            self.batch_log.append(f'<span style="color:{color};">{text}</span>')
        else:
            self.batch_log.append(text)

    def _start_batch(self):
        if not self._files:
            QMessageBox.warning(self, "No files", "Please add audio files first.")
            return
        s = self.config.load_settings()
        sp = s.get('save_path','').strip()
        if not sp:
            QMessageBox.warning(self, "No save folder",
                                "Please set a save folder in Settings first.")
            return
        if not os.path.exists(sp):
            try: os.makedirs(sp, exist_ok=True)
            except Exception:
                QMessageBox.critical(self,"Error",f"Cannot create save folder:\n{sp}"); return

        s['_rdj_export'] = self.rdj_cb.isChecked()

        self.batch_log.clear()
        self.batch_progress.setMaximum(len(self._files))
        self.batch_progress.setValue(0)
        self._done_count = 0
        self.start_btn.setEnabled(False)
        self.stop_btn .setEnabled(True)
        self.close_btn.setEnabled(False)

        self._log(f"Starting batch: {len(self._files)} files", "#60A5FA")
        self._log(f"Filter: {s.get('filter_preset','None')}  |  "
                  f"Format: {s.get('format','WAV')}  |  "
                  f"Threshold: {s.get('threshold','-53dB')}", "#94A3B8")

        self._worker = BatchWorker(self.processor, list(self._files), s)
        self._worker.progress .connect(lambda m: self._log(m))
        self._worker.file_done.connect(self._on_file_done)
        self._worker.finished .connect(self._on_batch_finished)
        self._worker.start()

    def _on_file_done(self, fname, success):
        self._done_count += 1
        self.batch_progress.setValue(self._done_count)
        color = "#4ADE80" if success else "#F87171"
        icon  = "✓" if success else "✗"
        self._log(f"  {icon} {fname}", color)

    def _on_batch_finished(self, ok, fail):
        self.start_btn.setEnabled(True)
        self.stop_btn .setEnabled(False)
        self.close_btn.setEnabled(True)
        self._log(f"Done — {ok} succeeded, {fail} failed.", "#E2E8F0")
        if fail == 0:
            self.batch_progress.setStyleSheet(
                self.batch_progress.styleSheet().replace(C_ACCENT, C_GREEN))

    def _stop_batch(self):
        if self._worker:
            self._worker.stop()
            self._log("⏹ Stopped by user.", "#F87171")
        self.stop_btn.setEnabled(False)

    def _worker_running(self):
        return self._worker is not None and self._worker.isRunning()

    def closeEvent(self, event):
        """Block closing (X button) while a batch is running."""
        if self._worker_running():
            QMessageBox.warning(self, "Batch running",
                                "A batch is still running.\n"
                                "Press Stop first, then close.")
            event.ignore()
            return
        event.accept()

    def reject(self):
        """Block Esc-close while a batch is running."""
        if self._worker_running():
            return
        super().reject()


# ─── Entry point ─────────────────────────────────────────────────────────────
def main():
    if hasattr(Qt,'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    if hasattr(Qt,'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    load_custom_presets_into_global()
    app = QApplication(sys.argv)
    w   = VoiceTrackMagic()
    w.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
