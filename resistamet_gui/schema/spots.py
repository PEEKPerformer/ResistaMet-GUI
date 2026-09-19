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
from typing import Literal, Optional

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
