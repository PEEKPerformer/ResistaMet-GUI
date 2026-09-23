"""Choosing the VISA implementation that opens the bus."""
import logging
import sys

import pyvisa
import pytest

from resistamet_gui import visa_backend
from resistamet_gui.instrument import Keithley2400
from tests.fakes.fake_prologix import IDN, FakePrologix


@pytest.fixture
def recording_rm(monkeypatch):
    """Replace pyvisa.ResourceManager with a factory that records its args."""
    calls = []

    class _RM:
        def list_resources(self):
            return ()

    def _factory(*args, **kwargs):
        calls.append(args)
        return _RM()

    monkeypatch.setattr(pyvisa, 'ResourceManager', _factory)
    return calls


class TestResourceManager:
    def test_auto_leaves_the_choice_to_pyvisa(self, recording_rm):
        visa_backend.resource_manager('')
        assert recording_rm == [()]

    def test_whitespace_counts_as_auto(self, recording_rm):
        visa_backend.resource_manager('  ')
        assert recording_rm == [()]

    def test_an_explicit_library_is_passed_through(self, recording_rm):
        visa_backend.resource_manager('@py')
        visa_backend.resource_manager('/opt/visa/libvisa.so')
        assert recording_rm == [('@py',), ('/opt/visa/libvisa.so',)]

    def test_py_extensions_run_when_pyvisa_py_may_answer(self, recording_rm, monkeypatch):
        ran = []
        monkeypatch.setattr(visa_backend, '_PY_EXTENSIONS', [])
        visa_backend.register_py_extension(lambda: ran.append('hook'))
        visa_backend.register_py_extension(lambda: ran.append('hook'))

        visa_backend.resource_manager('@ivi')
        assert ran == []  # a vendor library never consults pyvisa-py

        visa_backend.resource_manager('@py')
        assert ran == ['hook', 'hook']

        visa_backend.resource_manager('')  # automatic may resolve to pyvisa-py
        assert ran == ['hook'] * 4

    def test_the_ni_usb_driver_is_a_registered_extension(self):
        assert visa_backend._install_ni_usb in visa_backend._PY_EXTENSIONS

    def test_registering_the_same_hook_twice_runs_it_once(self, recording_rm, monkeypatch):
        ran = []
        monkeypatch.setattr(visa_backend, '_PY_EXTENSIONS', [])

        def hook():
            ran.append(1)

        visa_backend.register_py_extension(hook)
        visa_backend.register_py_extension(hook)
        visa_backend.resource_manager('@py')
        assert ran == [1]


class _Lib:
    def __init__(self, path):
        self.library_path = path


class _RM:
    def __init__(self, path):
        self.visalib = _Lib(path)
        self.session = 1


class TestDescribe:
    def test_pyvisa_py(self):
        info = visa_backend.describe(_RM('py'), '@py')
        assert info['kind'] == 'py'
        assert info['library'] == 'pyvisa-py'
        assert info['requested'] == '@py'

    def test_vendor_library(self):
        info = visa_backend.describe(_RM('/Library/Frameworks/visa.framework/visa'), '')
        assert info['kind'] == 'ivi'
        assert info['library'].endswith('visa')
        assert info['version'] is None  # the stand-in has no get_attribute

    def test_a_fake_is_unknown_rather_than_an_error(self):
        info = visa_backend.describe(object(), '')
        assert info == {'requested': '', 'kind': 'unknown', 'library': None, 'version': None}


PRLGX = 'PRLGX-ASRL::/dev/cu.usbserial-PX12345::INTFC'


class _Session:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


class _InterfaceRM:
    """Records what it is asked to open, in order."""

    def __init__(self, library_path=None, error=None):
        self.opened = []
        self.sessions = []
        self._error = error
        if library_path is not None:
            self.visalib = _Lib(library_path)

    def list_resources(self):
        return ()

    def open_resource(self, name, **_kwargs):
        if self._error is not None:
            raise self._error
        self.opened.append(name)
        self.sessions.append(_Session(name))
        return self.sessions[-1]


