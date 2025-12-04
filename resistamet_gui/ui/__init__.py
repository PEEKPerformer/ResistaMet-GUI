# ResistaMet-GUI UI Package
"""
User interface components for ResistaMet-GUI.

Modules:
    main_window: Main application window (ResistanceMeterApp)
    dialogs: Settings and user selection dialogs
    canvas: Matplotlib integration for real-time plotting
"""

from .main_window import ResistanceMeterApp
from .dialogs import SettingsDialog, UserSelectionDialog
from .canvas import MplCanvas

__all__ = ['ResistanceMeterApp', 'SettingsDialog', 'UserSelectionDialog', 'MplCanvas']
