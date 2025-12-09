"""
System Utilities Module

Cross-platform utilities for system-level operations like
preventing sleep during long measurements.
"""

import logging
import platform
import subprocess
import sys
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# Platform detection
PLATFORM = platform.system().lower()
IS_MACOS = PLATFORM == 'darwin'
IS_WINDOWS = PLATFORM == 'windows'
IS_LINUX = PLATFORM == 'linux'


class SleepInhibitor:
    """Prevents system sleep during long-running measurements.

    Usage:
        inhibitor = SleepInhibitor()
        inhibitor.inhibit("Running 4-point probe measurement")
        # ... measurement runs ...
        inhibitor.uninhibit()

    Or as context manager:
        with SleepInhibitor.context("Running measurement"):
            # ... measurement runs ...
    """

    def __init__(self):
        self._active = False
        self._process: Optional[subprocess.Popen] = None
        self._reason = ""

        # Windows-specific
        self._previous_state = None

    def inhibit(self, reason: str = "Measurement in progress") -> bool:
        """Prevent system from sleeping.

        Args:
            reason: Human-readable reason (shown in system UI on some platforms)

        Returns:
            True if successfully inhibited, False otherwise
        """
        if self._active:
            logger.debug("Sleep already inhibited")
            return True

        self._reason = reason
        success = False

        try:
            if IS_MACOS:
                success = self._inhibit_macos()
            elif IS_WINDOWS:
                success = self._inhibit_windows()
            elif IS_LINUX:
                success = self._inhibit_linux()
            else:
                logger.warning(f"Sleep inhibition not supported on {PLATFORM}")
                return False

            if success:
                self._active = True
                logger.info(f"System sleep inhibited: {reason}")
            return success

        except Exception as e:
            logger.warning(f"Failed to inhibit sleep: {e}")
            return False

    def uninhibit(self) -> None:
        """Allow system to sleep again."""
        if not self._active:
            return

        try:
            if IS_MACOS:
                self._uninhibit_macos()
            elif IS_WINDOWS:
                self._uninhibit_windows()
            elif IS_LINUX:
                self._uninhibit_linux()

            self._active = False
            logger.info("System sleep re-enabled")

        except Exception as e:
            logger.warning(f"Failed to uninhibit sleep: {e}")

    def _inhibit_macos(self) -> bool:
        """Use caffeinate to prevent sleep on macOS."""
        # -i: prevent idle sleep
        # -w: wait for process (we'll kill it on uninhibit)
        self._process = subprocess.Popen(
            ['caffeinate', '-i', '-w', str(subprocess.os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return self._process.poll() is None

    def _uninhibit_macos(self) -> None:
        """Kill caffeinate process."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def _inhibit_windows(self) -> bool:
        """Use SetThreadExecutionState on Windows."""
        import ctypes

        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002

        # Save previous state and set new state
        self._previous_state = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        return True

    def _uninhibit_windows(self) -> None:
        """Reset execution state on Windows."""
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self._previous_state = None

    def _inhibit_linux(self) -> bool:
        """Try systemd-inhibit on Linux."""
        # This is a best-effort approach - may not work on all distros
        try:
            self._process = subprocess.Popen(
                [
                    'systemd-inhibit',
                    '--what=idle:sleep',
                    f'--why={self._reason}',
                    '--mode=block',
                    'sleep', 'infinity'
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return self._process.poll() is None
        except FileNotFoundError:
            logger.debug("systemd-inhibit not available")
            return False

    def _uninhibit_linux(self) -> None:
        """Kill systemd-inhibit process."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    @property
    def is_active(self) -> bool:
        """Whether sleep is currently inhibited."""
        return self._active

    def __enter__(self):
        self.inhibit()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.uninhibit()
        return False

    @classmethod
    @contextmanager
    def context(cls, reason: str = "Measurement in progress"):
        """Context manager for sleep inhibition.

        Args:
            reason: Why sleep is being inhibited

        Example:
            with SleepInhibitor.context("Running 8-hour measurement"):
                run_measurement()
        """
        inhibitor = cls()
        try:
            inhibitor.inhibit(reason)
            yield inhibitor
        finally:
            inhibitor.uninhibit()


def get_platform_info() -> dict:
    """Get platform information for diagnostics."""
    return {
        'system': platform.system(),
        'release': platform.release(),
        'version': platform.version(),
        'machine': platform.machine(),
        'python_version': sys.version,
    }
