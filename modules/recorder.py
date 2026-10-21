"""
Audio Recording Module for VoiceTrack Magic
Handles microphone input, recording, and internal playback via sounddevice.
"""

import os
import threading
import numpy as np
import sounddevice as sd
from pydub import AudioSegment
import sys
import time
from .path_manager import PathManager


class AudioRecorder:
    CH_STEREO     = "Stereo"
    CH_MONO_MIX   = "Mono (Mix)"
    CH_MONO_LEFT  = "Mono (Left)"
    CH_MONO_RIGHT = "Mono (Right)"

    def __init__(self):
        self.path_manager = PathManager()
        self.recording  = False
        self.monitoring = False
        self.playing    = False
        self.recorded_data      = []
        self.actual_sample_rate = 44100
        self.dtype              = np.float32
        self.temp_file          = self.path_manager.get_temp_recording_path()
        self.channel_mode       = self.CH_STEREO
        self.audio_level        = 0.0
        self.level_smoothing    = 0.8
        self.previous_level     = 0.0
        self.recording_thread   = None
        self.monitor_thread     = None
        self.playback_thread    = None
        self._playback_stop     = threading.Event()
        self._seek_pos          = None   # float 0.0–1.0, set to seek
        self._seek_lock         = threading.Lock()
        self.playback_position  = 0.0   # float 0.0–1.0, updated during playback
        self.on_playback_finished = None
        # Recording safety limit (minutes). 0 = unlimited.
        self.max_recording_minutes = 120
        self._recording_frames  = 0
        self.recording_limit_hit = False
        # Phone integration (set by PhoneController, None when SIP is off):
        #   mic_tap(mono_float32_block, sample_rate)  - copy of the microphone for the caller
        #   call_mix(n_frames, sample_rate) -> float32 array or None - caller audio to record
        self.mic_tap   = None
        self.call_mix  = None
        self.call_split = False   # True: left = you, right = caller (fixed per recording)
        print(f"Using temp directory: {self.path_manager.get_temp_dir()}")

    # ── Device enumeration ────────────────────────────────────────────────────
    def get_input_devices(self):
        host_api_priority = {'MME':0,'DirectSound':1,'WASAPI':2,'ASIO':3,'WDM-KS':4}
        host_apis   = sd.query_hostapis()
        all_devices = sd.query_devices()
        seen = {}
        for i, dev in enumerate(all_devices):
            if dev['max_input_channels'] == 0:
                continue
            name     = dev['name']
            api_name = host_apis[dev['hostapi']]['name']
            if self._should_skip_device(name):
                continue
            priority = host_api_priority.get(api_name, 999)
            if name not in seen or priority < seen[name]['_priority']:
                seen[name] = {
                    'index':i,'name':name,
                    'channels':dev['max_input_channels'],
                    'samplerate':dev['default_samplerate'],
                    'hostapi_name':api_name,'_priority':priority,
                }
        devices = list(seen.values())
        devices.sort(key=lambda d: self._get_device_priority(d['name']))
        return devices

    def get_output_devices(self):
        """Return list of output devices."""
        host_api_priority = {'MME':0,'DirectSound':1,'WASAPI':2,'ASIO':3,'WDM-KS':4}
        host_apis   = sd.query_hostapis()
        all_devices = sd.query_devices()
        seen = {}
        for i, dev in enumerate(all_devices):
            if dev['max_output_channels'] == 0:
                continue
            name     = dev['name']
            api_name = host_apis[dev['hostapi']]['name']
            priority = host_api_priority.get(api_name, 999)
            if name not in seen or priority < seen[name]['_priority']:
                seen[name] = {
                    'index':i, 'name':name,
                    'channels':dev['max_output_channels'],
                    'samplerate':dev['default_samplerate'],
                    'hostapi_name':api_name, '_priority':priority,
                }
        devices = list(seen.values())
        devices.sort(key=lambda d: self._get_output_priority(d['name']))
        return devices

    def _get_output_priority(self, name):
        if 'Speaker' in name or 'Headphone' in name: return 1
        if 'HDMI' in name:                           return 3
        return 2

    def _should_skip_device(self, name):
        """Skip only generic Windows mapper aliases — show all real devices."""
        skip_prefixes = [
            'Microsoft Sound Mapper',
            'Primary Sound',
            'Elsődleges hang',
            'Alapértelmezett',
        ]
        return any(name.startswith(s) for s in skip_prefixes)

    def _get_device_priority(self, name):
        if 'Microphone' in name and 'USB' in name: return 1
        if 'Microphone Array' in name:             return 2
        if 'Microphone' in name:                   return 3
        if 'Stereo Mix' in name:                   return 4
        if 'CABLE Output' in name:                 return 5
        return 6

    # ── Level calculation ─────────────────────────────────────────────────────
    def _calculate_audio_level(self, indata):
        rms = np.sqrt(np.mean(indata ** 2))
        if rms > 0:
            db   = 20 * np.log10(rms)
            norm = max(0.0, min(100.0, (db + 60) * (100 / 60)))
        else:
            norm = 0.0
        smoothed = self.level_smoothing * self.previous_level + (1 - self.level_smoothing) * norm
        self.previous_level = smoothed
        return smoothed / 100.0

    def _mix_channels(self, indata):
        mode = self.channel_mode
        if indata.shape[1] == 1 or mode == self.CH_STEREO:
            return indata
        if mode == self.CH_MONO_LEFT:
            return indata[:, 0:1]
        elif mode == self.CH_MONO_RIGHT:
            idx = min(1, indata.shape[1] - 1)
            return indata[:, idx:idx+1]
        else:  # MIX
            return indata.mean(axis=1, keepdims=True)

    # ── Phone integration helpers ─────────────────────────────────────────────
    def _tap_mic(self, block, sr):
        """Give the caller a copy of what the microphone hears (mono)."""
        tap = self.mic_tap
        if tap is None:
            return
        try:
            mono = block[:, 0] if block.shape[1] == 1 else block.mean(axis=1)
            tap(mono, sr)
        except Exception as e:
            print(f"mic tap error: {e}")

    def _add_call_audio(self, mixed, sr, split):
        """Mix the caller into the recording. split=True: [you | caller] as 2 channels,
        otherwise the caller is added to every channel (dual mono for a mono mix)."""
        cm = self.call_mix
        remote = None
        if cm is not None:
            try:
                remote = cm(len(mixed), sr)
            except Exception as e:
                print(f"call mix error: {e}")
        if split:
            mono = mixed[:, 0:1] if mixed.shape[1] == 1 else mixed.mean(axis=1, keepdims=True)
            r = (remote if remote is not None else np.zeros(len(mixed), dtype=np.float32)).reshape(-1, 1)
            return np.concatenate([mono, r.astype(mixed.dtype)], axis=1)
        if remote is None:
            return mixed
        return mixed + remote.reshape(-1, 1).astype(mixed.dtype)

    # ── Recording ─────────────────────────────────────────────────────────────
    def start_recording(self, device_index=None):
        if self.recording:
            return False
        self.stop_monitoring()
        self.recorded_data = []
        self._recording_frames = 0
        self.recording_limit_hit = False
        self.recording = True
        self.recording_thread = threading.Thread(
            target=self._record_audio, args=(device_index,), daemon=True)
        self.recording_thread.start()
        return True

    def _record_audio(self, device_index):
        try:
            dev_info    = sd.query_devices(device_index)
            sr          = int(dev_info['default_samplerate'])
            hw_channels = dev_info['max_input_channels']
            self.actual_sample_rate = sr

            # Compute max frames from the minute limit (0 = unlimited)
            max_frames = (self.max_recording_minutes * 60 * sr
                          if self.max_recording_minutes > 0 else 0)

            split = bool(self.call_split and self.call_mix)   # layout fixed for the whole recording

            def callback(indata, frames, t, status):
                if status:
                    print(f"Recording status: {status}")
                mixed = self._mix_channels(indata)
                self._tap_mic(mixed, sr)
                mixed = self._add_call_audio(mixed, sr, split)
                self.audio_level = self._calculate_audio_level(mixed)
                self.recorded_data.append(mixed.copy())
                self._recording_frames += len(mixed)
                # Enforce safety limit
                if max_frames and self._recording_frames >= max_frames:
                    self.recording_limit_hit = True
                    self.recording = False

            with sd.InputStream(samplerate=sr, channels=min(hw_channels,2),
                                dtype=self.dtype, callback=callback,
                                device=device_index):
                while self.recording:
                    time.sleep(0.05)
        except Exception as e:
            print(f"Error in recording thread: {e}")
            self.recording = False

    def stop_recording(self):
        if not self.recording and not self.recorded_data:
            return False
        self.recording = False
        if self.recording_thread:
            self.recording_thread.join(timeout=5)
            if self.recording_thread.is_alive():
                print("Warning: recording thread did not stop within 5s")
        if not self.recorded_data:
            return False
        try:
            audio_data  = np.concatenate(self.recorded_data, axis=0)
            out_ch      = audio_data.shape[1] if audio_data.ndim > 1 else 1
            # Clip to [-1, 1] BEFORE int16 conversion — hot signals would
            # otherwise wrap around and produce severe distortion
            audio_data  = np.clip(audio_data, -1.0, 1.0)
            audio_int16 = (audio_data * 32767).astype(np.int16)
            seg = AudioSegment(
                audio_int16.tobytes(),
                frame_rate=self.actual_sample_rate,
                sample_width=2,
                channels=out_ch,
            )
            seg.export(self.temp_file, format="wav")
            self.audio_level = 0.0
            return True
        except Exception as e:
            print(f"Error saving recording: {e}")
            return False

    # ── Monitoring ────────────────────────────────────────────────────────────
    def start_monitoring(self, device_index=None):
        if self.monitoring or self.recording:
            return False
        self.monitoring = True
        self.monitor_thread = threading.Thread(
            target=self._monitor_audio, args=(device_index,), daemon=True)
        self.monitor_thread.start()
        return True

    def _monitor_audio(self, device_index):
        try:
            dev_info    = sd.query_devices(device_index)
            sr          = int(dev_info['default_samplerate'])
            hw_channels = dev_info['max_input_channels']

            def callback(indata, frames, t, status):
                mixed = self._mix_channels(indata)
                self._tap_mic(mixed, sr)
                self.audio_level = self._calculate_audio_level(mixed)

            with sd.InputStream(samplerate=sr, channels=min(hw_channels,2),
                                dtype=self.dtype, callback=callback,
                                device=device_index):
                while self.monitoring:
                    time.sleep(0.05)
        except Exception as e:
            print(f"Error in monitor thread: {e}")
            self.monitoring = False

    def stop_monitoring(self):
        if not self.monitoring:
            return False
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2)
        self.audio_level    = 0.0
        self.previous_level = 0.0
        return True

    # ── Playback (internal, sounddevice) ──────────────────────────────────────
    def play_recording(self, file_path=None, start_pos=0.0, output_device=None):
        """Start playback. start_pos is 0.0–1.0. output_device is device index or None."""
        target = file_path or self.temp_file
        if not os.path.exists(target):
            return False
        if self.playing:
            return True
        self.playing = True
        self._playback_stop.clear()
        self.playback_position = max(0.0, min(1.0, start_pos))
        self._output_device = output_device
        with self._seek_lock:
            self._seek_pos = None
        self.playback_thread = threading.Thread(
            target=self._playback_worker, args=(target,), daemon=True)
        self.playback_thread.start()
        return True

    def seek(self, position):
        """Seek to position (0.0–1.0). Works during playback or when stopped."""
        position = max(0.0, min(1.0, position))
        if self.playing:
            with self._seek_lock:
                self._seek_pos = position
        else:
            self.playback_position = position

    def _playback_worker(self, file_path):
        try:
            seg      = AudioSegment.from_file(file_path)
            samples  = np.array(seg.get_array_of_samples(), dtype=np.float32)
            max_val  = 2 ** (8 * seg.sample_width - 1)
            samples /= max_val
            ch = seg.channels
            if ch > 1:
                samples = samples.reshape(-1, ch)
            total      = len(samples)
            sr         = seg.frame_rate
            chunk_size = int(sr * 0.05)   # 50ms chunks — snappy seek response

            if total == 0:
                print("Playback: empty audio file")
                return

            pos = int(self.playback_position * total)

            with sd.OutputStream(samplerate=sr, channels=ch, dtype='float32',
                                  device=getattr(self, '_output_device', None)) as stream:
                while pos < total and not self._playback_stop.is_set():
                    # Check for pending seek
                    with self._seek_lock:
                        sp = self._seek_pos
                        if sp is not None:
                            pos = int(sp * total)
                            self._seek_pos = None
                            stream.write(np.zeros((chunk_size, ch) if ch > 1
                                                  else (chunk_size,), dtype='float32'))

                    chunk = samples[pos:pos+chunk_size]
                    if len(chunk) == 0:
                        break
                    # Pad last chunk if needed
                    if len(chunk) < chunk_size:
                        pad_shape = (chunk_size - len(chunk), ch) if ch > 1 else (chunk_size - len(chunk),)
                        chunk = np.concatenate([chunk, np.zeros(pad_shape, dtype='float32')])
                    stream.write(chunk)
                    pos += chunk_size
                    self.playback_position = pos / total

        except Exception as e:
            print(f"Error in playback worker: {e}")
        finally:
            self.playing = False
            if self.on_playback_finished:
                self.on_playback_finished()

    def stop_playback(self):
        if not self.playing:
            return False
        self._playback_stop.set()
        self.playing = False
        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join(timeout=2)
        return True

    # ── Misc ──────────────────────────────────────────────────────────────────
    def delete_recording(self):
        try:
            if os.path.exists(self.temp_file):
                os.remove(self.temp_file)
            self.recorded_data  = []
            self.audio_level    = 0.0
            self.previous_level = 0.0
            return True
        except Exception as e:
            print(f"Error deleting recording: {e}")
            return False

    def get_audio_level(self):    return self.audio_level
    def is_recording(self):       return self.recording
    def is_monitoring(self):      return self.monitoring
    def is_playing(self):         return self.playing
    def has_recording(self):      return os.path.exists(self.temp_file)
    def get_recording_path(self):
        return self.temp_file if self.has_recording() else None
