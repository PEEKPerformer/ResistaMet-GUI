"""Which VISA implementation opens the bus.

pyvisa can sit on two very different things: a vendor library (NI-VISA,
Keysight IO Libraries, R&S VISA — the ``@ivi`` backend) or its own pure
Python implementation (pyvisa-py, ``@py``). Left to itself pyvisa takes the
vendor library when one is installed and falls back to pyvisa-py otherwise.
That is right on the lab's Windows PCs. It is wrong on a Mac with NI-VISA
installed but no GPIB driver behind it, where the instrument is reachable
only through pyvisa-py (a Prologix adapter, a serial port, or the NI USB
adapter driven from user space).

The choice is machine-local, like the instrument address, and lives in the
``visa_library`` measurement setting. The empty string means "pyvisa's
default"; anything else is handed to :class:`pyvisa.ResourceManager` as-is.

Every ResourceManager the application opens comes through here, so this is
the one place to register extra pyvisa-py sessions before a bus is touched.
"""
from typing import Any, Callable, Dict, List, Optional

import pyvisa

#: Use pyvisa's own default: the vendor library if installed, else pyvisa-py.
AUTO = ''
#: Vendor VISA (NI-VISA and friends).
IVI = '@ivi'
#: pyvisa-py, pure Python.
PY = '@py'

#: Choices the UI offers. A file path to a VISA library is also accepted.
CHOICES: Dict[str, str] = {
    AUTO: 'Automatic',
    IVI: 'Vendor VISA (NI-VISA)',
    PY: 'pyvisa-py',
}

#: Hooks run before a ResourceManager that may be pyvisa-py is handed out.
#: Used to register sessions pyvisa-py does not ship (see ``gpib_usb``).
_PY_EXTENSIONS: List[Callable[[], None]] = []


def register_py_extension(hook: Callable[[], None]) -> None:
    """Run ``hook`` before every ResourceManager that may be pyvisa-py.

    That is ``@py`` and also the automatic choice, which pyvisa resolves to
    pyvisa-py when no vendor library is installed (the usual Mac). Hooks
    must be idempotent and must not touch any bus.
    """
    if hook not in _PY_EXTENSIONS:
        _PY_EXTENSIONS.append(hook)


def resource_manager(visa_library: str = AUTO) -> Any:
    """Open the ResourceManager for ``visa_library``.

    ``pyvisa.ResourceManager`` is looked up at call time so the simulator
    and the test fakes, which replace it, keep working.
    """
    library = (visa_library or AUTO).strip()
    if library in (AUTO, PY):
        for hook in _PY_EXTENSIONS:
            hook()
    if library:
        return pyvisa.ResourceManager(library)
    return pyvisa.ResourceManager()


def describe(rm: Any, requested: str = AUTO) -> Dict[str, Optional[str]]:
    """What ``rm`` actually is, for the UI and for bug reports.

    ``kind`` is ``'ivi'`` (a vendor library), ``'py'`` (pyvisa-py) or
    ``'unknown'`` (a fake, or a backend that does not say). Tolerates any
    object, so a fake ResourceManager describes itself as unknown rather
    than raising.
    """
    visalib = getattr(rm, 'visalib', None)
    path = getattr(visalib, 'library_path', None)
    if path == 'py':
        return {
            'requested': requested,
            'kind': 'py',
            'library': 'pyvisa-py',
            'version': _pyvisa_py_version(),
        }
    if isinstance(path, str) and path:
        return {
            'requested': requested,
            'kind': 'ivi',
            'library': path,
            'version': _ivi_version(rm),
        }
    return {'requested': requested, 'kind': 'unknown', 'library': None, 'version': None}


def _pyvisa_py_version() -> Optional[str]:
    try:
        import pyvisa_py
        return getattr(pyvisa_py, '__version__', None)
    except Exception:
        return None


def _ivi_version(rm: Any) -> Optional[str]:
    """The vendor library's implementation version, e.g. ``'23.5.0'``."""
    try:
        raw = rm.visalib.get_attribute(
            rm.session, pyvisa.constants.ResourceAttribute.resource_impl_version)[0]
        return f"{(raw >> 20) & 0xFFF}.{(raw >> 8) & 0xFFF}.{raw & 0xFF}"
    except Exception:
        return None


def report(visa_library: str = AUTO, probe_bus: bool = False) -> Dict[str, Any]:
    """What VISA this machine has, for a diagnostic that runs without a GUI.

    Answers the question a frozen install fails on: is there a VISA
    implementation here at all, which one, and can the NI USB driver load
    libusb. Touches no instrument unless ``probe_bus`` is set — enumerating
    resources puts traffic on the bus and can disturb another process's run.
    """
    info: Dict[str, Any] = {'requested': visa_library, 'ni_usb': _ni_usb_report()}
    try:
        rm = resource_manager(visa_library)
    except Exception as exc:
        info['ok'] = False
        info['error'] = f"{type(exc).__name__}: {exc}"
        return info
    info['ok'] = True
    info['backend'] = describe(rm, visa_library)
    if probe_bus:
        try:
            info['resources'] = list(rm.list_resources())
        except Exception as exc:
            info['resources_error'] = f"{type(exc).__name__}: {exc}"
    return info


def _ni_usb_report() -> Dict[str, Any]:
    """The NI GPIB-USB driver's own view of itself. Never raises."""
    try:
        from . import gpib_usb
        from .gpib_usb import transport
        adapters = [
            {'model': a.model, 'serial': a.serial, 'bus': a.bus,
             'address': a.address, 'needs_firmware': a.needs_firmware}
            for a in gpib_usb.find_adapters()
        ]
        return {'available': gpib_usb.available(),
                'libusb': transport.libusb_library_path(),
                'adapters': adapters}
    except Exception as exc:
        return {'available': False, 'error': f"{type(exc).__name__}: {exc}",
                'adapters': []}


def _install_ni_usb() -> None:
    """The NI GPIB-USB user-space driver; a no-op without pyusb and libusb."""
    from . import gpib_usb
    gpib_usb.install()


register_py_extension(_install_ni_usb)
