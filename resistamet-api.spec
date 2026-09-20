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
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


def _version_metadata():
    """Distribution metadata for the packages that report their own version.

    pyvisa and pyvisa-py read their version through importlib.metadata; with
    no dist-info in the bundle they answer "unknown", which makes
    ``--check-visa`` output useless in a bug report from the lab.
    """
    found = []
    for name in ("pyvisa", "pyvisa-py", "pyusb"):
        try:
            found += copy_metadata(name)
        except Exception:
            pass  # not installed on this build machine; the version stays unknown
    return found

block_cipher = None

ROOT = Path(SPECPATH)


#: A libusb shared library by its file name in the bundle, whoever put it
#: there: libusb-1.0.0.dylib, libusb-1.0.so.0, libusb-1.0.dll, libusb0.dll,
#: cygusb-1.0-0.dll. Not libusbmuxd.
_LIBUSB_BINARY = re.compile(
    r"^(?:lib|cyg)?usb(?:-?[0-9.]+)*\.(?:dylib|dll|so(?:\.[0-9]+)*)$", re.IGNORECASE)
_LIBUSB_LICENCE = "libusb-COPYING"


def _libusb_binaries():
    """The libusb shared library this spec bundles itself: macOS only.

    pyusb loads the library at runtime by name; a frozen app has no Homebrew
    path to find it on, so it ships beside the executable and
    ``gpib_usb.transport.libusb_backend`` looks there first.

    What ends up in the bundle, per platform:

    * macOS: the Homebrew library found here, under its real file name
      (``libusb-1.0.0.dylib``). pyinstaller-hooks-contrib's ``hook-usb``
      adds the library pyusb loaded on the build machine as well, the same
      file under the name it was loaded by (``libusb-1.0.dylib``).
    * Linux: nothing from here. ``find_library`` answers with a soname
      (``libusb-1.0.so.0``), not a path, so there is no file to name.
      ``hook-usb`` resolves the soname and bundles the system library.
    * Windows: nothing from here; the NI adapter belongs to NI's own driver
      and the app goes through NI-VISA. ``hook-usb`` bundles a libusb DLL
      only if the build machine has one, which the CI runner does not.

    Whichever of these put a libusb in the bundle, its licence goes with it
    or the build fails: see :func:`_require_libusb_licence`.

    Returns ``binaries`` in PyInstaller's tuple form.
    """
    if sys.platform != "darwin":
        return []
    import ctypes.util

    found = ctypes.util.find_library("usb-1.0")
    candidates = [found] if found else []
    candidates += [
        "/opt/homebrew/lib/libusb-1.0.0.dylib",
        "/usr/local/lib/libusb-1.0.0.dylib",
    ]
    for candidate in candidates:
        path = Path(candidate).resolve()
        if path.is_file():
            return [(str(path), ".")]
    return []


def _libusb_licence_source(library):
    """The licence text that came with ``library`` on this build machine, or None."""
    for licence in (Path(library).resolve().parent.parent / "COPYING",  # a Homebrew keg
                    Path("/opt/homebrew/opt/libusb/COPYING"),
                    Path("/usr/local/opt/libusb/COPYING"),
                    Path("/usr/share/doc/libusb-1.0-0/copyright")):
        if licence.is_file():
            return licence
    return None


def _require_libusb_licence(analysis):
    """Ship ``libusb-COPYING`` with any bundled libusb, or stop the build.

    libusb is LGPL-2.1. Loading the unmodified shared library dynamically
    from an MIT program is permitted; the licence asks that its text
    accompany the library. Checked on what the analysis collected, not on
    what this spec asked for, because ``hook-usb`` bundles a libusb of its
    own accord on every platform. A bundle with the library and without
    the text is not one to publish, so that is an error, not a warning.
    """
    bundled = [(dest, source) for dest, source, _kind in analysis.binaries
               if _LIBUSB_BINARY.match(Path(dest).name)]
    if not bundled:
        return
    licence = _libusb_licence_source(bundled[0][1])
    if licence is None:
        raise SystemExit(
            "resistamet-api.spec: %s would be bundled without the libusb licence. libusb is "
            "LGPL-2.1 and its COPYING must ship beside it as %s; none was found on this "
            "build machine (looked beside the library's Homebrew keg and in "
            "/usr/share/doc/libusb-1.0-0)."
            % (", ".join(sorted(dest for dest, _ in bundled)), _LIBUSB_LICENCE))
    # datas keeps the source file's name; stage a copy under the name it
    # should have in the bundle.
    staged = Path(tempfile.mkdtemp(prefix="resistamet-libusb-")) / _LIBUSB_LICENCE
    shutil.copyfile(licence, staged)
    analysis.datas.append((_LIBUSB_LICENCE, str(staged), "DATA"))


a = Analysis(
    # A launcher, not the module: PyInstaller runs its entry as a plain
    # script, where the package-relative imports in api/__main__.py would fail.
    [str(ROOT / "resistamet-api.py")],
    pathex=[str(ROOT)],
    binaries=_libusb_binaries(),
    datas=_version_metadata(),
    hiddenimports=[
        # pyvisa backends are chosen by name at runtime; static analysis does
        # not see them.
        "pyvisa",
        "pyvisa_py",
        "pyvisa_py.highlevel",
        # the NI GPIB-USB user-space driver imports pyusb lazily
        "usb",
        "usb.core",
        "usb.util",
        "usb.backend.libusb1",
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
        # uvicorn's auto selection has moved between these two across
        # releases; ship both so the pick at runtime always resolves.
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.websockets_sansio_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
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
        # The WebSocket implementation uvicorn loads by name at runtime.
        # Without it every upgrade is answered 404 and the desktop UI sits
        # on "Reconnecting"; the whole package, so the impl uvicorn picks
        # always has what it imports.
        *collect_submodules("websockets"),
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

_require_libusb_licence(a)

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
