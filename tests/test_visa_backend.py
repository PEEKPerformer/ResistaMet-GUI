"""Choosing the VISA implementation that opens the bus."""
import pyvisa
import pytest

from resistamet_gui import visa_backend
from resistamet_gui.instrument import Keithley2400


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

    def test_py_extensions_run_before_a_pyvisa_py_manager(self, recording_rm, monkeypatch):
        ran = []
        monkeypatch.setattr(visa_backend, '_PY_EXTENSIONS', [])
        visa_backend.register_py_extension(lambda: ran.append('hook'))
        visa_backend.register_py_extension(lambda: ran.append('hook'))

        visa_backend.resource_manager('')
        assert ran == []  # only pyvisa-py gets extensions

        visa_backend.resource_manager('@py')
        assert ran == ['hook', 'hook']

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


class TestInstrumentUsesTheChoice:
    def test_default_is_auto(self, recording_rm):
        with pytest.raises(RuntimeError):  # the recording RM lists nothing
            Keithley2400('GPIB0::24::INSTR').connect()
        assert recording_rm == [()]

    def test_the_library_reaches_pyvisa(self, recording_rm):
        with pytest.raises(RuntimeError):
            Keithley2400('GPIB0::24::INSTR', visa_library='@py').connect()
        assert recording_rm == [('@py',)]
