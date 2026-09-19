"""The bus, opened for the UI the way this machine's runs open it.

Scan and Test answer "can a run reach the instrument?", so they have to go
through the VISA implementation and GPIB interface a run would use. Opening
pyvisa's default instead tells the operator "not found" about an instrument
the run reaches through pyvisa-py, or the reverse.
"""
from typing import Any

from .. import visa_backend
from ..config import ConfigManager


def configured_resource_manager(config_manager: ConfigManager) -> Any:
    """The ResourceManager for this machine's VISA backend and GPIB interface.

    Never close it: pyvisa hands the same manager to every caller of the
    same library, and closing it severs every session open on it — the aux
    preview's, or a run's (see ``VisaInstrument.close``).
    """
    return visa_backend.resource_manager(
        config_manager.get_visa_library(), config_manager.get_gpib_interface())
