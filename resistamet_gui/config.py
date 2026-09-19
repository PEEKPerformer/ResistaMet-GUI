import copy
import json
import logging
import math
import os
import secrets
import shutil
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .constants import CONFIG_FILE, DEFAULT_SETTINGS, OUTPUT_RESET_MIGRATION

# Get logger for this module
logger = logging.getLogger(__name__)


# Settings keys that are inherently machine-local: which instrument address,
# VISA library and GPIB adapter this PC has. They are never stored in the
# shared `measurement` block or in per-user overrides, because the same
# config.json may be opened from another lab PC with different wiring. They
# live in a file of this machine's own (``default_machine_file``).
_MACHINE_LOCAL_MEASUREMENT_KEYS = ('gpib_address', 'visa_library', 'gpib_interface')

# The sections a user profile can override.
_USER_SECTIONS = ('measurement', 'display', 'file', 'output')


# Top-level lists that are sets of names: two writers' additions are both kept.
_NAME_LISTS = ('users', 'migrations')

#: How long a save waits for another process's save to finish.
_FILE_LOCK_WAIT_S = 5.0

#: os.replace fails with PermissionError on Windows while another process --
#: a sync client, a virus scanner, a second ResistaMet -- has the target open.
_REPLACE_ATTEMPTS = 10
_REPLACE_RETRY_S = 0.1


class ConfigSaveError(OSError):
    """A settings file could not be written; the change is in memory only."""


def default_machine_file() -> str:
    """This machine's own settings file, beside its logs and instrument locks."""
    return str(Path.home() / '.resistamet' / 'machine.json')


def _current_hostname() -> str:
    try:
        return socket.gethostname() or 'unknown_host'
    except Exception:
        return 'unknown_host'


def _same(a, b) -> bool:
    """Equality in which NaN equals NaN ("not measured" is a stored value)."""
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def merge_changes(base: Dict, ours: Dict, theirs: Dict) -> Dict:
    """``theirs`` with the changes made from ``base`` to ``ours`` applied.

    The three-way merge behind :meth:`ConfigManager.save_config`: ``base`` is
    the config as this process last read or wrote it, ``ours`` is what it has
    in memory now, ``theirs`` is what is in the file now. Only the keys this
    process changed are carried over, so another writer's changes to anything
    else survive. Where both changed the same key, ours wins.
    """
    result = copy.deepcopy(theirs)
    for key in list(base) + [key for key in ours if key not in base]:
        if key not in ours:
            result.pop(key, None)
            continue
        mine, was = ours[key], base.get(key)
        if key in base and _same(was, mine):
            continue
        if isinstance(mine, dict) and isinstance(result.get(key), dict):
            result[key] = merge_changes(was if isinstance(was, dict) else {}, mine, result[key])
        else:
            result[key] = copy.deepcopy(mine)
    return result


def _merge_name_list(was, mine, theirs) -> List:
    was = was if isinstance(was, list) else []
    theirs = theirs if isinstance(theirs, list) else []
    removed = [name for name in was if name not in mine]
    merged = [name for name in theirs if name not in removed]
    merged += [name for name in mine if name not in was and name not in merged]
    return merged


def _with_defaults(loaded: Dict) -> Dict:
    """A loaded config with every default section and key present."""
    # deepcopy is required -- dict() would share nested dicts with the module
    # constant and a later pop would mutate the defaults globally.
    config = copy.deepcopy(DEFAULT_SETTINGS)
    for section, defaults in DEFAULT_SETTINGS.items():
        if section in loaded:
            if isinstance(defaults, dict):
                config[section].update(loaded[section])
            else:
                config[section] = loaded[section]
    # Preserve any non-default top-level sections (e.g. machines,
    # user_settings, users, last_user).
    for key, value in loaded.items():
        if key not in config:
            config[key] = value
    return config


def _read_json_object(path: str) -> Dict:
    with open(path, 'r') as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError(f"expected a JSON object, found {type(loaded).__name__}")
    return loaded


