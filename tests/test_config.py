"""
Unit tests for the ConfigManager class.

Tests cover:
- Loading and saving configuration
- User management
- Settings merging
- Default value handling
"""

import json
import os
import time

import pytest
from pathlib import Path

from resistamet_gui.config import ConfigManager
from resistamet_gui.constants import DEFAULT_SETTINGS


@pytest.fixture
def temp_config_file(tmp_path):
    """Create a temporary config file path."""
    return str(tmp_path / "test_config.json")


@pytest.fixture
def machine_file(tmp_path):
    """This machine's settings file; ``other_machine_file`` is another PC's."""
    return str(tmp_path / "machine.json")


@pytest.fixture
def other_machine_file(tmp_path):
    return str(tmp_path / "other-pc" / "machine.json")


@pytest.fixture
def config_manager(temp_config_file):
    """Create a ConfigManager with a temporary config file."""
    return ConfigManager(config_file=temp_config_file)


class TestConfigManagerInit:
    """Tests for ConfigManager initialization."""

    def test_creates_default_config(self, temp_config_file):
        """Test that default config is created if file doesn't exist."""
        assert not os.path.exists(temp_config_file)
        manager = ConfigManager(config_file=temp_config_file)

        # Config should have default sections
        assert 'measurement' in manager.config
        assert 'display' in manager.config
        assert 'file' in manager.config
        assert 'users' in manager.config

    def test_loads_existing_config(self, temp_config_file):
        """Test loading an existing config file."""
        # Create a config file with custom value
        custom_config = {
            **DEFAULT_SETTINGS,
            'measurement': {**DEFAULT_SETTINGS['measurement'], 'sampling_rate': 99.0}
        }
        with open(temp_config_file, 'w') as f:
            json.dump(custom_config, f)

        manager = ConfigManager(config_file=temp_config_file)
        assert manager.config['measurement']['sampling_rate'] == 99.0

    def test_handles_corrupted_config(self, temp_config_file):
        """Test that corrupted config falls back to defaults."""
        # Write invalid JSON
        with open(temp_config_file, 'w') as f:
            f.write("not valid json {{{")

        # Should load defaults without crashing
        manager = ConfigManager(config_file=temp_config_file)
        assert 'measurement' in manager.config


class TestUserManagement:
    """Tests for user management functions."""

    def test_get_users_empty(self, config_manager):
        """Test getting users when none exist."""
        users = config_manager.get_users()
        assert users == []

    def test_add_user(self, config_manager):
        """Test adding a new user."""
        config_manager.add_user("test_user")
        users = config_manager.get_users()
        assert "test_user" in users

    def test_add_duplicate_user(self, config_manager):
        """Test that adding duplicate user doesn't create duplicates."""
        config_manager.add_user("test_user")
        config_manager.add_user("test_user")
        users = config_manager.get_users()
        assert users.count("test_user") == 1

    def test_get_last_user(self, config_manager):
        """Test getting and setting last user."""
        assert config_manager.get_last_user() is None

        # set_last_user requires the user to exist in the users list
        config_manager.add_user("test_user")
        config_manager.set_last_user("test_user")
        assert config_manager.get_last_user() == "test_user"


class TestUserSettings:
    """Tests for user settings management."""

    def test_get_user_settings_new_user(self, config_manager):
        """Test getting settings for a new user returns defaults."""
        config_manager.add_user("new_user")
        settings = config_manager.get_user_settings("new_user")

        for section in ('measurement', 'display', 'file', 'output'):
            assert settings[section], section
        assert settings['measurement']['nplc'] == DEFAULT_SETTINGS['measurement']['nplc']
        assert settings['display'] == DEFAULT_SETTINGS['display']
        assert settings['file'] == DEFAULT_SETTINGS['file']

    def test_update_user_settings(self, config_manager):
        """Test updating user-specific settings."""
        config_manager.add_user("test_user")

        new_settings = {
            'measurement': {'sampling_rate': 50.0},
            'display': {},
            'file': {}
        }
        config_manager.update_user_settings("test_user", new_settings)

        settings = config_manager.get_user_settings("test_user")
        assert settings.get('measurement', {}).get('sampling_rate') == 50.0

    def test_update_replaces_the_section_it_is_given(self, config_manager):
        """The PySide6 dialog sends whole sections and relies on this."""
        config_manager.add_user("test_user")
        config_manager.update_user_settings(
            "test_user", {'measurement': {'sampling_rate': 50.0, 'nplc': 2.0}})

        config_manager.update_user_settings("test_user", {'measurement': {'nplc': 5.0}})

        stored = config_manager.config['user_settings']['test_user']['measurement']
        assert stored == {'nplc': 5.0}


