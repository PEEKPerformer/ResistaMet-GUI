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

It is also where a Prologix-style adapter gets its interface opened.
pyvisa-py routes ``GPIB<n>::<addr>::INSTR`` to such an adapter only while
the adapter's own resource (``PRLGX-ASRL<n>::<device>::INTFC`` or
``PRLGX-TCPIP<n>::<host>::INTFC``) is open, so the machine-local
``gpib_interface`` setting names that resource and the manager holds it.
"""
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

import pyvisa

# The settings model's grammar for a Prologix interface name. One pattern,
# so what a profile may store and what this module will open cannot drift.
from .schema.settings_common import _PRLGX_INTFC

logger = logging.getLogger(__name__)

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

#: Set to ``1`` to keep the NI GPIB-USB user-space driver out of pyvisa-py
#: for this process. A test and diagnostic switch, not a setting: with it a
#: pyvisa-py manager cannot reach an attached NI adapter, so a test that
#: enumerates a real manager cannot pulse IFC on a bench that is in use, and
#: a bug report can say whether pyvisa-py alone behaves differently.
DISABLE_NI_USB_ENV = 'RESISTAMET_DISABLE_NI_USB'

#: Hooks run before a ResourceManager that may be pyvisa-py is handed out.
#: Used to register sessions pyvisa-py does not ship (see ``gpib_usb``).
_PY_EXTENSIONS: List[Callable[[], None]] = []


def register_py_extension(hook: Callable[[], None]) -> None:
    """Run ``hook`` before every ResourceManager that may be pyvisa-py.

    That is ``@py`` and also the automatic choice, which pyvisa resolves to
    pyvisa-py when no vendor library is installed (the usual Mac). Hooks
    must be idempotent and must not touch any bus. A hook that raises is
    logged as a warning and skipped; the manager is opened regardless.
    """
    if hook not in _PY_EXTENSIONS:
        _PY_EXTENSIONS.append(hook)


class GpibInterfaceError(RuntimeError):
    """The configured GPIB interface resource could not be opened."""


#: Where a manager keeps its open interface: ``(resource name, session)``.
#: On the manager because pyvisa hands the same manager back for the same
#: library, and because pyvisa only holds its sessions weakly — an interface
#: nobody referenced would be collected, and closed, behind the run's back.
_INTERFACE_ATTR = '_resistamet_gpib_interface'
_interface_lock = threading.Lock()


def resource_manager(visa_library: str = AUTO,
                     gpib_interface: Optional[str] = None) -> Any:
    """Open the ResourceManager for ``visa_library``.

    ``pyvisa.ResourceManager`` is looked up at call time so the simulator
    and the test fakes, which replace it, keep working.

    ``gpib_interface`` is the configured interface name. A non-empty one is
    opened on the manager before it is handed out and stays open until it
    is released, replaced or the manager closes; an empty one means none is
    configured, and closes the one the manager holds. See
    :func:`open_gpib_interface`. Raises :class:`GpibInterfaceError` when it
    cannot be opened. ``None``, the default, is a caller with no say in the
    matter: the manager's interface is left as it is.
    """
    library = (visa_library or AUTO).strip()
    if library in (AUTO, PY):
        for hook in _PY_EXTENSIONS:
            # An extension only adds a route. One that fails must not take
            # the others down with it, the vendor library least of all.
            try:
                hook()
            except Exception as exc:
                logger.warning("pyvisa-py extension %s skipped: %s: %s",
                               getattr(hook, '__name__', repr(hook)),
                               type(exc).__name__, exc)
    rm = pyvisa.ResourceManager(library) if library else pyvisa.ResourceManager()
    if gpib_interface is not None:
        open_gpib_interface(rm, gpib_interface)
    return rm


def open_gpib_interface(rm: Any, gpib_interface: str) -> bool:
    """Hold ``gpib_interface`` open on ``rm``. True when it is open afterwards.

    Once per manager: a second call with the same name finds the session
    already there. A different name replaces it, closing the old one first
    because two interfaces on one board number would fight over it. An empty
    name is "none configured" and closes the one that is held: pyvisa hands
    the same manager back for the life of the process, so an interface
    nobody closes keeps its serial port — exclusively, on Windows — until
    the process exits.

    False, with nothing opened, under a vendor library (NI-VISA has no
    PRLGX resource class; the name is ignored with a warning) and under the
    simulator. A failure to open raises :class:`GpibInterfaceError` naming
    the resource, so the operator reads "could not open PRLGX-ASRL…" rather
    than "instrument not found" a step later.

    Only a Prologix interface name is ever opened. The session opened here
    is held for the life of the manager, so any other resource — the aux
    sensor's serial port, an instrument — would be taken from whoever it
    belongs to. Such a name raises :class:`GpibInterfaceError` with nothing
    opened and the interface already held left as it was.
    """
    name = (gpib_interface or '').strip()
    if not name:
        release_gpib_interface(rm)
        return False
    if _simulating():
        logger.info("Simulating: GPIB interface %s is not opened", name)
        return False
    if _kind(rm) == 'ivi':
        logger.warning("GPIB interface %s ignored: a vendor VISA library has no such "
                       "resource. Choose the pyvisa-py backend to use it.", name)
        return False
    if not _PRLGX_INTFC.match(name):
        raise GpibInterfaceError(
            f"Could not open GPIB interface {name}: not a Prologix interface. "
            "Expected PRLGX-ASRL[board]::<serial device>::INTFC or "
            "PRLGX-TCPIP[board]::<host>[::port]::INTFC")
    with _interface_lock:
        held = getattr(rm, _INTERFACE_ATTR, None)
        if held is not None:
            held_name, session = held
            if held_name == name and _is_open(session):
                return True
            _close_quietly(session)
            setattr(rm, _INTERFACE_ATTR, None)
        try:
            session = rm.open_resource(name)
        except Exception as exc:
            raise GpibInterfaceError(
                f"Could not open GPIB interface {name}: {exc}") from exc
        setattr(rm, _INTERFACE_ATTR, (name, session))
        logger.info("GPIB interface %s opened", name)
        return True


def release_gpib_interface(rm: Any) -> Optional[str]:
    """Close the interface ``rm`` holds. Its name, or None when none was held.

    For a caller that opened one only to try it — a scan or an identify
    with a name that is not saved yet — and for giving the adapter's port
    back to another program without closing the manager. The next
    :func:`resource_manager` call that names an interface opens it again.
    Not for use while a run is on the bus: the run's instrument session
    is routed through this interface.
    """
    with _interface_lock:
        held = getattr(rm, _INTERFACE_ATTR, None)
        if held is None:
            return None
        name, session = held
        _close_quietly(session)
        setattr(rm, _INTERFACE_ATTR, None)
    logger.info("GPIB interface %s released", name)
    return name


def held_gpib_interface(rm: Any) -> Optional[str]:
    """The interface resource open on ``rm`` right now, or None."""
    held = getattr(rm, _INTERFACE_ATTR, None)
    if held is not None and _is_open(held[1]):
        return held[0]
    return None


def _simulating() -> bool:
    from . import simulator
    return simulator.is_simulating()


def _is_open(session: Any) -> bool:
    """Whether a pyvisa resource still has its session. A fake counts as open."""
    try:
        session.session
    except pyvisa.errors.InvalidSession:
        return False
    except AttributeError:
        pass
    return True


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception as exc:
        logger.debug("closing the GPIB interface failed: %s", exc)


def _kind(rm: Any) -> str:
    """``'py'``, ``'ivi'`` or ``'unknown'``, from the manager alone."""
    path = getattr(getattr(rm, 'visalib', None), 'library_path', None)
    if path == 'py':
        return 'py'
    if isinstance(path, str) and path:
        return 'ivi'
    return 'unknown'


def describe(rm: Any, requested: str = AUTO) -> Dict[str, Optional[str]]:
    """What ``rm`` actually is, for the UI and for bug reports.

    ``kind`` is ``'ivi'`` (a vendor library), ``'py'`` (pyvisa-py) or
    ``'unknown'`` (a fake, or a backend that does not say). Tolerates any
    object, so a fake ResourceManager describes itself as unknown rather
    than raising.
    """
    kind = _kind(rm)
    if kind == 'py':
        return {
            'requested': requested,
            'kind': 'py',
            'library': 'pyvisa-py',
            'version': _pyvisa_py_version(),
        }
    if kind == 'ivi':
        return {
            'requested': requested,
            'kind': 'ivi',
            'library': rm.visalib.library_path,
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


def report(visa_library: str = AUTO, probe_bus: bool = False,
           gpib_interface: str = '') -> Dict[str, Any]:
    """What VISA this machine has, for a diagnostic that runs without a GUI.

    Answers the question a frozen install fails on: is there a VISA
    implementation here at all, which one, and can the NI USB driver load
    libusb. Touches no instrument unless ``probe_bus`` is set — enumerating
    resources puts traffic on the bus and can disturb another process's run.

    ``gpib_interface`` is reported as configured. Opening it takes the
    adapter's serial port and makes it controller of the bus, so that too
    waits for ``probe_bus``; then ``opened`` says whether it worked, with
    the ``error`` when it did not.
    """
    interface = (gpib_interface or '').strip()
    info: Dict[str, Any] = {'requested': visa_library, 'ni_usb': _ni_usb_report(),
                            'gpib_interface': {'configured': interface}}
    try:
        rm = resource_manager(visa_library)
    except Exception as exc:
        info['ok'] = False
        info['error'] = f"{type(exc).__name__}: {exc}"
        return info
    info['ok'] = True
    info['backend'] = describe(rm, visa_library)
    if probe_bus:
        if interface:
            try:
                info['gpib_interface']['opened'] = open_gpib_interface(rm, interface)
            except GpibInterfaceError as exc:
                info['gpib_interface']['opened'] = False
                info['gpib_interface']['error'] = str(exc)
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
    """The NI GPIB-USB user-space driver; a no-op without pyusb and libusb.

    Also a no-op while :data:`DISABLE_NI_USB_ENV` is ``1``.
    """
    if os.environ.get(DISABLE_NI_USB_ENV, '') == '1':
        logger.info("NI GPIB-USB driver not installed: %s=1", DISABLE_NI_USB_ENV)
        return
    from . import gpib_usb
    gpib_usb.install()


register_py_extension(_install_ni_usb)
