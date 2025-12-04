import json
import logging
import os
from typing import Dict, List, Optional

from .constants import CONFIG_FILE, DEFAULT_SETTINGS

# Get logger for this module
logger = logging.getLogger(__name__)


class ConfigManager:
    def __init__(self, config_file: str = CONFIG_FILE):
        self.config_file = config_file
        self.config = self.load_config()

    def load_config(self) -> Dict:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded_config = json.load(f)

                # Merge with defaults to ensure all keys exist
                config = dict(DEFAULT_SETTINGS)
                for section, defaults in DEFAULT_SETTINGS.items():
                    if section in loaded_config:
                        if isinstance(defaults, dict):
                            config[section].update(loaded_config[section])
                        else:
                            config[section] = loaded_config[section]

                # Ensure nested defaults are present
                for section, defaults in DEFAULT_SETTINGS.items():
                    if isinstance(defaults, dict):
                        for key, value in defaults.items():
                            if key not in config[section]:
                                config[section][key] = value

                return config
            except Exception as e:
                logger.warning(f"Error loading configuration file '{self.config_file}': {str(e)}. Using defaults.")
                return dict(DEFAULT_SETTINGS)
        else:
            logger.info(f"Configuration file '{self.config_file}' not found. Creating with defaults.")
            new_config = dict(DEFAULT_SETTINGS)
            self.config = new_config
            self.save_config()
            return new_config

    def save_config(self) -> None:
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4, sort_keys=True)
        except Exception as e:
            logger.error(f"Error saving configuration: {str(e)}")

    def get_user_settings(self, username: str) -> Dict:
        user_settings = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULT_SETTINGS.items()}

        if 'user_settings' in self.config and username in self.config['user_settings']:
            user_specific = self.config['user_settings'][username]
            for section, settings in user_specific.items():
                if section in user_settings and isinstance(user_settings[section], dict):
                    user_settings[section].update(settings)
        else:
            for section in ['measurement', 'display', 'file']:
                user_settings[section] = dict(self.config[section])
        return user_settings

    def update_user_settings(self, username: str, settings: Dict) -> None:
        if 'user_settings' not in self.config:
            self.config['user_settings'] = {}
        if username not in self.config['user_settings']:
            self.config['user_settings'][username] = {}

        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file']:
                if section not in self.config['user_settings'][username]:
                    self.config['user_settings'][username][section] = {}
                self.config['user_settings'][username][section] = dict(section_settings)
        self.save_config()

    def update_global_settings(self, settings: Dict) -> None:
        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file'] and isinstance(self.config[section], dict):
                self.config[section].update(section_settings)
        self.save_config()

    def get_users(self) -> List[str]:
        return self.config.get('users', [])

    def get_last_user(self) -> Optional[str]:
        return self.config.get('last_user')

    def add_user(self, username: str) -> None:
        username = username.strip()
        if username and username not in self.config.get('users', []):
            if 'users' not in self.config:
                self.config['users'] = []
            self.config['users'].append(username)
            self.config['users'].sort()
            self.save_config()

    def set_last_user(self, username: str) -> None:
        if username in self.config.get('users', []):
            self.config['last_user'] = username
            self.save_config()

