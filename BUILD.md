# VoiceTrack Magic — Build Guide

## Quick build (recommended)

1. Copy `ffmpeg.exe` into this folder
   (download **ffmpeg-release-full** from https://www.gyan.dev/ffmpeg/builds/,
   extract `bin\ffmpeg.exe`)
2. Optionally copy `icon.png` here
3. Double-click **`build.bat`**

Result: `dist\VoiceTrackMagic.exe` + `dist\ffmpeg.exe`
**Ship both files together** — the app looks for ffmpeg.exe next to the
EXE first (antivirus software may block the copy bundled inside).

## Manual build

```
pip install -r requirements.txt
pyinstaller VoiceTrackMagic.spec --clean
```

## Runtime notes

- Settings: `%APPDATA%\VoiceTrackMagic\config.json`
  (old EasyTracker settings are migrated automatically)
- Custom filter presets: `%APPDATA%\VoiceTrackMagic\custom_presets.json`
- Temp files: `%LOCALAPPDATA%\VoiceTrackMagic\temp`
- The RadioDJ password is stored base64-obfuscated in config.json
  (not encryption — prevents casual reading only)
- Debug output: run the EXE from a command prompt

## Bundled in the EXE

Python runtime · PyQt5 · pydub · sounddevice+portaudio · numpy · pymysql
· ffmpeg.exe · icon.png

## SIP / phone feature

* Own licence-clean SIP/RTP stack in `sipphone\` (numpy + sounddevice only, G.711 A-law/u-law).
* Settings → **SIP** tab: account, devices (caller input, output, ring), behaviour, phonebook (vCard/CSV import).
* The SIP password is stored obfuscated in config.json (not encrypted).
* Phonebook: `%APPDATA%\VoiceTrackMagic\phonebook.json`.
* `SipTest.exe` (built by GitHub Actions) is a console tool to test the provider without the main app:
  copy `siptest.example.json` to `siptest.json`, fill it in, run `SipTest.exe --verbose`.
  **Never commit siptest.json.**
* Tests: `python -m pytest -q tests` (mock SIP provider, fake sound card, offscreen Qt).

## GitHub Actions

Every push to `main` runs the tests and builds `VoiceTrackMagic.exe` and `SipTest.exe`
(Actions → run → Artifacts). ffmpeg.exe is not bundled in CI: put it next to the EXE.
