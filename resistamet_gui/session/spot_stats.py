"""What one placement of the four-point probe measured, as statistics.

A spot is N samples at one place. Its result is the mean of each derived
quantity with two uncertainties: the scatter of the samples and the
instrument's accuracy floor. Both UIs used to compute these for themselves
from the rows they had seen; computed here, the file, the event and every
client carry the same numbers.

The combination of the two uncertainties is
:func:`resistamet_gui.calculations.four_point_combined_uncertainty`, the
function the PySide6 result panel already calls, with the same inputs: the
samples that are not in compliance. A sample in compliance records a bound on
the quantity, not a measurement of it, so it is counted and left out.

Pure: no Qt, no instrument, no I/O.
"""
import math
from array import array
from typing import Any, Dict, Sequence

from ..calculations import four_point_combined_uncertainty

#: The derived quantities a spot reports, as named in ``sample.derived``.
QUANTITIES = ('rs', 'rho', 'sigma')

_NAN = float('nan')


class SpotSamples:
    """The numbers of a spot's samples, kept column by column.

    Packed doubles rather than a list of rows: a four-point run with no sample
    target runs until it is stopped, and this must not be what grows without
    bound in a way anyone notices (40 bytes a sample).
    """

    def __init__(self):
        self.voltage = array('d')
        self.current = array('d')
        self.rs = array('d')
        self.rho = array('d')
        self.sigma = array('d')
        #: Samples taken in compliance, which the columns above leave out.
        self.excluded = 0

    def __len__(self) -> int:
        return len(self.voltage)

    def add(self, voltage: Any, current: Any, derived: Dict[str, Any],
            compliance: str = 'OK') -> None:
        """Record one sample. ``derived`` is the dict ``build_row`` returns.

        Every value is converted before any column is touched, so a sample is
        recorded whole or not at all and the columns cannot end up with
        different lengths. Anything that is not a number becomes NaN; a
        ``derived`` that is not a dict is a sample with no derived values.
        """
        if compliance != 'OK':
            self.excluded += 1
            return
        if not isinstance(derived, dict):
            derived = {}
        row = (_as_float(voltage), _as_float(current), _as_float(derived.get('rs')),
               _as_float(derived.get('rho')), _as_float(derived.get('sigma')))
        for column, value in zip((self.voltage, self.current, self.rs, self.rho, self.sigma), row):
            column.append(value)


def quantity_statistics(values: Sequence[float], v_readings: Sequence[float],
                        i_readings: Sequence[float], model: str = "2400",
                        nplc: float = 1.0) -> Dict[str, Any]:
    """n, mean, sample SD, RSD and the combined uncertainty of one quantity.

    Non-finite values are skipped and ``n`` counts what is left. With no
    finite value every number is NaN. With one, the mean stands but ``sd`` and
    ``rsd_pct`` are NaN: a sample standard deviation of one value is not zero,
    it is undefined, and a file should not claim a spread that was never
    observed. ``u_stat``, ``u_inst`` and ``u_total`` are exactly what
    ``four_point_combined_uncertainty`` returns, so they match the PySide6
    panel (which reports ``u_stat`` as 0 for a single sample).
    """
    finite = [float(v) for v in values if _is_finite_number(v)]
    n = len(finite)
    combined = four_point_combined_uncertainty(
        list(values), list(v_readings), list(i_readings), model=model, nplc=nplc)
    if n == 0 or combined is None:
        return {'n': 0, 'mean': _NAN, 'sd': _NAN, 'rsd_pct': _NAN,
                'u_stat': _NAN, 'u_inst': _NAN, 'u_total': _NAN}
    mean = math.fsum(finite) / n
    if n > 1:
        sd = math.sqrt(math.fsum((x - mean) ** 2 for x in finite) / (n - 1))
    else:
        sd = _NAN
    rsd_pct = sd / abs(mean) * 100.0 if mean != 0 else _NAN
    return {'n': n, 'mean': mean, 'sd': sd, 'rsd_pct': rsd_pct,
            'u_stat': combined.u_stat, 'u_inst': combined.u_inst,
            'u_total': combined.u_total}


def spot_statistics(samples: SpotSamples, model: str = "2400",
                    nplc: float = 1.0) -> Dict[str, Any]:
    """The statistics block of one spot, as written to the file footer.

    ``n`` is the number of samples that entered the statistics and
    ``n_excluded`` the number left out for being in compliance; each
    quantity's own ``n`` can be smaller still, because a conductivity is NaN
    when no thickness was entered.
    """
    stats: Dict[str, Any] = {'n': len(samples), 'n_excluded': samples.excluded}
    for name in QUANTITIES:
        stats[name] = quantity_statistics(
            getattr(samples, name), samples.voltage, samples.current,
            model=model, nplc=nplc)
    return stats


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _as_float(value: Any) -> float:
    """A float, or NaN for anything that cannot be made one."""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return _NAN
