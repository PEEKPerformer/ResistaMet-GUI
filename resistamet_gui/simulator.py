"""Public entry point for the in-package Keithley 2400-family simulator.

The simulator is the same one tests use to validate SCPI fidelity against
captured hardware traces (see ``tests/fixtures/scpi_traces*``). Enabling it
at runtime lets reviewers, demo builds, and CI smoke tests exercise the GUI
end-to-end with no instrument attached.
"""
from __future__ import annotations

from typing import Optional

import pyvisa

from ._simulator import FakeResourceManager


_active = False


def enable_simulation(
    *,
    dut_resistance_ohms: float = 100.0,
    dut_voltage_offset: float = 0.0,
    model: str = "2420",
    gpib_address: str = "GPIB0::24::INSTR",
    noise_rsd: float = 0.0,
    aux_address: str = "ASRL6::INSTR",
    sim_temp_c: float = 25.0,
) -> None:
    """Replace ``pyvisa.ResourceManager`` with the in-package fake.

    Must be called *before* any code path constructs a ResourceManager —
    in practice, before ``QApplication`` runs and before the user picks an
    instrument in the connect dialog. After this returns, every subsequent
    ``pyvisa.ResourceManager()`` instantiation hands back a
    :class:`~resistamet_gui._simulator.FakeResourceManager` that vends a
    single ``model``-flavored fake instrument at ``gpib_address``.

    Idempotent — safe to call more than once; later calls override earlier
    DUT/model parameters.
    """
    global _active

    def _factory(*_args, **_kwargs):
        return FakeResourceManager(
            gpib_address=gpib_address,
            dut_resistance_ohms=dut_resistance_ohms,
            dut_voltage_offset=dut_voltage_offset,
            model=model,
            noise_rsd=noise_rsd,
            aux_address=aux_address,
            sim_temp_c=sim_temp_c,
        )

    pyvisa.ResourceManager = _factory  # type: ignore[assignment]
    _active = True


def is_simulating() -> bool:
    """Return True if :func:`enable_simulation` has been called this process."""
    return _active
