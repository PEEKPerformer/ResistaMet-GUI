"""
Logging Configuration for ResistaMet-GUI

This module provides centralized logging configuration for the application.
All modules should use:

    from resistamet_gui.logging_config import get_logger
    logger = get_logger(__name__)

Log files are stored in the user's home directory under .resistamet/logs/
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


# Default configuration
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
DEFAULT_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3


def get_log_directory() -> Path:
    """Get the directory for log files.

    Creates the directory if it doesn't exist.

    Returns:
        Path to the log directory (~/.resistamet/logs/)
    """
    log_dir = Path.home() / '.resistamet' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def setup_logging(
    level: int = DEFAULT_LOG_LEVEL,
    log_to_file: bool = True,
    log_to_console: bool = True,
    log_file: Optional[str] = None
) -> None:
    """Configure the root logger for the application.

    This should be called once at application startup.

    Args:
        level: Logging level (default: INFO)
        log_to_file: Whether to write logs to file (default: True)
        log_to_console: Whether to output logs to console (default: True)
        log_file: Custom log file path (default: auto-generated in ~/.resistamet/logs/)
    """
    root_logger = logging.getLogger('resistamet_gui')
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    formatter = logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT)

    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if log_to_file:
        if log_file is None:
            log_dir = get_log_directory()
            timestamp = datetime.now().strftime('%Y%m%d')
            log_file = log_dir / f'resistamet_{timestamp}.log'

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=MAX_LOG_SIZE,
            backupCount=BACKUP_COUNT
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    root_logger.info(f"Logging initialized. Level: {logging.getLevelName(level)}")


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module.

    Args:
        name: Module name (typically __name__)

    Returns:
        Logger instance configured as child of resistamet_gui logger
    """
    if not name.startswith('resistamet_gui'):
        name = f'resistamet_gui.{name}'
    return logging.getLogger(name)


# Convenience function to adjust log level at runtime
def set_log_level(level: int) -> None:
    """Change the log level at runtime.

    Args:
        level: New logging level (e.g., logging.DEBUG)
    """
    logger = logging.getLogger('resistamet_gui')
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)