def _write_json_atomically(path: str, data: Dict) -> None:
    """Write ``data`` to ``path`` so a reader sees the old file or the new one.

    The temporary file is created with ordinary permissions and then given
    the mode of the file it replaces, so a config that other lab accounts can
    read stays readable. Raises :class:`ConfigSaveError`.
    """
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    temp = None
    try:
        os.makedirs(directory, exist_ok=True)
        temp = os.path.join(directory, f".{os.path.basename(path)}-{secrets.token_hex(6)}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(data, handle, indent=4, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            shutil.copymode(path, temp)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                # os.replace is atomic on POSIX and on Windows (unlike rename).
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_RETRY_S)
    except Exception as e:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
        logger.error(f"Error saving '{path}': {str(e)}")
        raise ConfigSaveError(f"could not save '{path}': {e}") from e


@contextmanager
def _file_lock(path: str):
    """Hold an OS lock on ``path`` for the block; released if the holder dies.

    Best effort: where the lock cannot be taken -- a file system without
    locks, a holder that does not let go -- the block runs anyway, with a
    warning, because a save that never happens loses more than a save that
    races.
    """
    handle = None
    try:
        handle = open(path, 'a+')
        deadline = time.monotonic() + _FILE_LOCK_WAIT_S
        while True:
            try:
                try:
                    import fcntl
                except ImportError:  # Windows
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    except OSError as e:
        logger.warning(f"Saving without the lock '{path}' ({e}); a save by another "
                       "process at the same moment could be lost.")
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                import msvcrt
            except ImportError:
                pass  # POSIX: closing the handle drops the flock
            else:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            handle.close()


