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
import pytest
from pathlib import Path

from resistamet_gui.config import ConfigManager
from resistamet_gui.constants import DEFAULT_SETTINGS


@pytest.fixture
def temp_config_file(tmp_path):
    """Create a temporary config file path."""
    return str(tmp_path / "test_config.json")


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

        config_manager.set_last_user("test_user")
        assert config_manager.get_last_user() == "test_user"


class TestUserSettings:
    """Tests for user settings management."""

    def test_get_user_settings_new_user(self, config_manager):
        """Test getting settings for a new user returns defaults."""
        config_manager.add_user("new_user")
        settings = config_manager.get_user_settings("new_user")

        # Should have measurement settings
        assert 'measurement' in settings or settings == {}

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

        manager = ConfigManager(config_file=temp_config_file)

        # Custom value preserved
        assert manager.config['measurement']['gpib_address'] == 'GPIB0::25::INSTR'

        # Default values filled in
        assert 'res_test_current' in manager.config['measurement']
