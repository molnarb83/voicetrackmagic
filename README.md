# VoiceTrack Magic – SIP engine prototype (SipTest)

Licence-clean, own SIP/RTP implementation (UDP, digest auth, G.711 A-law/u-law, RFC 2833 DTMF).
Dependencies: numpy (BSD), sounddevice (MIT, bundles PortAudio, MIT).

## What it tests
* registration with username/password (digest MD5 / SHA-256), automatic re-registration, NAT keep-alive
* several simultaneous incoming calls (all ring), answer / reject / hold / hang up any line
* switching lines: the previous line is put on hold (or hung up – `mode hangup`)
* outgoing calls (`dial NUMBER`), DTMF
* manual recording: mic + active call mixed, dual-mono by default (`split on` = you left / caller right)
* ring tone on its own output device (`ring_device`), or silent (`null`)

## Use
1. Copy `siptest.example.json` to `siptest.json`, fill in user/password (the password can also be set in the
   `SIP_PASSWORD` environment variable). **Never commit siptest.json.**
2. `SipTest.exe --list-devices` shows device numbers for `input_device`, `output_device`, `ring_device`.
3. `SipTest.exe`  (add `--verbose` for SIP debug output, `--no-audio` for signalling only)

Recording here is 8 kHz (telephone band). The main application will upsample to the normal recording rate.