class ConfigManager:
    def __init__(self, config_file: str = CONFIG_FILE, hostname: Optional[str] = None,
                 machine_file: Optional[str] = None, raise_on_save_error: bool = False):
        self.config_file = config_file
        #: A save that fails is always logged as an error. A caller that can
        #: tell its user -- the API answers 500 -- also asks for the
        #: exception; the PySide6 dialogs do not handle one yet, and an
        #: unwritable config must not stop them from opening a session.
        self.raise_on_save_error = raise_on_save_error
        #: Where this machine's instrument address, VISA library and GPIB
        #: interface are kept. Not in config.json: that file may be shared
        #: between PCs, and a key derived from the hostname there stops
        #: matching when the hostname changes -- which macOS does by itself,
        #: from the network.
        self.machine_file = machine_file or default_machine_file()
        # Guards mutate-and-save. Reentrant because the mutators call
        # save_config while holding it. Never held across anything else —
        # certainly not across instrument I/O.
        self._lock = threading.RLock()
        self._hostname = hostname or _current_hostname()
        #: True when the file exists but could not be read as a config. The
        #: app then runs on the defaults, in memory, and the file is left
        #: exactly as found until something is deliberately saved.
        self.load_failed = False
        # The config as this manager last read or wrote it: what a save
        # compares with to tell its own changes from everybody else's.
        self._baseline: Dict = {}
        self._machine = self._load_machine_file()
        self.config = self.load_config()
        if not self._baseline:
            self._baseline = copy.deepcopy(self.config)
        if self.load_failed:
            # A migration would "fix" the defaults and save them over the
            # file, which may only be half-synced and whole again in a moment.
            return
        try:
            self._migrate_machine_file()
            if self._migrate_output_reset():
                self.save_config()
        except ConfigSaveError:
            # Already logged. The application still opens, on what is in
            # memory; the migrations are tried again at the next start.
            pass

    # --- machine-local layer ---------------------------------------------

    def _load_machine_file(self) -> Dict:
        if not os.path.exists(self.machine_file):
            return {}
        try:
            with open(self.machine_file, 'r') as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("not a JSON object")
            return loaded
        except Exception as e:
            logger.error(f"Machine settings file '{self.machine_file}' could not be read: "
                         f"{str(e)}. Using the instrument address and VISA defaults.")
            return {}

    def _write(self, path: str, data: Dict) -> None:
        try:
            _write_json_atomically(path, data)
        except ConfigSaveError:
            if self.raise_on_save_error:
                raise
        else:
            if path == self.config_file:
                self._baseline = copy.deepcopy(data)

    def _save_machine_file(self) -> None:
        self._write(self.machine_file, self._machine)

    def _legacy_machine_slot(self) -> Dict:
        """What config.json holds for this hostname, from before the machine file."""
        slot = self.config.get('machines', {}).get(self._hostname, {})
        return slot if isinstance(slot, dict) else {}

    def _migrate_machine_file(self) -> None:
        """Start the machine file from this host's old slot in config.json, once.

        Only while the machine file does not exist, and only values that say
        something: the old code wrote the default address into the slot of
        every host that opened the config, and copying that would pin a
        default. The old slot is left where it is, for a rollback and for an
        older version opening the same config.
        """
        if os.path.exists(self.machine_file):
            return
        found = {}
        for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
            value = self._legacy_machine_slot().get(key)
            if not value:
                value = self.config.get('measurement', {}).get(key)
            if value and value != DEFAULT_SETTINGS['measurement'].get(key, ''):
                found[key] = value
        if found:
            self._machine = found
            self._save_machine_file()
            logger.info(f"Machine settings moved to '{self.machine_file}': {sorted(found)}")

    def get_machine_local(self, key: str) -> str:
        """Resolve a machine-local measurement key for this machine.

        Lookup order: the machine file, then what an older version left in
        config.json (this hostname's ``machines`` slot, then the shared
        ``measurement`` block), then the default.
        """
        if key in self._machine:
            return self._machine[key]
        legacy = self._legacy_machine_slot().get(key) or \
            self.config.get('measurement', {}).get(key)
        if legacy:
            return legacy
        return DEFAULT_SETTINGS['measurement'].get(key, '')

    def set_machine_local(self, key: str, value: str) -> None:
        """Persist a machine-local key to the machine file.

        Also strips any stale copies from the shared measurement block and
        per-user overrides, so a profile never carries one to another PC. An
        empty value is ignored for keys whose default is non-empty: there is
        no such thing as an empty instrument address. Keys whose default is
        empty (a "use the default" sentinel) accept it.
        """
        with self._lock:
            if not self._machine_local_is_settable(key, value):
                return
            self._machine[key] = value
            self._save_machine_file()
            stale = False
            shared = self.config.get('measurement')
            if isinstance(shared, dict) and key in shared:
                shared.pop(key)
                stale = True
            for user_overrides in self.config.get('user_settings', {}).values():
                measurement = user_overrides.get('measurement') if isinstance(user_overrides, dict) else None
                if isinstance(measurement, dict) and key in measurement:
                    measurement.pop(key)
                    stale = True
            if stale:
                self.save_config()

    @staticmethod
    def _machine_local_is_settable(key: str, value) -> bool:
        """False for an empty value of a key that cannot be empty."""
        return bool(value) or not DEFAULT_SETTINGS['measurement'].get(key, '')

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
                return _with_defaults(_read_json_object(self.config_file))
            except Exception as e:
                self.load_failed = True
                logger.error(
                    f"Configuration file '{self.config_file}' could not be read: {str(e)}. "
                    "Running on the defaults, in memory; no user or profile is loaded. "
                    "The file is left as it is, and is copied aside before anything "
                    "is saved over it.")
                return copy.deepcopy(DEFAULT_SETTINGS)
        else:
            logger.info(f"Configuration file '{self.config_file}' not found. Creating with defaults.")
            new_config = copy.deepcopy(DEFAULT_SETTINGS)
            self.config = new_config
            try:
                self.save_config()
            except ConfigSaveError:
                pass  # logged; run on the defaults in memory
            return new_config

    def _set_unreadable_file_aside(self) -> bool:
        """Copy a config that could not be read to ``<name>.corrupt-<UTC time>``.

        Whatever is in it -- a half-synced file, a hand edit gone wrong -- is
        the only record of the lab's profiles, so it is kept byte for byte
        before the defaults are written in its place. False means the copy
        could not be made, and the caller must not write.
        """
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        try:
            for attempt in range(1, 100):
                suffix = '' if attempt == 1 else f'-{attempt}'
                copy_path = f"{self.config_file}.corrupt-{stamp}{suffix}"
                try:
                    with open(self.config_file, 'rb') as source, open(copy_path, 'xb') as target:
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                except FileExistsError:
                    continue
                logger.error(f"The unreadable configuration was kept as '{copy_path}'.")
                return True
            raise OSError("no free name for the copy")
        except OSError as e:
            logger.error(f"Could not copy the unreadable configuration aside ({e}); "
                         f"'{self.config_file}' is not being overwritten.")
            return False

    def _merged_with_disk(self) -> Optional[Dict]:
        """What to write: the file as it is now, with this manager's changes.

        A missing file gets everything in memory. One that cannot be read is
        copied aside first and then gets everything in memory too; None when
        that copy could not be made, and nothing may be written.
        """
        if not os.path.exists(self.config_file):
            return self.config
        try:
            theirs = _with_defaults(_read_json_object(self.config_file))
        except Exception as e:
            logger.error(f"Configuration file '{self.config_file}' cannot be read "
                         f"back for saving: {str(e)}")
            return self.config if self._set_unreadable_file_aside() else None
        merged = merge_changes(self._baseline, self.config, theirs)
        for key in _NAME_LISTS:
            if isinstance(self.config.get(key), list):
                merged[key] = _merge_name_list(self._baseline.get(key), self.config[key],
                                               theirs.get(key))
        if isinstance(merged.get('users'), list):
            merged['users'].sort()
        return merged

    def save_config(self) -> None:
        """Write this manager's changes into the config file, atomically.

        More than one ConfigManager can hold the same file -- the PySide6 app
        and a sidecar, or two lab PCs on a shared folder -- and each read it
        once, at start. Writing memory out whole would undo whatever the
        others saved since. So a save re-reads the file, applies only what
        this manager changed since it last read or wrote it (``merge_changes``),
        writes the result and adopts it, all under an OS lock on
        ``<config>.lock`` so that two saves cannot interleave. Two managers
        that change the same key still end with the later one's value, and
        nothing here refreshes a manager that is not saving.

        The lock excludes processes that see the same file system. Across a
        file-sync service it does not reach the other machine; there the
        merge only narrows the window to the time a sync takes.

        The write is a sibling temp file renamed into place, so a reader sees
        the old file or the new one, never a truncated one.

        A save that fails is logged as an error, and raised as
        :class:`ConfigSaveError` when the manager was built with
        ``raise_on_save_error``, so that a caller able to tell its user can
        say the change was not kept.
        """
        with self._lock, _file_lock(f"{self.config_file}.lock"):
            merged = self._merged_with_disk()
            if merged is None:
                if self.raise_on_save_error:
                    raise ConfigSaveError(
                        f"'{self.config_file}' is unreadable and could not be copied "
                        "aside; it is not being overwritten")
                return
            if merged is not self.config:
                # In place: callers may hold a reference to the top-level dict.
                self.config.clear()
                self.config.update(merged)
            self._write(self.config_file, self.config)

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

    def merge_user_settings(self, username: str, settings: Dict,
                            check: Optional[Callable[[Dict, Dict], None]] = None) -> Dict:
        """Change the keys given and leave every other stored key alone.

        ``update_user_settings`` replaces a section with what it is handed,
        which is right for a caller that sends whole sections (the PySide6
        Settings dialog) and wrong for one that sends only the keys it edited:
        the rest of the section would fall back to the defaults.

        ``check(current, merged)`` sees the user's effective settings before
        and after the change, inside the lock and before anything is stored.
        Whatever it raises propagates and the change is dropped. Returns the
        effective settings after the change.
        """
        with self._lock:
            current = self.get_user_settings(username)
            changes = {}
            machine_local = {}
            for section, incoming in settings.items():
                if section not in _USER_SECTIONS or not isinstance(incoming, dict):
                    continue
                incoming = dict(incoming)
                if section == 'measurement':
                    for key in _MACHINE_LOCAL_MEASUREMENT_KEYS:
                        if key in incoming:
                            machine_local[key] = incoming.pop(key)
                if incoming:
                    changes[section] = incoming

            if check is not None:
                merged = copy.deepcopy(current)
                for section, incoming in changes.items():
                    merged[section].update(incoming)
                for key, value in machine_local.items():
                    if self._machine_local_is_settable(key, value):
                        merged['measurement'][key] = value
                check(current, merged)

            self._store_machine_local_from(machine_local)
            if changes:
                stored = self.config.setdefault('user_settings', {})
                if username not in stored:
                    # Until now this user ran on the shared sections; keep
                    # those values rather than let the first edit swap every
                    # other key for a default.
                    stored[username] = {
                        section: {key: value for key, value in self.config[section].items()
                                  if key not in _MACHINE_LOCAL_MEASUREMENT_KEYS}
                        for section in _USER_SECTIONS
                        if isinstance(self.config.get(section), dict)}
                for section, incoming in changes.items():
                    stored[username].setdefault(section, {}).update(incoming)
                self.save_config()
            return self.get_user_settings(username)

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
