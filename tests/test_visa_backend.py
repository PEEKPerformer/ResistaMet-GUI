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

    def test_a_caller_without_one_leaves_it_open(self, one_rm):
        rm = one_rm(_InterfaceRM())
        visa_backend.resource_manager('@py', PRLGX)
        visa_backend.resource_manager('@py')
        assert rm.sessions[0].closed is False

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

    def test_the_simulator_has_no_adapter_to_open(self):
        from resistamet_gui import simulator
        simulator.enable_simulation()
        try:
            rm = visa_backend.resource_manager('', PRLGX)
            assert 'GPIB0::24::INSTR' in rm.list_resources()
        finally:
            simulator.disable_simulation()


@pytest.fixture
def prologix():
    adapter = FakePrologix()
    yield adapter
    adapter.close()


class TestGpibInterfaceOnPyvisaPy:
    """The same, against the real pyvisa-py and a Prologix stand-in on TCP."""

    def test_the_instrument_address_resolves_through_the_interface(self, prologix):
        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource())
        try:
            instrument = rm.open_resource('GPIB0::24::INSTR')
            instrument.timeout = 2000
            assert instrument.query('*IDN?').strip() == IDN
            assert '++addr 24' in prologix.lines
        finally:
            rm.close()

    def test_one_connection_however_many_callers(self, prologix):
        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource())
        try:
            assert visa_backend.resource_manager(visa_backend.PY, prologix.resource()) is rm
            assert prologix.connections == 1
        finally:
            rm.close()

    def test_it_survives_garbage_collection(self, prologix):
        import gc
        from pyvisa_py.prologix import _PrologixIntfcSession

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource())
        try:
            gc.collect()
            assert '0' in _PrologixIntfcSession.boards
        finally:
            rm.close()

    def test_it_closes_with_the_manager_and_reopens_with_the_next(self, prologix):
        from pyvisa_py.prologix import _PrologixIntfcSession

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource())
        _, session = getattr(rm, visa_backend._INTERFACE_ATTR)
        rm.close()
        assert visa_backend._is_open(session) is False
        assert _PrologixIntfcSession.boards == {}

        rm = visa_backend.resource_manager(visa_backend.PY, prologix.resource())
        try:
            assert prologix.connections == 2
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
        try:
            with pytest.raises(visa_backend.GpibInterfaceError) as raised:
                visa_backend.resource_manager(visa_backend.PY, 'PRLGX-ASRL::INTFC')
        finally:
            visa_backend.resource_manager(visa_backend.PY).close()
        assert 'PRLGX-ASRL::INTFC' in str(raised.value)


class TestInstrumentUsesTheChoice:
    def test_default_is_auto(self, recording_rm):
        with pytest.raises(RuntimeError):  # the recording RM lists nothing
            Keithley2400('GPIB0::24::INSTR').connect()
        assert recording_rm == [()]

    def test_the_library_reaches_pyvisa(self, recording_rm):
        with pytest.raises(RuntimeError):
            Keithley2400('GPIB0::24::INSTR', visa_library='@py').connect()
        assert recording_rm == [('@py',)]


class TestNiUsbExtensionDegrades:
    """Opening a ResourceManager must not depend on the NI USB driver loading.

    pyvisa-py 0.8 requires Python 3.10; on 3.9 pip resolves an older release
    whose Session API the session module is not written against, and a lab PC
    may have no libusb at all. Either way the app still opens a bus.
    """

    def test_install_survives_a_session_module_that_cannot_import(self, monkeypatch):
        import resistamet_gui.gpib_usb as gpib_usb

        monkeypatch.setattr(gpib_usb, 'available', lambda: True)
        # None in sys.modules makes the import inside install() raise ImportError,
        # which is what an old pyvisa-py looks like.
        monkeypatch.setitem(sys.modules, 'resistamet_gui.gpib_usb.visa_session', None)
        visa_backend._install_ni_usb()

    def test_install_is_skipped_without_libusb(self, monkeypatch):
        import resistamet_gui.gpib_usb as gpib_usb

        monkeypatch.setattr(gpib_usb, 'available', lambda: False)
        called = []
        monkeypatch.setitem(sys.modules, 'resistamet_gui.gpib_usb.visa_session',
                            type(sys)('stub'))
        sys.modules['resistamet_gui.gpib_usb.visa_session'].install = lambda: called.append(1)
        visa_backend._install_ni_usb()
        assert called == []

    def test_a_pyvisa_py_manager_still_opens_when_the_driver_is_absent(self, monkeypatch):
        import resistamet_gui.gpib_usb as gpib_usb

        monkeypatch.setattr(gpib_usb, 'available', lambda: True)
        monkeypatch.setitem(sys.modules, 'resistamet_gui.gpib_usb.visa_session', None)
        rm = visa_backend.resource_manager(visa_backend.PY)
        try:
            assert visa_backend.describe(rm, visa_backend.PY)['kind'] == 'py'
        finally:
            rm.close()
