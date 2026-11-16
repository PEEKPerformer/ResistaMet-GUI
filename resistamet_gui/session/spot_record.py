"""What a four-point run records about the spot it measures.

A run that carries a spot writes it into the file header, together with the
sample outline and -- when the spot has a position on a bounded sample -- what
the position costs: the geometry factor there, the factor at the centre, the
error made by assuming the centre, and how far the nearest tip is from an edge.

The position effect is *reported, never applied*. The per-sample Rs, rho and
sigma come from the F84 / Smits table look-up exactly as before
(``session/samples.py``); ASTM F84 measures at the centre, and whether a
position-aware correction may be offered at all is an open question of
``docs/design/four_point_probe_spots.md``.

Three factors are recorded, because two comparisons are possible and only one
of them is about the numbers in the file:

* ``factor_here``   -- closed form, at the spot, for the ``fpp_sample_*``
  outline (or the legacy keys mapped onto one).
* ``factor_centre`` -- closed form, at the centre of that same outline.
  ``relative_error = factor_centre / factor_here - 1`` is what the position
  costs *if the rows used the centred factor of this outline*.
* ``factor_rows``   -- the lateral factor the rows really applied, which comes
  from ``fpp_geometry`` / ``fpp_diameter_cm`` and the tables, or from K*alpha
  when no diameter was entered. ``relative_error_rows = factor_rows /
  factor_here - 1`` is the error actually in the file's Rs. It equals
  ``relative_error`` when both describe the same sample (to the tables'
  printed digits) and is much larger when, say, the outline is a 20 mm square
  and the rows assume an unbounded sheet.

The edge warning uses ``relative_error_rows`` whenever it exists.

Pure: no Qt, no instrument, no I/O.
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .. import calculations_geometry as geo
from ..calculations import f_thickness_correction
from ..schema.spots import (SampleGeometry, SpotPreflight, SpotRequest,
                            sample_geometry_from_settings)
from .samples import build_row


#: What ``build_row`` takes the probe spacing to be when the settings hold
#: none (``samples.py``). The same value here, so a run whose rows would be
#: computed is not refused over a spacing the rows never needed.
_DEFAULT_SPACING_CM = 0.1016


@dataclass(frozen=True)
class SpotPosition:
    """The position effect at one spot. Factors are None off the sample."""

    edge_clearance_s: float
    factor_here: Optional[float] = None
    factor_centre: Optional[float] = None
    relative_error: Optional[float] = None

    @property
    def off_sample(self) -> bool:
        """A tip on the edge counts as off: the factor diverges there."""
        return not self.edge_clearance_s > 0


def check_spot_position(geometry: SampleGeometry, spacing_mm: float, x_mm: float,
                        y_mm: float, angle_deg: float) -> Optional[SpotPosition]:
    """Edge clearance and position effect, or None for an unbounded sample."""
    centre = (x_mm, y_mm)
    angle = math.radians(angle_deg)
    if geometry.shape == 'circle':
        clearance = geo.circle_edge_clearance(geometry.diameter_mm, spacing_mm, centre, angle)
        if not clearance > 0:
            return SpotPosition(edge_clearance_s=clearance)
        effect = geo.circle_position_effect(geometry.diameter_mm, spacing_mm, centre, angle)
    elif geometry.shape == 'rectangle':
        clearance = geo.rectangle_edge_clearance(
            geometry.width_mm, geometry.length_mm, spacing_mm, centre, angle)
        if not clearance > 0:
            return SpotPosition(edge_clearance_s=clearance)
        effect = geo.rectangle_position_effect(
            geometry.width_mm, geometry.length_mm, spacing_mm, centre, angle)
    else:
        return None
    return SpotPosition(clearance, effect.factor_here, effect.factor_centre,
                        effect.relative_error)


def rows_lateral_factor(measurement: Dict[str, Any]) -> Optional[float]:
    """The lateral geometry factor the run's rows apply: their Rs / (V/I).

    Asked of ``build_row`` itself, with one made-up reading, rather than
    worked out again here: which correction path a run takes (F84 tables or
    K*alpha) and with which inputs is decided in one place, and a second copy
    of that decision is a second answer waiting to happen. Once per run,
    before the instrument is opened; the per-sample path is not touched.

    On the F84 path the rows' Rs also carries the thickness term F(w/S). That
    term is divided out, because the factors this is compared with are
    thin-sheet, lateral-only values and F84 keeps thickness as a separate
    multiplicative correction; for w/S < 0.4 it is 1 and nothing changes.

    None when the rows have no finite Rs to speak of (the F84 path with no
    thickness entered).
    """
    reading = {'voltage': 1e-3, 'current': 1e-3}          # V/I = 1 ohm
    # Model and NPLC only feed the uncertainty columns, not Rs.
    _, derived = build_row('four_point', 0.0, reading, 'OK', '', measurement,
                           1.0, False, '2400', None)
    rs, ratio = derived.get('rs'), derived.get('ratio')
    if not (_is_finite(rs) and _is_finite(ratio)) or ratio == 0:
        return None
    factor = rs / ratio
    if derived.get('method') == 'f84':
        thickness_cm = float(measurement.get('fpp_thickness_um') or 0.0) * 1e-4
        spacing_cm = float(measurement.get('fpp_spacing_cm') or _DEFAULT_SPACING_CM)
        thickness_term = f_thickness_correction(thickness_cm, spacing_cm)
        if not _is_finite(thickness_term) or thickness_term == 0:
            return None
        factor /= thickness_term
    return factor if _is_finite(factor) else None


def _is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True)
class SpotRecord:
    """A run's spot, resolved against the run's settings."""

    spot: SpotRequest
    geometry: SampleGeometry
    #: The spot's own angle, or the ``fpp_array_angle_deg`` setting.
    angle_deg: float
    position_correction: str
    edge_warn_pct: float
    #: None when the spot has no position or the sample no edges.
    position: Optional[SpotPosition] = None
    #: What ``fpp_position_correction`` held when it was not 'warn'. Nothing
    #: but 'warn' is implemented, so nothing else is ever recorded as in
    #: force; the run says so in its log.
    ignored_position_correction: Optional[str] = None
    #: See ``rows_lateral_factor``. None when the rows have no finite Rs.
    factor_rows: Optional[float] = None

    @property
    def off_sample(self) -> bool:
        return self.position is not None and self.position.off_sample

    @property
    def relative_error_rows(self) -> Optional[float]:
        """``factor_rows / factor_here - 1``: the error in the file's own Rs."""
        if self.position is None or self.position.factor_here is None:
            return None
        if self.factor_rows is None:
            return None
        return self.factor_rows / self.position.factor_here - 1.0

    @property
    def warning_error(self) -> Optional[float]:
        """The error the edge warning is judged on: against the rows' factor
        when there is one, else against the outline's own centre."""
        if self.relative_error_rows is not None:
            return self.relative_error_rows
        return None if self.position is None else self.position.relative_error

    @property
    def warning_compares_with(self) -> str:
        """'rows' or 'centre': which of the two ``warning_error`` is."""
        return 'rows' if self.relative_error_rows is not None else 'centre'

    @property
    def near_edge(self) -> bool:
        """The position costs more here than the operator allows."""
        error = self.warning_error
        return error is not None and abs(error) * 100.0 > self.edge_warn_pct

    def header(self) -> Dict[str, Any]:
        """The ``spot`` block of the file header."""
        block: Dict[str, Any] = {
            'map_id': self.spot.map_id,
            'index': self.spot.index,
            'label': self.spot.label,
            'x_mm': self.spot.x_mm,
            'y_mm': self.spot.y_mm,
            'angle_deg': self.angle_deg,
            'sample': self.geometry.model_dump(),
            'position_correction': self.position_correction,
            'edge_warn_pct': self.edge_warn_pct,
        }
        if self.position is not None:
            block.update({
                'factor_here': self.position.factor_here,
                'factor_centre': self.position.factor_centre,
                'relative_error': self.position.relative_error,
                'factor_rows': self.factor_rows,
                'relative_error_rows': self.relative_error_rows,
                'edge_clearance_s': self.position.edge_clearance_s,
            })
        return block