@pytest.fixture
def one_rm(monkeypatch):
    """pyvisa hands the same manager back for the same library; so does this."""
    def install(rm):
        monkeypatch.setattr(pyvisa, 'ResourceManager', lambda *a, **k: rm)
        return rm
    return install


class TestGpibInterface:
    def test_nothing_is_opened_without_one(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py')
        visa_backend.resource_manager('@py', '   ')
        assert rm.opened == []

    def test_it_is_opened_once_and_before_any_instrument(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        again = visa_backend.resource_manager('@py', PRLGX)
        again.open_resource('GPIB0::24::INSTR')
        assert again is rm
        assert rm.opened == [PRLGX, 'GPIB0::24::INSTR']

    def test_the_manager_keeps_the_session(self, one_rm):
        """pyvisa holds sessions weakly; an unreferenced interface would close."""
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        assert getattr(rm, visa_backend._INTERFACE_ATTR) == (PRLGX, rm.sessions[0])
        assert rm.sessions[0].closed is False

    def test_the_manager_says_which_interface_it_holds(self, one_rm):
        rm = one_rm(_InterfaceRM())
        assert visa_backend.held_gpib_interface(rm) is None
        visa_backend.resource_manager('@py', PRLGX)
        assert visa_backend.held_gpib_interface(rm) == PRLGX

    def test_a_caller_without_one_leaves_it_open(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        visa_backend.resource_manager('@py')
        assert rm.sessions[0].closed is False

    @pytest.mark.parametrize('cleared', ['', '   '])
    def test_a_configured_name_that_became_empty_closes_it(self, one_rm, cleared):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        visa_backend.resource_manager('@py', cleared)
        assert rm.sessions[0].closed is True
        assert visa_backend.held_gpib_interface(rm) is None
        assert getattr(rm, visa_backend._INTERFACE_ATTR) is None

    def test_release_closes_it_and_says_what_it_closed(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        assert visa_backend.release_gpib_interface(rm) == PRLGX
        assert rm.sessions[0].closed is True
        assert visa_backend.held_gpib_interface(rm) is None

    def test_release_with_nothing_held_is_harmless(self, one_rm):
        rm = one_rm(_InterfaceRM())
        assert visa_backend.release_gpib_interface(rm) is None
        assert visa_backend.release_gpib_interface(object()) is None
        visa_backend.resource_manager('@py', PRLGX)
        visa_backend.release_gpib_interface(rm)
        assert visa_backend.release_gpib_interface(rm) is None

    def test_a_released_interface_is_opened_again_by_the_next_caller(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        visa_backend.release_gpib_interface(rm)
        visa_backend.resource_manager('@py', PRLGX)
        assert rm.opened == [PRLGX, PRLGX]
        assert rm.sessions[1].closed is False

    def test_another_name_replaces_it_closing_the_old_one_first(self, one_rm):
        rm = one_rm(_InterfaceRM())
        other = 'PRLGX-TCPIP::192.168.1.50::1234::INTFC'
        visa_backend.resource_manager('@py', PRLGX)
        order = []
        rm.sessions[0].close = lambda: order.append('closed')
        opening = rm.open_resource
        rm.open_resource = lambda name, **k: (order.append('opened'), opening(name, **k))[1]
        visa_backend.resource_manager('@py', other)
        assert order == ['closed', 'opened']
        assert rm.opened == [PRLGX, other]

    def test_a_vendor_library_ignores_it_with_a_warning(self, one_rm, caplog):
        rm = one_rm(_InterfaceRM('/Library/Frameworks/VISA.framework/VISA'))
        with caplog.at_level(logging.WARNING, logger=visa_backend.__name__):
            visa_backend.resource_manager('', PRLGX)
        assert rm.opened == []
        assert PRLGX in caplog.text
        assert 'ignored' in caplog.text

    def test_a_failure_names_the_interface_and_the_cause(self, one_rm):
        one_rm(_InterfaceRM(error=OSError('[Errno 2] could not open port')))
        with pytest.raises(visa_backend.GpibInterfaceError) as raised:
            visa_backend.resource_manager('@py', PRLGX)
        assert PRLGX in str(raised.value)
        assert 'could not open port' in str(raised.value)
        assert isinstance(raised.value.__cause__, OSError)

    def test_a_failure_is_retried_by_the_next_caller(self, one_rm):
        rm = one_rm(_InterfaceRM(error=OSError('unplugged')))
        with pytest.raises(visa_backend.GpibInterfaceError):
            visa_backend.resource_manager('@py', PRLGX)
        rm._error = None
        visa_backend.resource_manager('@py', PRLGX)
        assert rm.opened == [PRLGX]

    @pytest.mark.parametrize('name', [
        'ASRL/dev/cu.usbmodem1101::INSTR',     # the aux sensor's serial port
        'GPIB0::24::INSTR',
        'TCPIP::192.168.1.50::1234::SOCKET',
        'PRLGX-ASRL::INTFC',
        'PRLGX-ASRL::5::INSTR',
    ])
    def test_only_a_prologix_interface_name_is_opened(self, one_rm, name):
        rm = one_rm(_InterfaceRM())
        with pytest.raises(visa_backend.GpibInterfaceError) as raised:
            visa_backend.resource_manager('@py', name)
        assert rm.opened == []
        assert name in str(raised.value)
        assert 'PRLGX-ASRL[board]' in str(raised.value)

    def test_a_refused_name_leaves_the_open_interface_alone(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        with pytest.raises(visa_backend.GpibInterfaceError):
            visa_backend.resource_manager('@py', 'ASRL3::INSTR')
        assert rm.sessions[0].closed is False
        assert visa_backend.held_gpib_interface(rm) == PRLGX

    def test_the_settings_model_and_the_backend_share_one_grammar(self):
        from resistamet_gui.schema import settings_common
        assert visa_backend._PRLGX_INTFC is settings_common._PRLGX_INTFC

    def test_the_simulator_has_no_adapter_to_open(self):
        from resistamet_gui import simulator
        simulator.enable_simulation()
        try:
            rm = visa_backend.resource_manager('', PRLGX)
            assert 'GPIB0::24::INSTR' in rm.list_resources()
        finally:
            simulator.disable_simulation()


class TestReportGpibInterface:
    """``--check-visa``: say what is configured; open it only when asked to touch the bus."""

    def test_none_configured(self, one_rm):
        one_rm(_InterfaceRM())
        assert visa_backend.report('@py')['gpib_interface'] == {'configured': ''}

    def test_quiet_names_it_without_opening_it(self, one_rm):
        rm = one_rm(_InterfaceRM())
        info = visa_backend.report('@py', gpib_interface=PRLGX)
        assert info['gpib_interface'] == {'configured': PRLGX}
        assert rm.opened == []

    def test_bus_opens_it_before_enumerating(self, one_rm):
        rm = one_rm(_InterfaceRM())
        order = []
        rm.list_resources = lambda: (order.append('listed'), ())[1]
        opening = rm.open_resource
        rm.open_resource = lambda name, **k: (order.append('opened'), opening(name, **k))[1]
        info = visa_backend.report('@py', probe_bus=True, gpib_interface=PRLGX)
        assert info['gpib_interface'] == {'configured': PRLGX, 'opened': True}
        assert order == ['opened', 'listed']

    def test_bus_reports_a_failure_and_still_enumerates(self, one_rm):
        one_rm(_InterfaceRM(error=OSError('could not open port')))
        info = visa_backend.report('@py', probe_bus=True, gpib_interface=PRLGX)
        assert info['ok'] is True  # a VISA implementation did open
        assert info['gpib_interface']['opened'] is False
        assert PRLGX in info['gpib_interface']['error']
        assert info['resources'] == []

    def test_a_vendor_library_does_not_open_it(self, one_rm):
        rm = one_rm(_InterfaceRM('/Library/Frameworks/VISA.framework/VISA'))
        info = visa_backend.report('', probe_bus=True, gpib_interface=PRLGX)
        assert info['gpib_interface'] == {'configured': PRLGX, 'opened': False}
        assert rm.opened == []


@pytest.fixture
def prologix():
    # Python 3.9 resolves a pyvisa-py that predates its Prologix support.
    pytest.importorskip('pyvisa_py.prologix')
    adapter = FakePrologix()
    yield adapter
    adapter.close()


def _connections(adapter, expected, timeout=2.0):
    """``adapter.connections`` once it reaches ``expected``, or after ``timeout``.

    The stand-in counts a connection on its accept thread, which can run
    after the client's connect() has already returned.
    """
    import time
    deadline = time.monotonic() + timeout
    while adapter.connections < expected and time.monotonic() < deadline:
        time.sleep(0.01)
    return adapter.connections


#: The stand-in's board. An NI GPIB-USB adapter plugged into the machine
#: running the tests is GPIB0 (GPIB1 for a second one); nothing here may
#: name a board a real adapter could own.
BOARD = '7'
INSTRUMENT = f'GPIB{BOARD}::24::INSTR'


@pytest.fixture
def pyvisa_py_as_shipped(monkeypatch):
    """A real pyvisa-py manager that cannot reach an attached NI adapter.

    The NI GPIB-USB hook is kept from running, and the dispatchers an
    earlier test in this process let it install are taken out again:
    pyvisa-py keeps its session classes in one process-wide table, and
    ``list_resources()`` through the dispatcher walks the real bus.
    """
    monkeypatch.setattr(visa_backend, '_PY_EXTENSIONS', [])
    try:
        from pyvisa_py.sessions import Session
    except ImportError:
        return
    for key, session_class in list(Session._session_classes.items()):
        if not session_class.__module__.startswith('resistamet_gui.gpib_usb'):
            continue
        if session_class.previous is None:
            monkeypatch.delitem(Session._session_classes, key)
        else:
            monkeypatch.setitem(Session._session_classes, key, session_class.previous)


def test_as_shipped_undoes_an_install_an_earlier_test_ran(request):
    sessions = pytest.importorskip('pyvisa_py.sessions')

    def ours():
        return [cls for cls in sessions.Session._session_classes.values()
                if cls.__module__.startswith('resistamet_gui.gpib_usb')]

    # What any test that reaches resource_manager('') or ('@py') does, on a
    # machine with pyusb and libusb. It registers classes; it touches no bus.
    visa_backend._install_ni_usb()

    request.getfixturevalue('pyvisa_py_as_shipped')

    assert visa_backend._PY_EXTENSIONS == []
    assert ours() == []


@pytest.mark.usefixtures('pyvisa_py_as_shipped')
class TestGpibInterfaceOnPyvisaPy:
    """The same, against the real pyvisa-py and a Prologix stand-in on TCP."""

    def test_the_instrument_address_resolves_through_the_interface(self, prologix):
        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD))
        try:
            instrument = rm.open_resource(INSTRUMENT)
            instrument.timeout = 2000
            assert instrument.query('*IDN?').strip() == IDN
            assert '++addr 24' in prologix.lines
        finally:
            rm.close()

    def test_one_connection_however_many_callers(self, prologix):
        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD))
        try:
            assert visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD)) is rm
            assert _connections(prologix, 1) == 1
        finally:
            rm.close()

    def test_it_survives_garbage_collection(self, prologix):
        import gc
        from pyvisa_py.prologix import _PrologixIntfcSession

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD))
        try:
            gc.collect()
            assert BOARD in _PrologixIntfcSession.boards
        finally:
            rm.close()

    def test_it_closes_with_the_manager_and_reopens_with_the_next(self, prologix):
        from pyvisa_py.prologix import _PrologixIntfcSession

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD))
        _, session = getattr(rm, visa_backend._INTERFACE_ATTR)
        rm.close()
        assert visa_backend._is_open(session) is False
        assert _PrologixIntfcSession.boards == {}

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD))
        try:
            assert _connections(prologix, 2) == 2
        finally:
            rm.close()

    @pytest.mark.parametrize('let_go', [
        lambda rm: visa_backend.release_gpib_interface(rm),
        lambda rm: visa_backend.resource_manager(visa_backend.PY, ''),
    ], ids=['released', 'configured-name-cleared'])
    def test_letting_go_frees_the_adapter_while_the_manager_lives(self, prologix, let_go):
        from pyvisa_py.prologix import _PrologixIntfcSession

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD))
        try:
            _, session = getattr(rm, visa_backend._INTERFACE_ATTR)
            let_go(rm)
            assert visa_backend._is_open(session) is False
            assert BOARD not in _PrologixIntfcSession.boards
            assert visa_backend.held_gpib_interface(rm) is None

            # the same manager, still open, takes the adapter again
            assert visa_backend.resource_manager(visa_backend.PY, prologix.resource(BOARD)) is rm
            assert _connections(prologix, 2) == 2
            instrument = rm.open_resource(INSTRUMENT)
            instrument.timeout = 2000
            assert instrument.query('*IDN?').strip() == IDN
        finally:
            rm.close()

    def test_a_serial_port_that_is_not_there(self):
        pytest.importorskip('serial')
        name = 'PRLGX-ASRL::/dev/cu.resistamet-no-such-adapter::INTFC'
        try:
            with pytest.raises(visa_backend.GpibInterfaceError) as raised:
                visa_backend.resource_manager(visa_backend.PY, name)
        finally:
            visa_backend.resource_manager(visa_backend.PY).close()
        assert name in str(raised.value)
        assert 'no-such-adapter' in str(raised.value.__cause__)

    def test_a_name_pyvisa_cannot_parse(self):
        # Past this module's own check, which leaves the middle part to pyvisa.
        name = 'PRLGX-ASRL::a::b::INTFC'
        try:
            with pytest.raises(visa_backend.GpibInterfaceError) as raised:
                visa_backend.resource_manager(visa_backend.PY, name)
        finally:
            visa_backend.resource_manager(visa_backend.PY).close()
        assert name in str(raised.value)
        assert 'VI_ERROR_INV_RSRC_NAME' in str(raised.value)  # pyvisa's verdict, not ours

    def test_a_resource_that_is_not_an_interface_is_never_connected_to(self, prologix):
        # A raw socket to the stand-in: pyvisa-py would open it happily.
        name = f'TCPIP::127.0.0.1::{prologix.port}::SOCKET'
        try:
            with pytest.raises(visa_backend.GpibInterfaceError):
                visa_backend.resource_manager(visa_backend.PY, name)
        finally:
            visa_backend.resource_manager(visa_backend.PY).close()
        assert prologix.connections == 0


