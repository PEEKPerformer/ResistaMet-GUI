import copy
import json
import logging
import os
import socket
from typing import Dict, List, Optional

from .constants import CONFIG_FILE, DEFAULT_SETTINGS

# Get logger for this module
logger = logging.getLogger(__name__)


# Settings keys that are inherently machine-local. They are never stored in
# the shared `measurement` block or in per-user overrides; they live under
# config['machines'][hostname] so a NAS-shared config.json works across lab
# PCs with different instrument wiring.
_MACHINE_LOCAL_MEASUREMENT_KEYS = ('gpib_address',)


def _current_hostname() -> str:
    try:
        return socket.gethostname() or 'unknown_host'
    except Exception:
        return 'unknown_host'


class ConfigManager:
    def __init__(self, config_file: str = CONFIG_FILE, hostname: Optional[str] = None):
        self.config_file = config_file
        self._hostname = hostname or _current_hostname()
        self.config = self.load_config()
        # One-shot: lift any legacy global gpib_address into this host's slot
        # the first time the host opens a NAS-shared config.
        if self._migrate_machine_local():
            self.save_config()

    # --- machine-local layer ---------------------------------------------

    def _machine_entry(self, create: bool = False) -> Dict:
        if create:
            machines = self.config.setdefault('machines', {})
            return machines.setdefault(self._hostname, {})
        return self.config.get('machines', {}).get(self._hostname, {})

    def get_gpib_address(self) -> str:
        """Resolve the instrument address for this machine.

        Lookup order: machines[hostname] → legacy measurement.gpib_address →
        default. The legacy fallback lets a freshly-copied config still work
        until the first save migrates it into the machine slot.
        """
        entry = self._machine_entry()
        if 'gpib_address' in entry:
            return entry['gpib_address']
        legacy = self.config.get('measurement', {}).get('gpib_address')
        if legacy:
            return legacy
        return DEFAULT_SETTINGS['measurement'].get('gpib_address', '')

    def set_gpib_address(self, addr: str) -> None:
        """Persist instrument address to the per-machine slot.

        Also strips any stale copies from the shared measurement block and
        per-user overrides so they cannot shadow the machine entry on
        reload.
        """
        if not addr:
            return
        entry = self._machine_entry(create=True)
        entry['gpib_address'] = addr
        if isinstance(self.config.get('measurement'), dict):
            self.config['measurement'].pop('gpib_address', None)
        for user_overrides in self.config.get('user_settings', {}).values():
            measurement = user_overrides.get('measurement') if isinstance(user_overrides, dict) else None
            if isinstance(measurement, dict):
                measurement.pop('gpib_address', None)
        self.save_config()

    def _migrate_machine_local(self) -> bool:
        entry = self._machine_entry()
        if 'gpib_address' in entry:
            return False
        legacy = self.config.get('measurement', {}).get('gpib_address')
        if not legacy:
            return False
        self._machine_entry(create=True)['gpib_address'] = legacy
        return True

    # --- file IO ----------------------------------------------------------

    def load_config(self) -> Dict:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded_config = json.load(f)

                # Merge with defaults to ensure all keys exist. deepcopy is
                # required — dict() would share nested dicts with the module
                # constant and a later set_gpib_address pop would mutate the
                # defaults globally.
                config = copy.deepcopy(DEFAULT_SETTINGS)
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

                # Preserve any non-default top-level sections (e.g. machines,
                # user_settings, users, last_user).
                for key, value in loaded_config.items():
                    if key not in config:
                        config[key] = value

                return config
            except Exception as e:
                logger.warning(f"Error loading configuration file '{self.config_file}': {str(e)}. Using defaults.")
                return copy.deepcopy(DEFAULT_SETTINGS)
        else:
            logger.info(f"Configuration file '{self.config_file}' not found. Creating with defaults.")
            new_config = copy.deepcopy(DEFAULT_SETTINGS)
            self.config = new_config
            self.save_config()
            return new_config

    def save_config(self) -> None:
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4, sort_keys=True)
        except Exception as e:
            logger.error(f"Error saving configuration: {str(e)}")

    # --- user / global settings ------------------------------------------

    def get_user_settings(self, username: str) -> Dict:
        user_settings = copy.deepcopy(DEFAULT_SETTINGS)

        if 'user_settings' in self.config and username in self.config['user_settings']:
            user_specific = self.config['user_settings'][username]
            for section, settings in user_specific.items():
                if section in user_settings and isinstance(user_settings[section], dict):
                    user_settings[section].update(settings)
        else:
            for section in ['measurement', 'display', 'file', 'output']:
                if section in self.config:
                    user_settings[section] = dict(self.config[section])

        # Machine-local fields always win — they never live in user_settings
        # because the same profile may run on a different PC tomorrow.
        user_settings['measurement']['gpib_address'] = self.get_gpib_address()
        return user_settings

    def update_user_settings(self, username: str, settings: Dict) -> None:
        if 'user_settings' not in self.config:
            self.config['user_settings'] = {}
        if username not in self.config['user_settings']:
            self.config['user_settings'][username] = {}

        # Route machine-local fields to the per-machine slot, never persist
        # them under the user profile.
        measurement_in = settings.get('measurement') if isinstance(settings, dict) else None
        if isinstance(measurement_in, dict) and 'gpib_address' in measurement_in:
            self.set_gpib_address(measurement_in['gpib_address'])

        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file', 'output']:
                if section not in self.config['user_settings'][username]:
                    self.config['user_settings'][username][section] = {}
                stored = dict(section_settings)
                if section == 'measurement':
                    for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
                        stored.pop(key, None)
                self.config['user_settings'][username][section] = stored
        self.save_config()

    def update_global_settings(self, settings: Dict) -> None:
        measurement_in = settings.get('measurement') if isinstance(settings, dict) else None
        if isinstance(measurement_in, dict) and 'gpib_address' in measurement_in:
            self.set_gpib_address(measurement_in['gpib_address'])

        for section, section_settings in settings.items():
            if section in ['measurement', 'display', 'file', 'output'] and isinstance(self.config.get(section), dict):
                incoming = dict(section_settings)
                if section == 'measurement':
                    for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
                        incoming.pop(key, None)
                self.config[section].update(incoming)
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