def spot_record_from_settings(settings: Dict[str, Any]) -> Optional[SpotRecord]:
    """The spot in ``settings['spot']``, checked against the sample outline.

    None when the run carries no spot. Raises ``ValueError`` when the spot or
    the outline cannot be described; the caller refuses the run.
    """
    raw = settings.get('spot')
    if not raw:
        return None
    spot = raw if isinstance(raw, SpotRequest) else SpotRequest.model_validate(raw)
    measurement = settings.get('measurement', {})
    geometry = sample_geometry_from_settings(measurement)
    angle_deg = spot.angle_deg
    if angle_deg is None:
        angle_deg = float(measurement.get('fpp_array_angle_deg') or 0.0)
    position = None
    if spot.has_position:
        spacing_mm = float(measurement.get('fpp_spacing_cm') or _DEFAULT_SPACING_CM) * 10.0
        position = check_spot_position(geometry, spacing_mm, spot.x_mm, spot.y_mm, angle_deg)
    # Only a checked position has anything to compare the rows' factor with.
    factor_rows = rows_lateral_factor(measurement) if position is not None else None
    # The schema accepts only 'warn', but the PySide6 path hands over settings
    # no schema has seen. A hand-edited 'apply' must not be written into a
    # file whose numbers had no correction applied.
    requested = str(measurement.get('fpp_position_correction') or 'warn')
    return SpotRecord(
        spot=spot,
        geometry=geometry,
        angle_deg=angle_deg,
        position_correction='warn',
        ignored_position_correction=None if requested == 'warn' else requested,
        edge_warn_pct=float(measurement.get('fpp_edge_warn_pct', 1.0)),
        position=position,
        factor_rows=factor_rows,
    )


