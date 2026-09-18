# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the measurement backend the desktop shell launches.

Builds ``resistamet-api`` (``.exe`` on Windows): ``python -m resistamet_gui.api``
frozen into a one-directory bundle. The Tauri shell looks for this binary
beside its own executable and falls back to a Python interpreter when it is
absent, so a source checkout and a packaged install use the same lookup.

Why one-dir rather than one-file: a one-file bundle unpacks itself to a temp
directory on every start, which is slow on the lab PCs' antivirus and makes the
sidecar's startup time depend on the scanner. One-dir starts in well under a
second and the whole folder ships inside the Tauri bundle.

What is deliberately excluded: PySide6 and shiboken6. The backend never imports
them — test_session_no_qt enforces it — and leaving them out keeps the sidecar
a tenth the size and out of any Qt-plugin path trouble on Windows.

    pyinstaller resistamet-api.spec --noconfirm --clean
    dist/resistamet-api/resistamet-api --port 0 --simulate --config /tmp/c.json
"""
import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH)

a = Analysis(
    # A launcher, not the module: PyInstaller runs its entry as a plain
    # script, where the package-relative imports in api/__main__.py would fail.
    [str(ROOT / "resistamet-api.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pyvisa backends are chosen by name at runtime; static analysis does
        # not see them.
        "pyvisa",
        "pyvisa_py",
        "pyvisa_py.highlevel",
        # serial aux sensors, imported inside the driver
        "serial",
        "serial.tools.list_ports",
        # uvicorn picks its loop and protocol implementations by string
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        # pydantic v2 keeps its core in a compiled module
        "pydantic",
        "pydantic_core",
        # the simulator and its sensors, imported on --simulate
        "resistamet_gui.simulator",
        "resistamet_gui._simulator",
        "resistamet_gui.sensors",
        # h5py is imported inside Hdf5Exporter
        "h5py",
        "h5py.defs",
        "h5py.utils",
        "h5py.h5ac",
        "h5py._proxy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "shiboken6",
        "PyQt5",
        "PyQt6",
        "matplotlib",
        "pyqtgraph",
        "tkinter",
        "IPython",
        "notebook",
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
    [],
    exclude_binaries=True,
    name="resistamet-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console process: the shell reads the handshake from stdout and the
    # logs from stderr, and closes stdin to ask it to exit.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="resistamet-api",
)
