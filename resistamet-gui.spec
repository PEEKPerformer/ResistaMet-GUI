# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for ResistaMet GUI.
#
# Build with:  pyinstaller resistamet-gui.spec
#
# Produces a single-file ResistaMet.exe under dist/ on Windows. The
# spec is Windows-targeted because NI-VISA — the driver layer Keithley
# 2400/2450 instruments speak through — is not available on macOS.
# A Mac build will technically run (PyInstaller doesn't care), but the
# instrument layer won't find a VISA library at runtime unless paired
# with a Prologix-style GPIB-USB adapter via pyvisa-py, which has not
# been tested against ResistaMet. The .exe assumes NI-VISA is already
# installed on the target Windows machine; it does not bundle the
# NI runtime itself.

block_cipher = None


a = Analysis(
    ['resistamet-gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # PyInstaller can miss imports done lazily inside resistamet_gui
    # (matplotlib backends, pyvisa-py transports, pyqtgraph helper modules).
    # Listing them here guarantees they end up in the frozen bundle.
    hiddenimports=[
        'pyqtgraph',
        'pyqtgraph.exporters',
        'pyvisa',
        'pyvisa_py',
        # Qt-agnostic matplotlib backend; dispatches via QT_API env var
        # (set in resistamet_gui/__main__.py before any mpl import).
        'matplotlib.backends.backend_qtagg',
        'PySide6',
        'shiboken6',
        'resistamet_gui.simulator',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim obvious bloat the GUI never imports.
        'tkinter',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'IPython',
        'jupyter',
        'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ResistaMet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowed GUI — no console window flashing on Windows launch.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