def position_message(record: SpotRecord) -> Optional[str]:
    """What a run says about this spot's position before its first sample.

    The refusal for a spot that is off the sample, the warning for one near
    an edge, None otherwise. The text of the run's ``geometry_warning`` event
    (``ContinuousRun``); a test holds the two together.
    """
    position = record.position
    if position is None:
        return None
    if record.off_sample:
        return (f"Spot '{record.spot.label}' is off the sample: a probe tip is "
                f"{abs(position.edge_clearance_s):.2f} s beyond the edge.")
    if not record.near_edge:
        return None
    if record.warning_compares_with == 'rows':
        compared = (f"the geometry factor this run applies ({record.factor_rows:.4g}) "
                    f"differs from the factor at the spot ({position.factor_here:.4g})")
    else:
        compared = (f"the factor at the centre of the sample ({position.factor_centre:.4g}) "
                    f"differs from the factor at the spot ({position.factor_here:.4g})")
    return (f"Spot '{record.spot.label}' is {position.edge_clearance_s:.1f} s from "
            f"the edge: {compared} by {abs(record.warning_error) * 100.0:.1f} % "
            f"(threshold {record.edge_warn_pct:g} %). No position correction is applied.")


def centred_factor(geometry: SampleGeometry, spacing_mm: float,
                   angle_deg: float) -> Optional[float]:
    """The closed-form factor with the probe at the centre of the outline.

    None when the probe does not fit on the sample even there.
    """
    if geometry.shape == 'unbounded':
        return geo.UNBOUNDED_FACTOR
    position = check_spot_position(geometry, spacing_mm, 0.0, 0.0, angle_deg)
    return None if position is None or position.off_sample else position.factor_here


def spot_preflight(settings: Dict[str, Any]) -> SpotPreflight:
    """What a four-point run of ``settings`` would record, without the run.

    ``settings`` are resolved run settings, with or without a ``spot``. The
    spot goes through ``spot_record_from_settings`` exactly as the run's
    does, so the numbers shown before Start are the numbers in the file.
    Raises ``ValueError`` as that function does.
    """
    measurement = settings.get('measurement', {})
    record = spot_record_from_settings(settings)
    if record is None:
        geometry = sample_geometry_from_settings(measurement)
        angle_deg = float(measurement.get('fpp_array_angle_deg') or 0.0)
        edge_warn_pct = float(measurement.get('fpp_edge_warn_pct', 1.0))
    else:
        geometry, angle_deg, edge_warn_pct = (record.geometry, record.angle_deg,
                                              record.edge_warn_pct)
    spacing_mm = float(measurement.get('fpp_spacing_cm') or _DEFAULT_SPACING_CM) * 10.0
    found = SpotPreflight(
        sample=geometry, spacing_mm=spacing_mm, angle_deg=angle_deg,
        edge_warn_pct=edge_warn_pct,
        geometry_factor=centred_factor(geometry, spacing_mm, angle_deg),
        factor_rows=rows_lateral_factor(measurement),
    )
    if record is None or record.position is None:
        return found
    position = record.position
    return found.model_copy(update={
        'checked': True,
        'off_sample': record.off_sample,
        'near_edge': record.near_edge,
        'edge_clearance_s': position.edge_clearance_s,
        'factor_here': position.factor_here,
        'factor_centre': position.factor_centre,
        'relative_error': position.relative_error,
        'factor_rows': record.factor_rows,
        'relative_error_rows': record.relative_error_rows,
        'compared_with': record.warning_compares_with,
        'message': position_message(record),
    })
