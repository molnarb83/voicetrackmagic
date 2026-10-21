"""
Audio Processing Module for VoiceTrack Magic
Handles silence detection, trimming, padding, and FFmpeg filter application
Now uses PathManager for proper Windows UAC compliance
"""

import os
import sys
import subprocess
from pydub import AudioSegment
from pydub.silence import detect_silence
from .path_manager import PathManager


class AudioProcessor:
    def __init__(self):
        # Initialize path manager
        self.path_manager = PathManager()
        
        self.ffmpeg_path = self._find_ffmpeg()
        
        # Temp directory is created by PathManager
        print(f"Using temp directory: {self.path_manager.get_temp_dir()}")
    
    def _find_ffmpeg(self):
        """Find FFmpeg executable.

        Search order (most reliable first):
        1. Same folder as the EXE (frozen) or project root (dev)
           — antivirus software often blocks execution from the _MEIPASS
             temp folder, so the EXE-adjacent copy is preferred
        2. Current working directory
        3. PyInstaller _MEIPASS bundle (last resort)
        4. System PATH
        """
        candidates = []

        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            candidates.append(os.path.join(exe_dir, "ffmpeg.exe"))
        else:
            script_dir   = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(script_dir)
            candidates.append(os.path.join(project_root, "ffmpeg.exe"))
            candidates.append(os.path.join(script_dir, "ffmpeg.exe"))

        candidates.append(os.path.join(os.getcwd(), "ffmpeg.exe"))

        if hasattr(sys, '_MEIPASS'):
            candidates.append(os.path.join(sys._MEIPASS, "ffmpeg.exe"))

        for path in candidates:
            if os.path.exists(path):
                print(f"FFmpeg found at: {path}")
                return path

        print("FFmpeg not found in known locations, falling back to PATH")
        return "ffmpeg"
    
    def process_audio(self, input_file, threshold_db=-53, intro_padding_ms=100, outro_padding_ms=50, filter_str="", output_format="wav", end_safety_margin_ms=180, bitrate="320k"):
        """Process audio: trim silence, add padding, apply filter, convert format."""
        self._last_trim_info = {
            'start_ms': 0, 'end_ms': 0,
            'filter_ok': True, 'filter_error': ''
        }
        try:
            audio = AudioSegment.from_wav(input_file)

            # Step 1: Trim silence
            audio = self._trim_silence(audio, threshold_db, end_safety_margin_ms)

            # Step 2: Add padding
            audio = self._add_padding(audio, intro_padding_ms, outro_padding_ms)

            # Step 3: Export to temp WAV
            temp_processed = os.path.join(self.path_manager.get_temp_dir(), "temp_processed.wav")
            audio.export(temp_processed, format="wav")

            # Step 4: Apply FFmpeg filter if specified
            if filter_str.strip():
                # Adapt filter string to actual channel count
                adapted_filter = self._adapt_filter_for_channels(
                    filter_str, audio.channels)
                filtered_file, filter_err = self._apply_ffmpeg_filter(
                    temp_processed, adapted_filter)
                if filtered_file:
                    temp_processed = filtered_file
                    self._last_trim_info['filter_ok'] = True
                else:
                    self._last_trim_info['filter_ok']    = False
                    self._last_trim_info['filter_error'] = filter_err or "Unknown error"
                    print(f"Warning: FFmpeg filter failed: {filter_err}")

            # Step 5: Convert format (bitrate passed in for MP3)
            final_file = self._convert_format(temp_processed, output_format, bitrate)
            return final_file

        except Exception as e:
            print(f"Error processing audio: {e}")
            return None

    def _adapt_filter_for_channels(self, filter_str, channels):
        """Rewrite aeval expressions to match the actual channel count."""
        import re
        if channels == 1:
            # Replace stereo aeval (two expressions separated by |) with mono version
            # Pattern: aeval=<expr1>|<expr2>:c=same  →  aeval=<expr1>:c=same
            def mono_aeval(m):
                exprs = m.group(1).split('|')
                # Use only first expression, replace val(0) references correctly
                mono_expr = exprs[0].replace('val(1)', 'val(0)')
                return f"aeval={mono_expr}:c=same"
            filter_str = re.sub(
                r'aeval=([^:]+(?:\|[^:]+)+):c=same',
                mono_aeval,
                filter_str
            )
        return filter_str

    def _trim_silence(self, audio, threshold_db, end_safety_margin_ms):
        """Remove leading and trailing silence using the user-supplied threshold."""
        try:
            start_trim = self._find_start_silence(audio, threshold_db)
            end_trim   = self._find_end_silence(audio, threshold_db, end_safety_margin_ms)
            trimmed_end_ms = len(audio) - end_trim
            print(f"Silence trim: start={start_trim}ms  end={trimmed_end_ms}ms  "
                  f"(threshold={threshold_db}dB)")
            # Store for log panel
            self._last_trim_info['start_ms'] = start_trim
            self._last_trim_info['end_ms']   = trimmed_end_ms
            trimmed = audio[start_trim:end_trim]
            return trimmed if len(trimmed) > 100 else audio
        except Exception as e:
            print(f"Error trimming silence: {e}")
            return audio

    def _find_start_silence(self, audio, threshold_db):
        """Return ms position where leading silence ends (uses user threshold)."""
        try:
            chunks = detect_silence(audio, min_silence_len=50,
                                    silence_thresh=threshold_db)
            # Walk contiguous silence blocks from position 0
            cursor = 0
            for start, end in chunks:
                if start <= cursor:
                    cursor = end
                else:
                    break
            return cursor
        except Exception as e:
            print(f"Error finding start silence: {e}")
            return 0

    def _find_end_silence(self, audio, threshold_db, end_safety_margin_ms):
        """Find silence at end of file and return trim point."""
        try:
            silence_chunks = detect_silence(
                audio,
                min_silence_len=100,
                silence_thresh=threshold_db
            )

            if not silence_chunks:
                return len(audio)

            # Find the last contiguous silence block that reaches (or is near) the end
            # Allow 50ms tolerance since detect_silence may not reach exactly len(audio)
            tolerance_ms = 50
            end_trim = len(audio)
            for start, end in reversed(silence_chunks):
                if end >= len(audio) - tolerance_ms:
                    end_trim = start
                else:
                    break

            # Add safety margin so trailing consonants are not cut
            end_trim = min(end_trim + end_safety_margin_ms, len(audio))

            return end_trim

        except Exception as e:
            print(f"Error in end silence detection: {e}")
            return len(audio)
    
    def _add_padding(self, audio, intro_padding_ms, outro_padding_ms):
        """Add silence padding to start and end"""
        try:
            # Create silence segments
            intro_silence = AudioSegment.silent(duration=intro_padding_ms)
            outro_silence = AudioSegment.silent(duration=outro_padding_ms)
            
            # Add padding to start and end
            padded = intro_silence + audio + outro_silence
            
            return padded
            
        except Exception as e:
            print(f"Error adding padding: {e}")
            return audio
    
    def _apply_ffmpeg_filter(self, input_file, filter_str):
        """Apply FFmpeg filter. Returns (output_path, error_msg) tuple."""
        try:
            output_file = os.path.join(self.path_manager.get_temp_dir(), "filtered.wav")
            cmd = [
                self.ffmpeg_path,
                "-i", input_file,
                "-af", filter_str,
                "-y",
                output_file
            ]
            kwargs = {}
            if sys.platform == "win32":
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, **kwargs)

            if result.returncode == 0 and os.path.exists(output_file):
                return output_file, None
            else:
                # Extract the last meaningful line from stderr
                lines = [l.strip() for l in result.stderr.splitlines() if l.strip()]
                err = lines[-1] if lines else "Unknown FFmpeg error"
                print(f"FFmpeg error: {result.stderr}")
                return None, err

        except subprocess.TimeoutExpired:
            return None, "FFmpeg timed out (>60s)"
        except FileNotFoundError:
            return None, f"FFmpeg not found at: {self.ffmpeg_path}"
        except Exception as e:
            return None, str(e)
    
    def _convert_format(self, input_file, output_format, bitrate="320k"):
        """Convert audio to specified format. Bitrate only used for MP3."""
        try:
            audio = AudioSegment.from_file(input_file)

            if output_format.lower() == "mp3":
                output_file = os.path.join(self.path_manager.get_temp_dir(), "final.mp3")
                audio.export(output_file, format="mp3", bitrate=bitrate)
            else:
                output_file = os.path.join(self.path_manager.get_temp_dir(), "final.wav")
                audio.export(output_file, format="wav")

            return output_file

        except Exception as e:
            print(f"Error converting format: {e}")
            return None
    
    def save_to_destination(self, processed_file, destination_path, filename):
        """Save processed file to final destination"""
        try:
            if not os.path.exists(processed_file):
                return False
            
            # Ensure destination directory exists
            os.makedirs(destination_path, exist_ok=True)
            
            # Copy file to destination
            final_path = os.path.join(destination_path, filename)
            
            # Read and write file
            with open(processed_file, 'rb') as src:
                with open(final_path, 'wb') as dst:
                    dst.write(src.read())
            
            return final_path
            
        except Exception as e:
            print(f"Error saving to destination: {e}")
            return False
    
    def cleanup_temp_files(self):
        """Clean up temporary processing files"""
        temp_files = [
            os.path.join(self.path_manager.get_temp_dir(), "temp_processed.wav"),
            os.path.join(self.path_manager.get_temp_dir(), "filtered.wav"),
            os.path.join(self.path_manager.get_temp_dir(), "final.wav"),
            os.path.join(self.path_manager.get_temp_dir(), "final.mp3"),
            os.path.join(self.path_manager.get_temp_dir(), "_batch_input.wav"),
        ]
        
        for file in temp_files:
            try:
                if os.path.exists(file):
                    os.remove(file)
            except Exception as e:
                print(f"Error cleaning up {file}: {e}")
    
    def test_ffmpeg(self):
        """Test if FFmpeg is working. Returns (ok, error_msg)."""
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True, text=True, timeout=10, **kwargs)
            if result.returncode == 0:
                # Extract version string from first line
                first_line = result.stdout.splitlines()[0] if result.stdout else "ffmpeg OK"
                return True, first_line
            return False, "FFmpeg returned non-zero exit code"
        except FileNotFoundError:
            return False, f"FFmpeg not found at: {self.ffmpeg_path}"
        except Exception as e:
            return False, str(e)