class TestInstrumentUsesTheChoice:
    def test_default_is_auto(self, recording_rm):
        with pytest.raises(RuntimeError):  # the recording RM lists nothing
            Keithley2400('GPIB0::24::INSTR').connect()
        assert recording_rm == [()]

    def test_the_library_reaches_pyvisa(self, recording_rm):
        with pytest.raises(RuntimeError):
            Keithley2400('GPIB0::24::INSTR', visa_library='@py').connect()
        assert recording_rm == [('@py',)]

    def test_the_interface_is_opened_before_the_instrument_is_looked_for(self, one_rm):
        rm = one_rm(_InterfaceRM())
        with pytest.raises(RuntimeError):  # this manager lists nothing either
            Keithley2400('GPIB0::24::INSTR', visa_library='@py', gpib_interface=PRLGX).connect()
        assert rm.opened == [PRLGX]

    def test_an_interface_that_will_not_open_is_what_connect_reports(self, one_rm):
        one_rm(_InterfaceRM(error=OSError('could not open port')))
        with pytest.raises(visa_backend.GpibInterfaceError, match='PRLGX-ASRL'):
            Keithley2400('GPIB0::24::INSTR', visa_library='@py', gpib_interface=PRLGX).connect()

    @pytest.mark.xfail(strict=True, raises=RuntimeError, reason=(
        "VisaInstrument.connect refuses an address that list_resources() does "
        "not return, and pyvisa-py cannot enumerate the instruments behind a "
        "Prologix adapter (PrologixInstrSession.list_resources returns [])."))
    def test_a_keithley_connects_through_a_prologix_interface(
            self, prologix, pyvisa_py_as_shipped):
        instrument = Keithley2400(INSTRUMENT, visa_library=visa_backend.PY,
                                  gpib_interface=prologix.resource(BOARD))
        try:
            instrument.connect()
            assert instrument.idn() == IDN
        finally:
            visa_backend.resource_manager(visa_backend.PY).close()