class TestMergeUserSettings:
    """Per-key edits: what a client that sends only its changes needs."""

    @pytest.fixture
    def manager(self, config_manager):
        config_manager.add_user("test_user")
        config_manager.update_user_settings("test_user", {
            'measurement': {'sampling_rate': 50.0, 'fpp_current': 5e-5},
            'file': {'auto_save_interval': 120}})
        return config_manager

    def test_other_keys_of_the_section_survive(self, manager, temp_config_file):
        merged = manager.merge_user_settings("test_user", {'measurement': {'nplc': 2.0}})

        assert merged['measurement']['nplc'] == 2.0
        assert merged['measurement']['fpp_current'] == 5e-5
        stored = ConfigManager(config_file=temp_config_file).config['user_settings']['test_user']
        assert stored['measurement'] == {'sampling_rate': 50.0, 'fpp_current': 5e-5, 'nplc': 2.0}
        assert stored['file'] == {'auto_save_interval': 120}

    def test_machine_local_keys_go_to_the_machine_and_nothing_else_moves(self, manager):
        before = json.loads(json.dumps(manager.config['user_settings']))

        manager.merge_user_settings(
            "test_user", {'measurement': {'gpib_address': 'GPIB0::7::INSTR'}})

        assert manager.config['user_settings'] == before
        assert manager.get_gpib_address() == 'GPIB0::7::INSTR'

    def test_a_refused_change_stores_nothing(self, manager):
        before = json.loads(json.dumps(manager.config))
        seen = {}

        def check(current, merged):
            seen['current'] = current['measurement']['nplc']
            seen['merged'] = merged['measurement']['nplc']
            seen['address'] = merged['measurement']['gpib_address']
            raise ValueError("no")

        with pytest.raises(ValueError):
            manager.merge_user_settings("test_user", {'measurement': {
                'nplc': 2.0, 'gpib_address': 'GPIB0::7::INSTR'}}, check=check)

        assert seen == {'current': DEFAULT_SETTINGS['measurement']['nplc'], 'merged': 2.0,
                        'address': 'GPIB0::7::INSTR'}
        assert json.loads(json.dumps(manager.config)) == before

    def test_first_edit_keeps_the_shared_values_the_user_ran_on(self, config_manager):
        config_manager.update_global_settings({'measurement': {'sampling_rate': 3.0}})
        config_manager.add_user("new_user")

        merged = config_manager.merge_user_settings("new_user", {'measurement': {'nplc': 2.0}})

        assert merged['measurement']['sampling_rate'] == 3.0
        stored = config_manager.config['user_settings']['new_user']['measurement']
        assert 'gpib_address' not in stored


class TestConfigPersistence:
    """Tests for config file persistence."""

    def test_save_and_reload(self, temp_config_file):
        """Test that config persists across manager instances."""
        # Create manager and modify config
        manager1 = ConfigManager(config_file=temp_config_file)
        manager1.add_user("persistent_user")
        manager1.set_last_user("persistent_user")
        manager1.save_config()

        # Create new manager instance
        manager2 = ConfigManager(config_file=temp_config_file)

        # Should have the user from first manager
        assert "persistent_user" in manager2.get_users()
        assert manager2.get_last_user() == "persistent_user"

    def test_auto_save_on_user_add(self, temp_config_file):
        """Test that adding a user auto-saves the config."""
        manager1 = ConfigManager(config_file=temp_config_file)
        manager1.add_user("auto_save_user")

        # Load fresh manager
        manager2 = ConfigManager(config_file=temp_config_file)
        assert "auto_save_user" in manager2.get_users()


