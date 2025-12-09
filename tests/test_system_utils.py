"""
Unit tests for system utilities.

Tests cover:
- Sleep inhibitor functionality
- Platform detection
"""

import pytest
from unittest.mock import patch, MagicMock

from resistamet_gui.system_utils import (
    SleepInhibitor,
    get_platform_info,
    IS_MACOS,
    IS_WINDOWS,
    IS_LINUX,
)


class TestSleepInhibitor:
    """Tests for SleepInhibitor class."""

    def test_initial_state(self):
        """Test inhibitor starts inactive."""
        inhibitor = SleepInhibitor()
        assert not inhibitor.is_active

    def test_inhibit_sets_active(self):
        """Test that inhibit() sets active state."""
        inhibitor = SleepInhibitor()

        # Mock the platform-specific method to avoid actual system calls
        with patch.object(inhibitor, '_inhibit_macos', return_value=True), \
             patch.object(inhibitor, '_inhibit_windows', return_value=True), \
             patch.object(inhibitor, '_inhibit_linux', return_value=True):

            result = inhibitor.inhibit("Test reason")

            # Should be active if platform is supported
            if IS_MACOS or IS_WINDOWS or IS_LINUX:
                assert result is True
                assert inhibitor.is_active

    def test_uninhibit_clears_active(self):
        """Test that uninhibit() clears active state."""
        inhibitor = SleepInhibitor()
        inhibitor._active = True  # Simulate active state

        with patch.object(inhibitor, '_uninhibit_macos'), \
             patch.object(inhibitor, '_uninhibit_windows'), \
             patch.object(inhibitor, '_uninhibit_linux'):

            inhibitor.uninhibit()
            assert not inhibitor.is_active

    def test_double_inhibit_returns_true(self):
        """Test that calling inhibit() twice returns True."""
        inhibitor = SleepInhibitor()
        inhibitor._active = True  # Simulate already active

        result = inhibitor.inhibit("Test")
        assert result is True

    def test_uninhibit_when_not_active_is_noop(self):
        """Test that uninhibit() when not active does nothing."""
        inhibitor = SleepInhibitor()
        assert not inhibitor.is_active

        # Should not raise
        inhibitor.uninhibit()
        assert not inhibitor.is_active

    def test_context_manager(self):
        """Test using inhibitor as context manager."""
        with patch.object(SleepInhibitor, 'inhibit') as mock_inhibit, \
             patch.object(SleepInhibitor, 'uninhibit') as mock_uninhibit:

            with SleepInhibitor() as inhibitor:
                mock_inhibit.assert_called_once()

            mock_uninhibit.assert_called_once()

    def test_context_class_method(self):
        """Test using SleepInhibitor.context() class method."""
        with patch.object(SleepInhibitor, 'inhibit') as mock_inhibit, \
             patch.object(SleepInhibitor, 'uninhibit') as mock_uninhibit:

            with SleepInhibitor.context("Test measurement"):
                mock_inhibit.assert_called_once_with("Test measurement")

            mock_uninhibit.assert_called_once()


class TestPlatformDetection:
    """Tests for platform detection."""

    def test_platform_flags_mutually_exclusive(self):
        """Test that only one platform flag is True."""
        active_flags = sum([IS_MACOS, IS_WINDOWS, IS_LINUX])
        # At most one should be True (could be 0 on unknown platform)
        assert active_flags <= 1

    def test_get_platform_info_returns_dict(self):
        """Test that get_platform_info returns expected structure."""
        info = get_platform_info()

        assert isinstance(info, dict)
        assert 'system' in info
        assert 'release' in info
        assert 'version' in info
        assert 'machine' in info
        assert 'python_version' in info


class TestMacOSInhibitor:
    """Tests specific to macOS implementation."""

    @pytest.mark.skipif(not IS_MACOS, reason="macOS only")
    def test_caffeinate_started(self):
        """Test that caffeinate process is started on macOS."""
        inhibitor = SleepInhibitor()

        with patch('subprocess.Popen') as mock_popen:
            mock_process = MagicMock()
            mock_process.poll.return_value = None  # Process running
            mock_popen.return_value = mock_process

            result = inhibitor._inhibit_macos()

            assert result is True
            mock_popen.assert_called_once()
            # Verify caffeinate is called
            call_args = mock_popen.call_args[0][0]
            assert 'caffeinate' in call_args


class TestWindowsInhibitor:
    """Tests specific to Windows implementation."""

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    def test_execution_state_set(self):
        """Test that SetThreadExecutionState is called on Windows."""
        inhibitor = SleepInhibitor()

        with patch('ctypes.windll.kernel32.SetThreadExecutionState') as mock_set:
            mock_set.return_value = 1

            result = inhibitor._inhibit_windows()

            assert result is True
            mock_set.assert_called_once()
