import copy
import json
import logging
import os
import socket
import tempfile
import threading
from typing import Dict, List, Optional

from .constants import CONFIG_FILE, DEFAULT_SETTINGS, OUTPUT_RESET_MIGRATION

# Get logger for this module
logger = logging.getLogger(__name__)


# Settings keys that are inherently machine-local. They are never stored in
# the shared `measurement` block or in per-user overrides; they live under
# config['machines'][hostname] so a NAS-shared config.json works across lab
# PCs with different instrument wiring.
_MACHINE_LOCAL_MEASUREMENT_KEYS = ('gpib_address', 'visa_library', 'gpib_interface')


def _current_hostname() -> str:
    try:
        return socket.gethostname() or 'unknown_host'
    except Exception:
        return 'unknown_host'


class ConfigManager:
    def __init__(self, config_file: str = CONFIG_FILE, hostname: Optional[str] = None):
        self.config_file = config_file
        # Guards mutate-and-save. Reentrant because the mutators call
        # save_config while holding it. Never held across anything else —
        # certainly not across instrument I/O.
        self._lock = threading.RLock()
        self._hostname = hostname or _current_hostname()
        self.config = self.load_config()
        # One-shot: lift any legacy global gpib_address into this host's slot
        # the first time the host opens a NAS-shared config.
        dirty = self._migrate_machine_local()
        dirty = self._migrate_output_reset() or dirty
        if dirty:
            self.save_config()

    # --- machine-local layer ---------------------------------------------

    def _machine_entry(self, create: bool = False) -> Dict:
        if create:
            machines = self.config.setdefault('machines', {})
            return machines.setdefault(self._hostname, {})
        return self.config.get('machines', {}).get(self._hostname, {})

    def get_machine_local(self, key: str) -> str:
        """Resolve a machine-local measurement key for this host.

        Lookup order: machines[hostname] → legacy measurement.<key> →
        default. The legacy fallback lets a freshly-copied config still work
        until the first save migrates it into the machine slot.
        """
        entry = self._machine_entry()
        if key in entry:
            return entry[key]
        legacy = self.config.get('measurement', {}).get(key)
        if legacy:
            return legacy
        return DEFAULT_SETTINGS['measurement'].get(key, '')

    def set_machine_local(self, key: str, value: str) -> None:
        """Persist a machine-local key to the per-machine slot.

        Also strips any stale copies from the shared measurement block and
        per-user overrides so they cannot shadow the machine entry on
        reload. An empty value is ignored for keys whose default is
        non-empty: there is no such thing as an empty instrument address.
        Keys whose default is empty (a "use the default" sentinel) accept it.
        """
        with self._lock:
            if not value and DEFAULT_SETTINGS['measurement'].get(key, ''):
                return
            entry = self._machine_entry(create=True)
            entry[key] = value
            if isinstance(self.config.get('measurement'), dict):
                self.config['measurement'].pop(key, None)
            for user_overrides in self.config.get('user_settings', {}).values():
                measurement = user_overrides.get('measurement') if isinstance(user_overrides, dict) else None
                if isinstance(measurement, dict):
                    measurement.pop(key, None)
            self.save_config()

    def get_gpib_address(self) -> str:
        """The instrument address for this machine."""
        return self.get_machine_local('gpib_address')

    def set_gpib_address(self, addr: str) -> None:
        self.set_machine_local('gpib_address', addr)

    def get_visa_library(self) -> str:
        """Which VISA implementation this machine opens the bus with."""
        return self.get_machine_local('visa_library')

    def get_gpib_interface(self) -> str:
        """The GPIB adapter interface resource this machine opens first, or ''."""
        return self.get_machine_local('gpib_interface')

    def _store_machine_local_from(self, measurement_in) -> None:
        """Route any machine-local keys in an incoming measurement block."""
        if not isinstance(measurement_in, dict):
            return
        for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
            if key in measurement_in:
                self.set_machine_local(key, measurement_in[key])

    def _migrate_machine_local(self) -> bool:
        entry = self._machine_entry()
        dirty = False
        for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
            if key in entry:
                continue
            legacy = self.config.get('measurement', {}).get(key)
            if not legacy:
                continue
            self._machine_entry(create=True)[key] = legacy
            dirty = True
        return dirty

    # --- migrations -------------------------------------------------------

    def _migrate_output_reset(self) -> bool:
        """Reset stale Output choices once, when they first take effect.

        Until 1.13 the Output section was saved but never handed to a run, so
        a profile can carry an HDF5 or always-compress choice made long ago
        and never seen. Delivering it (gather_settings_for_mode) would change
        the file format of the next run without warning, so drop those
        overrides once and let the CSV defaults apply.
        """
        if OUTPUT_RESET_MIGRATION in self.config.get('migrations', []):
            return False
        for user_overrides in self.config.get('user_settings', {}).values():
            if isinstance(user_overrides, dict):
                user_overrides.pop('output', None)
        self.config['output'] = copy.deepcopy(DEFAULT_SETTINGS['output'])
        self.config.setdefault('migrations', []).append(OUTPUT_RESET_MIGRATION)
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
        """Write the config, atomically.

        The old in-place rewrite truncated the file first, so a crash — or a
        second writer, now that a session and the GUI can both hold a
        ConfigManager — could leave an empty or half-written config.json and
        lose every profile. Writing a sibling temp file and renaming it means
        a reader sees either the old file or the new one.
        """
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.config_file)) or '.'
            handle = None
            try:
                os.makedirs(directory, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    'w', dir=directory, prefix='.config-', suffix='.tmp', delete=False)
                with handle:
                    json.dump(self.config, handle, indent=4, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                # os.replace is atomic on POSIX and on Windows (unlike rename).
                os.replace(handle.name, self.config_file)
            except Exception as e:
                logger.error(f"Error saving configuration: {str(e)}")
                if handle is not None:
                    try:
                        os.unlink(handle.name)
                    except OSError:
                        pass

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
        for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
            user_settings['measurement'][key] = self.get_machine_local(key)
        return user_settings

    def update_user_settings(self, username: str, settings: Dict) -> None:
        with self._lock:
            if 'user_settings' not in self.config:
                self.config['user_settings'] = {}
            if username not in self.config['user_settings']:
                self.config['user_settings'][username] = {}

            # Route machine-local fields to the per-machine slot, never persist
            # them under the user profile.
            if isinstance(settings, dict):
                self._store_machine_local_from(settings.get('measurement'))

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
        with self._lock:
            if isinstance(settings, dict):
                self._store_machine_local_from(settings.get('measurement'))

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
        with self._lock:
            username = username.strip()
            if username and username not in self.config.get('users', []):
                if 'users' not in self.config:
                    self.config['users'] = []
                self.config['users'].append(username)
                self.config['users'].sort()
                self.save_config()

    def set_last_user(self, username: str) -> None:
        with self._lock:
            if username in self.config.get('users', []):
                self.config['last_user'] = username
                self.save_config()