class TestUnreadableConfig:
    """A half-synced config.json must not be replaced by the defaults."""

    @pytest.fixture
    def truncated(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file)
        manager.add_user("alice")
        manager.update_user_settings("alice", {'measurement': {'fpp_current': 5e-5}})
        whole = Path(temp_config_file).read_bytes()
        Path(temp_config_file).write_bytes(whole[:len(whole) // 2])
        return whole[:len(whole) // 2]

    def _copies(self, temp_config_file):
        return sorted(Path(temp_config_file).parent.glob('test_config.json.corrupt-*'))

    def test_opening_it_writes_nothing(self, temp_config_file, truncated, caplog):
        with caplog.at_level('ERROR', logger='resistamet_gui.config'):
            manager = ConfigManager(config_file=temp_config_file)

        assert manager.load_failed is True
        assert Path(temp_config_file).read_bytes() == truncated
        assert self._copies(temp_config_file) == []
        assert manager.get_users() == []
        assert manager.config['measurement'] == DEFAULT_SETTINGS['measurement']
        assert 'defaults' in caplog.text

    def test_the_first_save_keeps_a_copy_of_what_was_there(self, temp_config_file, truncated):
        manager = ConfigManager(config_file=temp_config_file)

        manager.add_user("bob")

        copies = self._copies(temp_config_file)
        assert len(copies) == 1
        assert copies[0].read_bytes() == truncated
        assert "bob" in ConfigManager(config_file=temp_config_file).get_users()

    def test_later_saves_make_no_more_copies(self, temp_config_file, truncated):
        manager = ConfigManager(config_file=temp_config_file)
        manager.add_user("bob")
        manager.add_user("carol")

        assert len(self._copies(temp_config_file)) == 1
        reloaded = ConfigManager(config_file=temp_config_file)
        assert reloaded.load_failed is False
        assert reloaded.get_users() == ["bob", "carol"]

    def test_json_that_is_not_a_config_counts_as_unreadable(self, temp_config_file):
        Path(temp_config_file).write_text('[1, 2, 3]')

        manager = ConfigManager(config_file=temp_config_file)

        assert manager.load_failed is True
        assert Path(temp_config_file).read_text() == '[1, 2, 3]'

    def test_a_good_file_is_not_flagged(self, temp_config_file):
        ConfigManager(config_file=temp_config_file).add_user("alice")
        assert ConfigManager(config_file=temp_config_file).load_failed is False


class TestSaveRobustness:
    """A save either lands whole, with the file's mode kept, or says it failed."""

    @pytest.mark.skipif(os.name == 'nt', reason="POSIX permission bits")
    def test_the_files_mode_survives_a_save(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file)
        os.chmod(temp_config_file, 0o664)

        manager.add_user("alice")

        assert os.stat(temp_config_file).st_mode & 0o777 == 0o664

    @pytest.mark.skipif(os.name == 'nt', reason="POSIX permission bits")
    def test_a_new_file_is_not_private_to_its_creator(self, temp_config_file):
        previous = os.umask(0o022)
        try:
            ConfigManager(config_file=temp_config_file)
        finally:
            os.umask(previous)
        assert os.stat(temp_config_file).st_mode & 0o777 == 0o644

    def test_a_briefly_held_file_is_retried(self, temp_config_file, monkeypatch):
        """On Windows os.replace fails while a sync client has the file open."""
        from resistamet_gui import config as config_module
        manager = ConfigManager(config_file=temp_config_file)
        real_replace, failures = os.replace, []

        def held_twice(source, target):
            if len(failures) < 2:
                failures.append(target)
                raise PermissionError(13, "sharing violation")
            return real_replace(source, target)

        monkeypatch.setattr(config_module.os, 'replace', held_twice)
        monkeypatch.setattr(config_module, '_REPLACE_RETRY_S', 0.0)

        manager.add_user("alice")

        assert len(failures) == 2
        assert "alice" in ConfigManager(config_file=temp_config_file).get_users()

    def test_a_failed_save_is_raised_and_leaves_no_litter(self, temp_config_file,
                                                          monkeypatch, caplog):
        from resistamet_gui import config as config_module
        from resistamet_gui.config import ConfigSaveError
        manager = ConfigManager(config_file=temp_config_file, raise_on_save_error=True)
        before = Path(temp_config_file).read_bytes()

        def always_held(source, target):
            raise PermissionError(13, "sharing violation")

        monkeypatch.setattr(config_module.os, 'replace', always_held)
        monkeypatch.setattr(config_module, '_REPLACE_RETRY_S', 0.0)

        with caplog.at_level('ERROR', logger='resistamet_gui.config'):
            with pytest.raises(ConfigSaveError):
                manager.add_user("alice")

        assert 'sharing violation' in caplog.text
        assert Path(temp_config_file).read_bytes() == before
        assert [p.name for p in Path(temp_config_file).parent.iterdir()
                if p.name.endswith('.tmp')] == []

    def test_a_failed_save_is_always_logged(self, temp_config_file, monkeypatch, caplog):
        """The PySide6 dialogs do not ask for the exception; the log still says."""
        from resistamet_gui import config as config_module
        manager = ConfigManager(config_file=temp_config_file)

        def always_held(source, target):
            raise PermissionError(13, "sharing violation")

        monkeypatch.setattr(config_module.os, 'replace', always_held)
        monkeypatch.setattr(config_module, '_REPLACE_RETRY_S', 0.0)

        with caplog.at_level('ERROR', logger='resistamet_gui.config'):
            manager.add_user("alice")

        assert 'sharing violation' in caplog.text
        assert manager.get_users() == ["alice"]

    def test_opening_does_not_fail_where_nothing_can_be_written(self, tmp_path, monkeypatch):
        from resistamet_gui import config as config_module

        def refuse(path, data):
            raise config_module.ConfigSaveError("read-only")

        monkeypatch.setattr(config_module, '_write_json_atomically', refuse)

        manager = ConfigManager(config_file=str(tmp_path / 'config.json'),
                                raise_on_save_error=True)

        assert manager.get_users() == []


class TestReadOnly:
    """A diagnostic looks at the configuration; it does not change it."""

    def test_a_missing_config_is_not_created(self, temp_config_file, machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                                read_only=True)

        assert manager.get_gpib_address() == DEFAULT_SETTINGS['measurement']['gpib_address']
        assert list(Path(temp_config_file).parent.iterdir()) == []

    def test_no_migration_touches_an_old_config(self, temp_config_file, machine_file):
        old = {'users': ['alice'], 'machines': {'HOST-A': {'visa_library': '@py'}},
               'user_settings': {'alice': {'output': {'format': 'hdf5'}}}}
        Path(temp_config_file).write_text(json.dumps(old))

        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                                hostname='HOST-A', read_only=True)

        assert manager.get_visa_library() == '@py'
        assert json.loads(Path(temp_config_file).read_text()) == old
        assert not os.path.exists(machine_file)
        assert sorted(p.name for p in Path(temp_config_file).parent.iterdir()) == \
            ['test_config.json']

    def test_it_refuses_to_save(self, temp_config_file, machine_file):
        from resistamet_gui.config import ConfigSaveError
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                                read_only=True)
        with pytest.raises(ConfigSaveError):
            manager.add_user('alice')
        assert not os.path.exists(temp_config_file)


class TestOpeningWithoutWriting:
    """persist_on_open=False: migrated in memory, on disk only with a real save."""

    OLD = {'users': ['alice'], 'machines': {'HOST-A': {'visa_library': '@py'}},
           'user_settings': {'alice': {'output': {'format': 'hdf5'},
                                       'measurement': {'nplc': 2.0}}}}

    def test_a_missing_config_is_not_created(self, temp_config_file, machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                                persist_on_open=False)
        assert manager.get_users() == []
        assert list(Path(temp_config_file).parent.iterdir()) == []

    def test_an_old_config_is_migrated_in_memory_only(self, temp_config_file, machine_file):
        Path(temp_config_file).write_text(json.dumps(self.OLD))

        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                                hostname='HOST-A', persist_on_open=False)

        assert manager.get_user_settings('alice')['output']['format'] == 'csv'
        assert manager.get_visa_library() == '@py'
        assert json.loads(Path(temp_config_file).read_text()) == self.OLD
        assert sorted(p.name for p in Path(temp_config_file).parent.iterdir()) == \
            ['test_config.json']

    def test_the_first_save_carries_the_migration(self, temp_config_file, machine_file):
        Path(temp_config_file).write_text(json.dumps(self.OLD))
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                                persist_on_open=False)

        manager.add_user('bob')

        saved = json.loads(Path(temp_config_file).read_text())
        assert saved['users'] == ['alice', 'bob']
        assert saved['migrations'] == ['output_reset_1_13']
        assert saved['user_settings']['alice'] == {'measurement': {'nplc': 2.0}}


