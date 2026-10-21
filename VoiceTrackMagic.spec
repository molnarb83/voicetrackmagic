# -*- mode: python ; coding: utf-8 -*-
#
# VoiceTrack Magic — PyInstaller spec
# Single standalone EXE for Windows 10+ (no external DLLs or installs needed)
#
# BUILD:  run build.bat   (or: pyinstaller VoiceTrackMagic.spec --clean)
#
# Required in this folder before building:
#   ffmpeg.exe  — https://www.gyan.dev/ffmpeg/builds/  (release-full build)
#   icon.png    — app icon (optional but recommended)

import os, glob as _glob
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ── Hidden imports ────────────────────────────────────────────────────────────
hidden = []
hidden += collect_submodules('pydub')
hidden += collect_submodules('numpy')
hidden += collect_submodules('pymysql')
hidden += collect_submodules('sipphone')   # own SIP/RTP stack
hidden += collect_submodules('modules')
hidden += [
    'pymysql', 'pymysql.connections', 'pymysql.cursors',
    'pymysql.converters', 'pymysql.constants',
    'pymysql.constants.CLIENT', 'pymysql.constants.COMMAND',
    'pymysql.constants.ER', 'pymysql.constants.FIELD_TYPE',
    'pymysql.constants.FLAG',
    'sounddevice', 'cffi', '_cffi_backend',
    'webbrowser', 'json', 'glob', 're', 'base64',
]

# ── Data files ────────────────────────────────────────────────────────────────
datas = []
datas += collect_data_files('pydub')

if os.path.exists('icon.png'):
    datas.append(('icon.png', '.'))

# ── Binaries ──────────────────────────────────────────────────────────────────
binaries = []

# portaudio DLL (sounddevice dependency)
try:
    import _sounddevice_data
    pa_dir = os.path.join(os.path.dirname(_sounddevice_data.__file__),
                          'portaudio-binaries')
    for dll in _glob.glob(os.path.join(pa_dir, '*.dll')):
        binaries.append((dll, '_sounddevice_data/portaudio-binaries'))
        print(f"portaudio: {dll}")
except Exception as e:
    print(f"WARNING: portaudio binaries not found: {e}")

# ffmpeg.exe — bundled AND expected next to the EXE (AV-safe search order)
if os.path.exists('ffmpeg.exe'):
    binaries.append(('ffmpeg.exe', '.'))
    print("ffmpeg.exe found ✓")
else:
    print("=" * 60)
    print("WARNING: ffmpeg.exe not found — audio filters will NOT work!")
    print("Download: https://www.gyan.dev/ffmpeg/builds/ (release-full)")
    print("=" * 60)

# ── Icon conversion (PNG → ICO) ───────────────────────────────────────────────
icon_file = None
if os.path.exists('icon.png'):
    try:
        from PIL import Image
        Image.open('icon.png').save(
            'icon.ico', format='ICO',
            sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])
        icon_file = 'icon.ico'
        print("icon.ico created ✓")
    except ImportError:
        print("Pillow not installed — EXE gets default icon (pip install Pillow)")
    except Exception as e:
        print(f"Icon conversion failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'PIL', 'tkinter',
        'IPython', 'jupyter', 'notebook',
        'wx', 'gi', 'gtk', 'pygame',
        'tensorflow', 'torch', 'cv2', 'sklearn',
        'test', 'unittest', 'numpy.f2py',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='VoiceTrackMagic',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        'vcruntime*.dll', 'python*.dll',
        'Qt5*.dll', 'PyQt5*.pyd', 'libportaudio*.dll',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)
