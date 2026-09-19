"""What a four-point run records about the spot it measures.

A run that carries a spot writes it into the file header, together with the
sample outline and -- when the spot has a position on a bounded sample -- what
the position costs: the geometry factor there, the factor at the centre, the
error made by assuming the centre, and how far the nearest tip is from an edge.

The position effect is *reported, never applied*. The per-sample Rs, rho and
sigma come from the F84 / Smits table look-up exactly as before
(``session/samples.py``); ASTM F84 measures at the centre, and whether a
position-aware correction may be offered at all is an open question of
``docs/design/four_point_probe_spots.md``. Note that ``factor_centre`` is the
closed-form value, which agrees with the tables to their printed digits except
for the three entries that document lists.

Pure: no Qt, no instrument, no I/O.
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .. import calculations_geometry as geo
from ..schema.spots import SampleGeometry, SpotRequest, sample_geometry_from_settings


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

    @property
    def off_sample(self) -> bool:
        return self.position is not None and self.position.off_sample

    @property
    def near_edge(self) -> bool:
        """Assuming a centred probe costs more here than the operator allows."""
        if self.position is None or self.position.relative_error is None:
            return False
        return abs(self.position.relative_error) * 100.0 > self.edge_warn_pct

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
        spacing_mm = float(measurement.get('fpp_spacing_cm') or 0.0) * 10.0
        position = check_spot_position(geometry, spacing_mm, spot.x_mm, spot.y_mm, angle_deg)
    return SpotRecord(
        spot=spot,
        geometry=geometry,
        angle_deg=angle_deg,
        position_correction=str(measurement.get('fpp_position_correction') or 'warn'),
        edge_warn_pct=float(measurement.get('fpp_edge_warn_pct', 1.0)),
        position=position,
    )
