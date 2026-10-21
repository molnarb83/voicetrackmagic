"""
Configuration Manager for VoiceTrack Magic
Handles saving and loading user settings to/from JSON file.

The RadioDJ password is stored base64-obfuscated (prefix "enc:") so it is
not readable at a glance in config.json. This is obfuscation, not
encryption — anyone with the file can decode it, but it prevents casual
shoulder-surfing and accidental exposure in screenshots.
"""

import json
import os
import shutil
import base64
from .path_manager import PathManager


_PW_PREFIX = "enc:"


def _obfuscate(text):
    if not text:
        return ""
    return _PW_PREFIX + base64.b64encode(text.encode("utf-8")).decode("ascii")


def _deobfuscate(text):
    if not text:
        return ""
    if text.startswith(_PW_PREFIX):
        try:
            return base64.b64decode(text[len(_PW_PREFIX):]).decode("utf-8")
        except Exception:
            return ""
    return text  # legacy plaintext — accepted for backward compatibility


class ConfigManager:
    def __init__(self):
        self.path_manager = PathManager()
        self.config_file = self.path_manager.get_config_file_path()
        self.default_settings = {
            'threshold': '-53dB',
            'intro_padding': 50,
            'outro_padding': 50,
            'filter_preset': 'None',
            'custom_filter': '',
            'format': 'WAV',
            'mp3_bitrate': '320k',
            'save_path': self.path_manager.get_default_save_dir(),
            'device_index': 0,
            'window_geometry': None,
            'custom_filename': '',
            'suffix_type': 'Date/Time',
            'selected_device': '',
            'channel_mode': 'Stereo',
            'output_device_index': None,
            'selected_output_device': '',
            # RadioDJ
            'radiodj_enabled':    False,
            'radiodj_host':       'localhost',
            'radiodj_port':       '3306',
            'radiodj_user':       'root',
            'radiodj_pass':       '',
            'radiodj_database':   'radiodj',
            'radio_name':         '',
            'rdj_song_type_id':   0,
            'rdj_subcategory_id': 0,
            'rdj_genre_id':       0,
            'rdj_label':          '',
            'rdj_publisher':      '',
            'rdj_copyright':      '',
            'rdj_comments':       '',
            'rdj_overlay':        False,
            # SIP phone
            'sip_enabled':        False,
            'sip_user':           '',
            'sip_pass':           '',
            'sip_domain':         '',
            'sip_proxy':          '',
            'sip_port':           5060,
            'sip_auth_user':      '',
            'sip_display_name':   '',
            'sip_input_name':     '',     # '' = same as the recording input
            'sip_output_name':    '',     # '' = same as the Output device setting
            'sip_ring_name':      '',     # '' = silent
            'sip_switch_mode':    'hold', # hold | hangup
            'sip_rec_split':      False,  # False: both channels, True: me left / caller right
            'sip_max_calls':      10,
            'sip_auto_open':      True,
        }
        
        # Perform migration if needed
        self._migrate_settings_if_needed()
    
    def _migrate_settings_if_needed(self):
        """Migrate settings from old location to new AppData location"""
        if not self.path_manager.needs_migration():
            return
        
        old_config_path = self.path_manager.get_old_config_path()
        new_config_path = self.path_manager.get_config_file_path()
        
        try:
            print(f"Migrating settings from {old_config_path} to {new_config_path}")
            
            # Load old settings
            with open(old_config_path, 'r') as f:
                old_settings = json.load(f)
            
            # Handle backward compatibility for old 'padding' key
            if 'padding' in old_settings and 'intro_padding' not in old_settings:
                old_settings['intro_padding'] = old_settings['padding']
                old_settings['outro_padding'] = old_settings.get('outro_padding', old_settings['padding'])
                # Remove old key
                del old_settings['padding']
            
            # Merge with defaults for any missing keys
            migrated_settings = self.default_settings.copy()
            migrated_settings.update(old_settings)
            
            # If old save_path is empty or points to app directory, use new default
            old_save_path = migrated_settings.get('save_path', '')
            if not old_save_path or self._is_app_directory_path(old_save_path):
                migrated_settings['save_path'] = self.path_manager.get_default_save_dir()
            
            # Save to new location
            with open(new_config_path, 'w') as f:
                json.dump(migrated_settings, f, indent=2)
            
            # Create backup of old config file
            backup_path = old_config_path + '.migrated'
            shutil.copy2(old_config_path, backup_path)
            
            print(f"Settings migration completed successfully!")
            print(f"Old config backed up to: {backup_path}")
            print(f"New config location: {new_config_path}")
            
        except Exception as e:
            print(f"Warning: Settings migration failed: {e}")
            print("Using default settings instead")
    
    def _is_app_directory_path(self, path):
        """Check if a path points to the application directory"""
        if not path:
            return False
        
        old_config_dir = os.path.dirname(self.path_manager.get_old_config_path())
        try:
            return os.path.samefile(path, old_config_dir)
        except (OSError, FileNotFoundError):
            return False
    
    def load_settings(self):
        """Load settings from config file"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    settings = json.load(f)
                    
                # Merge with defaults for any missing keys
                merged_settings = self.default_settings.copy()
                merged_settings.update(settings)
                
                # Handle backward compatibility for old 'padding' key
                if 'padding' in settings and 'intro_padding' not in settings:
                    merged_settings['intro_padding'] = settings['padding']
                    merged_settings['outro_padding'] = settings.get('outro_padding', settings['padding'])

                # Transparently decode obfuscated RadioDJ password
                merged_settings['radiodj_pass'] = _deobfuscate(
                    merged_settings.get('radiodj_pass', ''))
                merged_settings['sip_pass'] = _deobfuscate(
                    merged_settings.get('sip_pass', ''))

                return merged_settings
            else:
                return self.default_settings.copy()
                
        except Exception as e:
            print(f"Error loading settings: {e}")
            return self.default_settings.copy()
    
    def save_settings(self, settings):
        """Save settings to config file"""
        try:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)

            # Work on a copy so the caller's dict is not mutated;
            # obfuscate the RadioDJ password before writing to disk
            to_write = dict(settings)
            to_write['radiodj_pass'] = _obfuscate(
                to_write.get('radiodj_pass', ''))
            to_write['sip_pass'] = _obfuscate(
                to_write.get('sip_pass', ''))

            with open(self.config_file, 'w') as f:
                json.dump(to_write, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving settings: {e}")
            return False
    
    def get_default_settings(self):
        """Get default settings"""
        return self.default_settings.copy()
    
    def reset_settings(self):
        """Reset settings to defaults"""
        return self.save_settings(self.default_settings)
    
    def get_path_info(self):
        """Get path information for debugging"""
        return self.path_manager.get_app_info() 