class TestDefaultMerging:
    """Tests for merging defaults with loaded config."""

    def test_missing_keys_filled_with_defaults(self, temp_config_file):
        """Test that missing keys are filled with defaults."""
        # Create config with missing keys
        partial_config = {
            'measurement': {'sampling_rate': 20.0},
            'users': []
        }
        with open(temp_config_file, 'w') as f:
            json.dump(partial_config, f)

        manager = ConfigManager(config_file=temp_config_file)

        # Should have custom value
        assert manager.config['measurement']['sampling_rate'] == 20.0

        # Should have default for missing keys
        assert 'nplc' in manager.config['measurement']

    def test_nested_defaults_merged(self, temp_config_file):
        """Test that nested default values are properly merged."""
        # Config with partial measurement settings
        partial_config = {
            'measurement': {'gpib_address': 'GPIB0::25::INSTR'},
            'display': {},
            'file': {},
            'users': []
        }
        with open(temp_config_file, 'w') as f:
            json.dump(partial_config, f)

        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')

        # Legacy resolves through machine-local fallback
        assert manager.get_gpib_address() == 'GPIB0::25::INSTR'

        # Default values filled in
        assert 'res_test_current' in manager.config['measurement']


class TestMachineLocalGpib:
    """The instrument address is per-machine, not per-user or per-config."""

    def test_get_falls_back_to_default(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        assert manager.get_gpib_address() == DEFAULT_SETTINGS['measurement']['gpib_address']

    def test_set_writes_to_the_machine_file_not_the_config(self, temp_config_file, machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        manager.set_gpib_address('GPIB0::25::INSTR')

        with open(machine_file) as f:
            assert json.load(f) == {'gpib_address': 'GPIB0::25::INSTR'}
        with open(temp_config_file) as f:
            assert 'machines' not in json.load(f)

    def test_different_machines_resolve_independently(self, temp_config_file, machine_file,
                                                      other_machine_file):
        """One shared config.json, two PCs."""
        pc_a = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        pc_a.set_gpib_address('GPIB0::25::INSTR')

        pc_b = ConfigManager(config_file=temp_config_file, machine_file=other_machine_file)
        pc_b.set_gpib_address('TCPIP0::192.168.1.10::inst0::INSTR')

        pc_a_reload = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        pc_b_reload = ConfigManager(config_file=temp_config_file, machine_file=other_machine_file)
        assert pc_a_reload.get_gpib_address() == 'GPIB0::25::INSTR'
        assert pc_b_reload.get_gpib_address() == 'TCPIP0::192.168.1.10::inst0::INSTR'

    def test_a_hostname_change_does_not_lose_the_settings(self, temp_config_file, machine_file):
        """macOS derives the hostname from the network; it changes by itself."""
        before = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                               hostname='Brendens-Laptop.local')
        before.set_gpib_address('GPIB0::5::INSTR')
        before.set_machine_local('visa_library', '@py')

        after = ConfigManager(config_file=temp_config_file, machine_file=machine_file,
                              hostname='Mac')

        assert after.get_gpib_address() == 'GPIB0::5::INSTR'
        assert after.get_visa_library() == '@py'

    def test_legacy_shared_address_still_resolves(self, temp_config_file, machine_file):
        legacy_config = {
            'measurement': {'gpib_address': 'GPIB0::7::INSTR'},
            'users': [],
        }
        with open(temp_config_file, 'w') as f:
            json.dump(legacy_config, f)

        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file)

        assert manager.get_gpib_address() == 'GPIB0::7::INSTR'
        with open(machine_file) as f:
            assert json.load(f) == {'gpib_address': 'GPIB0::7::INSTR'}

    def test_set_clears_legacy_and_user_copies(self, temp_config_file):
        polluted = {
            'measurement': {'gpib_address': 'GPIB0::99::INSTR'},
            'user_settings': {
                'alice': {'measurement': {'gpib_address': 'GPIB0::77::INSTR', 'sampling_rate': 50.0}}
            },
            'users': ['alice'],
        }
        with open(temp_config_file, 'w') as f:
            json.dump(polluted, f)

        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.set_gpib_address('GPIB0::25::INSTR')

        with open(temp_config_file) as f:
            saved = json.load(f)
        assert 'gpib_address' not in saved.get('measurement', {})
        assert 'gpib_address' not in saved['user_settings']['alice']['measurement']
        # Unrelated user fields are preserved
        assert saved['user_settings']['alice']['measurement']['sampling_rate'] == 50.0

    def test_get_user_settings_injects_machine_address(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.add_user('alice')
        manager.set_gpib_address('GPIB0::25::INSTR')

        settings = manager.get_user_settings('alice')
        assert settings['measurement']['gpib_address'] == 'GPIB0::25::INSTR'

    def test_update_user_settings_routes_address(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.add_user('alice')

        manager.update_user_settings('alice', {
            'measurement': {'gpib_address': 'GPIB0::25::INSTR', 'sampling_rate': 42.0},
            'display': {},
            'file': {},
        })

        # Address landed in machine slot, not in the user profile
        assert manager.get_gpib_address() == 'GPIB0::25::INSTR'
        stored = manager.config['user_settings']['alice']['measurement']
        assert 'gpib_address' not in stored
        assert stored['sampling_rate'] == 42.0

    def test_update_global_settings_routes_address(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.update_global_settings({
            'measurement': {'gpib_address': 'GPIB0::25::INSTR', 'sampling_rate': 42.0},
        })
        assert manager.get_gpib_address() == 'GPIB0::25::INSTR'
        # The address didn't leak into the shared measurement block, which
        # holds at most the default every load fills in.
        assert manager.config['measurement'].get('gpib_address') in (
            None, DEFAULT_SETTINGS['measurement']['gpib_address'])
        assert manager.config['measurement']['sampling_rate'] == 42.0


class TestTheSuiteStaysOutOfTheRealHome:
    """The machine file's real home is ~/.resistamet; no test may reach it.

    conftest's autouse ``_private_machine_settings`` redirects the default.
    If that fixture is removed or renamed, these fail, instead of some test
    quietly writing a simulated address into the developer's own settings.
    """

    def test_the_default_path_is_redirected(self):
        from resistamet_gui import config as config_module

        default = Path(config_module.default_machine_file()).resolve()
        real = (Path.home() / '.resistamet').resolve()

        assert real != default.parent and real not in default.parents

    def test_a_manager_built_without_a_path_gets_the_redirected_one(self, temp_config_file):
        from resistamet_gui import config as config_module

        manager = ConfigManager(config_file=temp_config_file)
        manager.set_gpib_address('GPIB0::25::INSTR')

        assert manager.machine_file == config_module.default_machine_file()
        assert Path(manager.machine_file).exists()
        assert not (Path.home() / '.resistamet' / 'machine.json').exists() or \
            'GPIB0::25::INSTR' not in (Path.home() / '.resistamet' / 'machine.json').read_text()


class TestMachineFileMigration:
    """The first open after the move takes this host's old slot along, once."""

    def _config_with_slots(self, path, slots, measurement=None):
        with open(path, 'w') as f:
            json.dump({'users': ['alice'], 'machines': slots,
                       'measurement': measurement or {}}, f)

    def test_this_hosts_slot_is_copied_and_left_in_place(self, temp_config_file, machine_file):
        slots = {'HOST-A': {'gpib_address': 'GPIB0::5::INSTR', 'visa_library': '@py'},
                 'HOST-B': {'gpib_address': 'GPIB0::9::INSTR'}}
        self._config_with_slots(temp_config_file, slots)

        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A',
                                machine_file=machine_file)

        assert manager.get_gpib_address() == 'GPIB0::5::INSTR'
        with open(machine_file) as f:
            assert json.load(f) == {'gpib_address': 'GPIB0::5::INSTR', 'visa_library': '@py'}
        with open(temp_config_file) as f:
            assert json.load(f)['machines'] == slots

    def test_a_default_value_is_not_worth_a_file(self, temp_config_file, machine_file):
        """Older versions wrote the default address into every host's slot."""
        default = DEFAULT_SETTINGS['measurement']['gpib_address']
        self._config_with_slots(temp_config_file, {'HOST-A': {'gpib_address': default}})

        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A',
                                machine_file=machine_file)

        assert manager.get_gpib_address() == default
        assert not os.path.exists(machine_file)

    def test_opening_a_config_writes_no_slot_for_this_host(self, temp_config_file, machine_file):
        ConfigManager(config_file=temp_config_file, hostname='HOST-A', machine_file=machine_file)
        ConfigManager(config_file=temp_config_file, hostname='HOST-B', machine_file=machine_file)

        with open(temp_config_file) as f:
            assert 'machines' not in json.load(f)

    def test_an_existing_machine_file_is_not_migrated_over(self, temp_config_file, machine_file):
        with open(machine_file, 'w') as f:
            json.dump({'gpib_address': 'GPIB0::3::INSTR'}, f)
        self._config_with_slots(temp_config_file, {'HOST-A': {'gpib_address': 'GPIB0::5::INSTR',
                                                              'visa_library': '@py'}})

        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A',
                                machine_file=machine_file)

        assert manager.get_gpib_address() == 'GPIB0::3::INSTR'
        # A key the machine file does not have still resolves from the old slot.
        assert manager.get_visa_library() == '@py'
        with open(machine_file) as f:
            assert json.load(f) == {'gpib_address': 'GPIB0::3::INSTR'}

    def test_the_default_location_is_under_the_home_directory(self, monkeypatch, tmp_path):
        from resistamet_gui import config as config_module
        monkeypatch.undo()  # the suite's redirection of the default
        monkeypatch.setattr(config_module.Path, 'home', lambda: tmp_path)
        assert config_module.default_machine_file() == str(
            tmp_path / '.resistamet' / 'machine.json')


