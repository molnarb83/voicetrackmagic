"""
PathManager for VoiceTrack Magic
Handles all file paths to ensure proper Windows UAC compliance
"""

import os
import sys
from pathlib import Path


class PathManager:
    """Manages file paths for VoiceTrack Magic to avoid permission issues"""
    
    def __init__(self):
        self.app_name = "VoiceTrackMagic"
        self.legacy_app_name = "EasyTracker"   # for migrating old settings
        self._setup_paths()
    
    def _setup_paths(self):
        """Set up all required paths based on Windows best practices"""
        # Get Windows special folders (with sane fallbacks for non-Windows
        # dev environments where %VARS% are not expanded)
        home = os.path.expanduser('~')
        self.appdata_roaming = os.path.expandvars(r'%APPDATA%')
        if '%' in self.appdata_roaming:
            self.appdata_roaming = os.path.join(home, '.config')
        self.appdata_local = os.path.expandvars(r'%LOCALAPPDATA%')
        if '%' in self.appdata_local:
            self.appdata_local = os.path.join(home, '.local', 'share')
        self.user_documents = os.path.expandvars(r'%USERPROFILE%\Documents')
        if '%' in self.user_documents:
            self.user_documents = os.path.join(home, 'Documents')
        
        # App-specific directories
        self.app_data_dir = os.path.join(self.appdata_roaming, self.app_name)
        self.temp_dir = os.path.join(self.appdata_local, self.app_name, 'temp')
        self.default_save_dir = os.path.join(self.user_documents, self.app_name)
        
        # Ensure directories exist
        self._create_directories()
    
    def _create_directories(self):
        """Create necessary directories if they don't exist"""
        directories = [
            self.app_data_dir,
            self.temp_dir,
            self.default_save_dir
        ]
        
        for directory in directories:
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
                print(f"Created/verified directory: {directory}")
            except Exception as e:
                print(f"Warning: Could not create directory {directory}: {e}")
    
    def get_config_file_path(self):
        """Get the path for the config.json file"""
        return os.path.join(self.app_data_dir, 'config.json')
    
    def get_temp_dir(self):
        """Get the temporary files directory"""
        return self.temp_dir
    
    def get_temp_recording_path(self):
        """Get the path for temporary recording file"""
        return os.path.join(self.temp_dir, 'recording.wav')
    
    def get_default_save_dir(self):
        """Get the default save directory for processed files"""
        return self.default_save_dir
    
    def get_old_config_path(self):
        """Get the old config file path for migration.

        Checks two legacy locations in order:
        1. The old EasyTracker AppData folder (previous app name)
        2. The app directory next to the EXE/script (very old versions)
        """
        # 1. Legacy EasyTracker AppData location
        legacy_appdata = os.path.join(self.appdata_roaming,
                                      self.legacy_app_name, 'config.json')
        if os.path.exists(legacy_appdata):
            return legacy_appdata

        # 2. Config next to EXE/script (oldest versions)
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.dirname(__file__))
        return os.path.join(app_dir, 'config.json')
    
    def is_running_from_program_files(self):
        """Check if the app is running from Program Files directory"""
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
        else:
            exe_path = os.path.abspath(__file__)
        
        exe_path_lower = exe_path.lower()
        return ('program files' in exe_path_lower or 
                'program files (x86)' in exe_path_lower)
    
    def needs_migration(self):
        """Check if settings migration is needed"""
        old_config = self.get_old_config_path()
        new_config = self.get_config_file_path()
        
        return (os.path.exists(old_config) and 
                not os.path.exists(new_config))
    
    def get_app_info(self):
        """Get information about current app paths for debugging"""
        return {
            'running_from_program_files': self.is_running_from_program_files(),
            'app_data_dir': self.app_data_dir,
            'temp_dir': self.temp_dir,
            'default_save_dir': self.default_save_dir,
            'config_file': self.get_config_file_path(),
            'old_config_file': self.get_old_config_path(),
            'needs_migration': self.needs_migration()
        }