class TestNiUsbExtensionDegrades:
    """Opening a ResourceManager must not depend on the NI USB driver loading.

    pyvisa-py 0.8 requires Python 3.10; on 3.9 pip resolves an older release
    whose Session API the session module is not written against, and a lab PC
    may have no libusb at all. Either way the app still opens a bus.
    """

    def test_install_survives_a_session_module_that_cannot_import(self, monkeypatch):
        import resistamet_gui.gpib_usb as gpib_usb

        sessions = pytest.importorskip('pyvisa_py.sessions')
        table = sessions.Session._session_classes
        # Take out any dispatcher an earlier test installed, so that one put back shows.
        for key, cls in list(table.items()):
            if cls.__module__.startswith('resistamet_gui.gpib_usb'):
                monkeypatch.delitem(table, key)
        before = dict(table)
        # Unset, or _install_ni_usb returns before it reaches install().
        monkeypatch.delenv(visa_backend.DISABLE_NI_USB_ENV, raising=False)
        monkeypatch.setattr(gpib_usb, 'available', lambda: True)
        # None in sys.modules makes the import inside install() raise ImportError,
        # which is what an old pyvisa-py looks like.
        monkeypatch.setitem(sys.modules, 'resistamet_gui.gpib_usb.visa_session', None)
        visa_backend._install_ni_usb()
        assert table == before

    def test_install_is_skipped_without_libusb(self, monkeypatch):
        import resistamet_gui.gpib_usb as gpib_usb

        monkeypatch.delenv(visa_backend.DISABLE_NI_USB_ENV, raising=False)
        monkeypatch.setattr(gpib_usb, 'available', lambda: False)
        called = []
        monkeypatch.setitem(sys.modules, 'resistamet_gui.gpib_usb.visa_session',
                            type(sys)('stub'))
        sys.modules['resistamet_gui.gpib_usb.visa_session'].install = lambda: called.append(1)
        visa_backend._install_ni_usb()
        assert called == []

    @pytest.mark.parametrize('value, installs', [('1', False), ('0', True), ('', True)])
    def test_the_environment_can_switch_the_driver_off(self, monkeypatch, value, installs):
        import resistamet_gui.gpib_usb as gpib_usb

        called = []
        monkeypatch.setattr(gpib_usb, 'install', lambda: called.append(1))
        monkeypatch.setenv(visa_backend.DISABLE_NI_USB_ENV, value)
        visa_backend._install_ni_usb()
        assert bool(called) is installs

    def test_a_pyvisa_py_manager_still_opens_when_the_driver_is_absent(self, monkeypatch, caplog):
        import resistamet_gui.gpib_usb as gpib_usb

        monkeypatch.delenv(visa_backend.DISABLE_NI_USB_ENV, raising=False)
        monkeypatch.setattr(gpib_usb, 'available', lambda: True)
        monkeypatch.setitem(sys.modules, 'resistamet_gui.gpib_usb.visa_session', None)
        with caplog.at_level(logging.WARNING, logger=visa_backend.logger.name):
            rm = visa_backend.resource_manager(visa_backend.PY)
        try:
            assert visa_backend.describe(rm, visa_backend.PY)['kind'] == 'py'
        finally:
            rm.close()
        # The driver stood down by itself; resource_manager's guard around the hooks
        # was not what kept the manager opening.
        assert not [r for r in caplog.records if 'extension' in r.getMessage() and 'skipped' in r.getMessage()]