class TestOutputResetMigration:
    """Stale Output overrides are dropped once, when they first take effect."""

    def _write(self, path, config):
        with open(path, 'w') as f:
            json.dump(config, f)

    def test_stale_user_override_dropped(self, temp_config_file):
        self._write(temp_config_file, {
            'users': ['alice'],
            'user_settings': {'alice': {
                'output': {'format': 'hdf5', 'compression': 'always'},
                'file': {'data_directory': 'measurement_data'},
            }},
        })
        manager = ConfigManager(config_file=temp_config_file)

        settings = manager.get_user_settings('alice')
        assert settings['output']['format'] == DEFAULT_SETTINGS['output']['format']
        assert settings['file']['data_directory'] == 'measurement_data'

    def test_runs_once(self, temp_config_file):
        self._write(temp_config_file, {
            'users': ['alice'],
            'user_settings': {'alice': {'output': {'format': 'hdf5'}}},
        })
        ConfigManager(config_file=temp_config_file)

        # A deliberate post-migration choice must survive the next open.
        second = ConfigManager(config_file=temp_config_file)
        second.update_user_settings('alice', {'output': {'format': 'hdf5'}})
        third = ConfigManager(config_file=temp_config_file)
        assert third.get_user_settings('alice')['output']['format'] == 'hdf5'

    def test_marker_recorded(self, temp_config_file):
        from resistamet_gui.constants import OUTPUT_RESET_MIGRATION
        ConfigManager(config_file=temp_config_file)
        with open(temp_config_file) as f:
            on_disk = json.load(f)
        assert on_disk['migrations'] == [OUTPUT_RESET_MIGRATION]


