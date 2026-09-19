"""A sample's outline and a spot on it, as a run request describes them.

A four-point-probe characterisation is several placements of the probe on one
sample. A *spot* is one placement; a *map* is the set of runs that share a
``map_id``. See ``docs/design/four_point_probe_spots.md``.

Lengths are millimetres, measured from the centre of the sample. A rectangle's
``length_mm`` lies along x and its ``width_mm`` along y, and an array angle of
0 degrees points along x -- the arrangement of the Smits table, whose ``D`` is
the width (``calculations_geometry.rectangle_factor`` uses the same
convention).

No Qt, no pyvisa: importable from anywhere.
"""
import math
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A map id becomes part of a file name (``<map_id>_map.json``) and of a URL,
#: so it is a plain token: no separators, no dots, nothing a path could be
#: built from.
MAP_ID_PATTERN = r'^[A-Za-z0-9_-]{1,64}$'

#: A label is written into a ``# key: value`` header line, so it must stay on
#: that line: no control characters.
LABEL_PATTERN = r'^[^\x00-\x1f\x7f]+$'

#: Which dimensions each shape is described by.
_DIMENSIONS = {
    'unbounded': (),
    'circle': ('diameter_mm',),
    'rectangle': ('width_mm', 'length_mm'),
}


class SampleGeometry(BaseModel):
    """The lateral outline of a thin sample with insulating edges.

    ``unbounded`` means a sheet large enough that its edges do not matter,
    which is what the software assumed before it knew about outlines.
    """

    model_config = ConfigDict(extra='forbid')

    shape: Literal['unbounded', 'circle', 'rectangle'] = 'unbounded'
    diameter_mm: Optional[float] = Field(default=None, gt=0.0, allow_inf_nan=False)
    width_mm: Optional[float] = Field(default=None, gt=0.0, allow_inf_nan=False)
    length_mm: Optional[float] = Field(default=None, gt=0.0, allow_inf_nan=False)

    @model_validator(mode='after')
    def _dimensions_match_the_shape(self):
        needed = _DIMENSIONS[self.shape]
        for name in ('diameter_mm', 'width_mm', 'length_mm'):
            value = getattr(self, name)
            if name in needed and value is None:
                raise ValueError(f"a {self.shape} sample needs {name}")
            # A dimension the shape does not have is refused rather than
            # ignored: a stray width on a circle is more likely a wrong shape
            # than a number nobody meant.
            if name not in needed and value is not None:
                raise ValueError(f"a {self.shape} sample has no {name}")
        return self


class SpotRequest(BaseModel):
    """One placement of the probe, as the client describes it.

    The position is optional -- a spot can be a label and nothing more -- but
    ``x_mm`` and ``y_mm`` only mean something together. ``angle_deg`` is the
    direction of the probe array, anticlockwise from +x; absent, the run uses
    the ``fpp_array_angle_deg`` setting.
    """

    model_config = ConfigDict(extra='forbid')

    map_id: str = Field(pattern=MAP_ID_PATTERN)
    index: int = Field(ge=0, le=9999)
    label: str = Field(min_length=1, max_length=80, pattern=LABEL_PATTERN)
    x_mm: Optional[float] = Field(default=None, allow_inf_nan=False)
    y_mm: Optional[float] = Field(default=None, allow_inf_nan=False)
    angle_deg: Optional[float] = Field(default=None, ge=-360.0, le=360.0, allow_inf_nan=False)

    @model_validator(mode='after')
    def _position_is_a_pair(self):
        if (self.x_mm is None) != (self.y_mm is None):
            raise ValueError("a spot position needs both x_mm and y_mm")
        return self

    @property
    def has_position(self) -> bool:
        return self.x_mm is not None and self.y_mm is not None


#: Legacy ``fpp_geometry`` value -> length / width of the rectangle it names.
_LEGACY_ASPECT = {'square': 1.0, 'rectangle_2': 2.0, 'rectangle_3': 3.0, 'rectangle_4': 4.0}


def legacy_sample_geometry(measurement: Dict[str, Any]) -> SampleGeometry:
    """The outline ``fpp_geometry`` and ``fpp_diameter_cm`` describe.

    These two keys feed the F84 and Smits table look-ups. ``fpp_diameter_cm``
    is the circle's diameter or, for the rectangles, the Smits ``D`` -- the
    side across the probe array -- with the named aspect ratio giving the side
    along it. A diameter of 0 means "treat the specimen as infinite".
    """
    lateral_mm = float(measurement.get('fpp_diameter_cm') or 0.0) * 10.0
    if not lateral_mm > 0:
        return SampleGeometry(shape='unbounded')
    legacy_shape = str(measurement.get('fpp_geometry') or 'circle')
    if legacy_shape == 'circle':
        return SampleGeometry(shape='circle', diameter_mm=lateral_mm)
    if legacy_shape not in _LEGACY_ASPECT:
        raise ValueError(f"unknown fpp_geometry '{legacy_shape}'")
    return SampleGeometry(shape='rectangle', width_mm=lateral_mm,
                          length_mm=_LEGACY_ASPECT[legacy_shape] * lateral_mm)


def sample_geometry_from_settings(measurement: Dict[str, Any]) -> SampleGeometry:
    """The sample outline a four-point run's settings describe.

    The ``fpp_sample_*`` keys when a shape has been chosen; otherwise the
    legacy keys, so a profile written before outlines existed describes the
    same sample it always did. Only the dimensions the chosen shape has are
    read: a form keeps the width of a rectangle around after the operator
    switches to a circle, and that is not an error.

    Raises ``ValueError`` (pydantic's ``ValidationError`` is one) when the
    shape's dimensions are missing.
    """
    shape = str(measurement.get('fpp_sample_shape') or 'unbounded')
    if shape == 'unbounded':
        return legacy_sample_geometry(measurement)
    dimensions = {}
    for name in _DIMENSIONS.get(shape, ()):
        value = float(measurement.get(f'fpp_sample_{name}') or 0.0)
        # 0 is the profile's "not entered"; the model reports it as missing.
        dimensions[name] = value if value > 0 else None
    return SampleGeometry(shape=shape, **dimensions)


def same_outline(first: SampleGeometry, second: SampleGeometry) -> bool:
    """Equal up to the rounding of a cm -> mm conversion."""
    if first.shape != second.shape:
        return False
    return all(
        math.isclose(getattr(first, name), getattr(second, name), rel_tol=1e-9)
        for name in _DIMENSIONS[first.shape]
    )