class TestConcurrentWrites:
    """Two writers must not be able to lose the file between them."""

    def test_save_is_atomic(self, temp_config_file):
        """A reader never sees a truncated file, only old or new."""
        manager = ConfigManager(config_file=temp_config_file)
        manager.add_user('alice')

        import threading

        errors = []

        def hammer(name):
            try:
                for _ in range(20):
                    manager.add_user(name)
                    manager.update_user_settings(name, {'measurement': {'nplc': 2.0}})
                    with open(temp_config_file) as handle:
                        json.load(handle)  # must always parse
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(f"user{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        reloaded = ConfigManager(config_file=temp_config_file)
        assert {'alice', 'user0', 'user1', 'user2', 'user3'} <= set(reloaded.config['users'])

    def test_no_temp_files_left_behind(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file)
        manager.add_user('alice')

        directory = Path(temp_config_file).parent
        assert list(directory.glob('.*.tmp')) == []

    def test_failed_write_keeps_the_old_file(self, temp_config_file, monkeypatch):
        manager = ConfigManager(config_file=temp_config_file)
        manager.add_user('alice')
        before = Path(temp_config_file).read_text()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(json, 'dump', boom)
        manager.add_user('bob')  # logged, not raised

        assert Path(temp_config_file).read_text() == before


class TestTwoManagers:
    """The GUI and a sidecar, or two lab PCs, each hold the file's contents.

    A save must only change what that manager changed; everything else in the
    file is whatever the other one last wrote.
    """

    @pytest.fixture
    def pair(self, temp_config_file):
        first = ConfigManager(config_file=temp_config_file)
        first.add_user('alice')
        first.update_user_settings('alice', {'measurement': {'nplc': 2.0}})
        return first, ConfigManager(config_file=temp_config_file)

    def test_both_new_users_are_kept(self, pair, temp_config_file):
        a, b = pair
        a.add_user('carol')
        b.add_user('dave')

        assert ConfigManager(config_file=temp_config_file).get_users() == \
            ['alice', 'carol', 'dave']
        assert b.get_users() == ['alice', 'carol', 'dave']

    def test_an_edit_does_not_undo_another_users_edit(self, pair, temp_config_file):
        a, b = pair
        a.add_user('bob')
        a.update_user_settings('bob', {'measurement': {'fpp_current': 5e-5}})

        b.update_user_settings('alice', {'measurement': {'nplc': 5.0}})

        stored = ConfigManager(config_file=temp_config_file).config['user_settings']
        assert stored['bob']['measurement'] == {'fpp_current': 5e-5}
        assert stored['alice']['measurement'] == {'nplc': 5.0}

    def test_the_same_key_goes_to_the_last_writer(self, pair, temp_config_file):
        a, b = pair
        a.merge_user_settings('alice', {'measurement': {'nplc': 3.0, 'sampling_rate': 4.0}})
        b.merge_user_settings('alice', {'measurement': {'nplc': 7.0}})

        stored = ConfigManager(config_file=temp_config_file).config['user_settings']['alice']
        assert stored['measurement'] == {'nplc': 7.0, 'sampling_rate': 4.0}

    def test_a_section_replaced_whole_stays_replaced(self, pair, temp_config_file):
        """update_user_settings drops the keys it is not given; a merge keeps that."""
        a, b = pair
        b.update_user_settings('alice', {'measurement': {'sampling_rate': 4.0}})

        stored = ConfigManager(config_file=temp_config_file).config['user_settings']['alice']
        assert stored['measurement'] == {'sampling_rate': 4.0}

    def test_last_user_and_unknown_keys_survive(self, pair, temp_config_file):
        a, b = pair
        a.set_last_user('alice')
        raw = json.loads(Path(temp_config_file).read_text())
        raw['written_by_a_newer_version'] = {'keep': True}
        Path(temp_config_file).write_text(json.dumps(raw))

        b.add_user('dave')

        saved = json.loads(Path(temp_config_file).read_text())
        assert saved['last_user'] == 'alice'
        assert saved['written_by_a_newer_version'] == {'keep': True}

    def test_a_file_that_went_missing_is_written_whole(self, pair, temp_config_file):
        a, b = pair
        os.unlink(temp_config_file)

        b.add_user('dave')

        assert ConfigManager(config_file=temp_config_file).get_users() == ['alice', 'dave']

    def test_a_file_that_became_unreadable_is_kept_aside(self, pair, temp_config_file):
        a, b = pair
        Path(temp_config_file).write_text('{"users": ["ali')

        b.add_user('dave')

        copies = list(Path(temp_config_file).parent.glob('test_config.json.corrupt-*'))
        assert [c.read_text() for c in copies] == ['{"users": ["ali']
        assert ConfigManager(config_file=temp_config_file).get_users() == ['alice', 'dave']

    def test_a_file_that_was_unreadable_at_start_and_is_whole_now(self, temp_config_file):
        """The sync finished after we opened: our change goes onto the real file."""
        whole = ConfigManager(config_file=temp_config_file)
        whole.add_user('alice')
        whole.update_user_settings('alice', {'measurement': {'nplc': 2.0}})
        good = Path(temp_config_file).read_bytes()
        Path(temp_config_file).write_bytes(good[:len(good) // 2])
        late = ConfigManager(config_file=temp_config_file)
        assert late.load_failed
        Path(temp_config_file).write_bytes(good)

        late.add_user('dave')

        reloaded = ConfigManager(config_file=temp_config_file)
        assert reloaded.get_users() == ['alice', 'dave']
        assert reloaded.config['user_settings']['alice']['measurement'] == {'nplc': 2.0}
        assert list(Path(temp_config_file).parent.glob('*.corrupt-*')) == []

    def test_saves_from_two_processes_do_not_lose_each_other(self, temp_config_file, tmp_path):
        import subprocess
        import sys
        ConfigManager(config_file=temp_config_file)
        # Every child loads the file, then all wait for 'go' before saving:
        # each holds a copy that knows nothing of the others' users.
        script = (
            "import os, sys, time\n"
            "from resistamet_gui.config import ConfigManager\n"
            "m = ConfigManager(config_file=sys.argv[1], machine_file=sys.argv[2])\n"
            "open(sys.argv[2] + '.ready-' + sys.argv[3], 'w').close()\n"
            "while not os.path.exists(sys.argv[2] + '.go'):\n"
            "    time.sleep(0.01)\n"
            "for i in range(15):\n"
            "    m.add_user(f'{sys.argv[3]}{i}')\n"
        )
        repo = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            filter(None, [repo, os.environ.get('PYTHONPATH')])))
        children = [subprocess.Popen(
            [sys.executable, '-c', script, temp_config_file, str(tmp_path / 'm.json'), name],
            cwd=str(tmp_path), env=env) for name in ('a', 'b', 'c')]
        deadline = time.time() + 30
        while time.time() < deadline and len(list(tmp_path.glob('m.json.ready-*'))) < 3:
            time.sleep(0.02)
        (tmp_path / 'm.json.go').touch()
        assert [child.wait(timeout=60) for child in children] == [0, 0, 0]

        users = ConfigManager(config_file=temp_config_file).get_users()
        assert len(users) == 45


class TestMachineLocalVisaLibrary:
    """The VISA backend is per-machine too: it describes this PC's driver stack."""

    def test_default_is_pyvisas_choice(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        assert manager.get_visa_library() == ''

    def test_set_writes_to_the_machine_file(self, temp_config_file, machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        manager.set_machine_local('visa_library', '@py')

        with open(machine_file) as f:
            assert json.load(f)['visa_library'] == '@py'
        reopened = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        assert reopened.get_visa_library() == '@py'

    def test_empty_means_back_to_automatic(self, temp_config_file):
        """Unlike an address, an empty backend is a real value: pyvisa decides."""
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.set_machine_local('visa_library', '@py')
        manager.set_machine_local('visa_library', '')
        assert manager.get_visa_library() == ''

    def test_empty_address_is_still_ignored(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.set_gpib_address('GPIB0::25::INSTR')
        manager.set_gpib_address('')
        assert manager.get_gpib_address() == 'GPIB0::25::INSTR'

    def test_profile_carries_the_machine_backend_not_the_users(self, temp_config_file,
                                                               machine_file, other_machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        manager.update_user_settings('alice', {'measurement': {'visa_library': '@py',
                                                                'sampling_rate': 50.0}})

        with open(temp_config_file) as f:
            saved = json.load(f)
        assert 'visa_library' not in saved['user_settings']['alice']['measurement']
        with open(machine_file) as f:
            assert json.load(f)['visa_library'] == '@py'
        assert manager.get_user_settings('alice')['measurement']['visa_library'] == '@py'

        other_pc = ConfigManager(config_file=temp_config_file, machine_file=other_machine_file)
        assert other_pc.get_user_settings('alice')['measurement']['visa_library'] == ''


class TestMachineLocalGpibInterface:
    """A Prologix adapter hangs off one PC's serial port or one lab's network."""

    NAME = 'PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC'

    def test_default_is_none(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        assert manager.get_gpib_interface() == ''

    def test_set_writes_to_the_machine_file(self, temp_config_file, machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        manager.set_machine_local('gpib_interface', self.NAME)

        with open(machine_file) as f:
            assert json.load(f)['gpib_interface'] == self.NAME
        reopened = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        assert reopened.get_gpib_interface() == self.NAME

    def test_empty_means_no_adapter_again(self, temp_config_file):
        manager = ConfigManager(config_file=temp_config_file, hostname='HOST-A')
        manager.set_machine_local('gpib_interface', self.NAME)
        manager.set_machine_local('gpib_interface', '')
        assert manager.get_gpib_interface() == ''

    def test_profile_carries_the_machine_interface_not_the_users(self, temp_config_file,
                                                                 machine_file, other_machine_file):
        manager = ConfigManager(config_file=temp_config_file, machine_file=machine_file)
        manager.update_user_settings('alice', {'measurement': {'gpib_interface': self.NAME,
                                                                'sampling_rate': 50.0}})

        with open(temp_config_file) as f:
            saved = json.load(f)
        assert 'gpib_interface' not in saved['user_settings']['alice']['measurement']
        with open(machine_file) as f:
            assert json.load(f)['gpib_interface'] == self.NAME
        assert manager.get_user_settings('alice')['measurement']['gpib_interface'] == self.NAME

        other_pc = ConfigManager(config_file=temp_config_file, machine_file=other_machine_file)
        assert other_pc.get_user_settings('alice')['measurement']['gpib_interface'] == ''
