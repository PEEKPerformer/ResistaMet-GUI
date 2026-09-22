"""Which map, and which spot of it, a run started by hand belongs to.

A client with a picture of the sample knows its map: the operator opened one.
The PySide6 window has no map, only a *Spot name* field, a spot counter and
*Save Spot* / *Clear All* buttons. This module turns those into the ``spot``
block a four-point run carries (``spots.SpotRequest``), so the files the
window writes say which spot they are and that they belong together. See
``docs/design/four_point_probe_spots.md``, step 5.

**A mapping session** is the runs one user makes on one sample name between
two resets. A new ``map_id`` is minted by the first four-point run

* after the application starts,
* after the user or the sample name differs from the previous run's, or
* after *Clear All*.

The rule is conservative: when in doubt the runs go to a new map. Two maps of
one sample can be merged afterwards from their files; two samples that were
written into one map cannot be told apart again. A map therefore never
outlives the process, and coming back to a sample later starts a second map
for it rather than continuing the first. The user is part of the rule because
the data files, and the ``<map_id>_map.json`` beside them, live in a directory
per user.

The id is minted when the run starts, not when the name is typed, so it
carries the time of the map's first run and the name that run was filed under.

No Qt, no pyvisa: importable from anywhere.
"""
import re
import secrets
import unicodedata
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from .spots import SpotRequest

#: Bounds of ``SpotRequest``, repeated here because the model keeps them in
#: its field definitions. ``test_schema_map_session`` pins them to the model.
MAX_MAP_ID_LENGTH = 64
MAX_LABEL_LENGTH = 80
MAX_INDEX = 9999

_NOT_A_TOKEN_CHARACTER = re.compile(r'[^A-Za-z0-9_-]+')
_TOKEN = re.compile(r'^[A-Za-z0-9]{1,8}$')


def auto_label(index: int) -> str:
    """The name the window gives a spot the operator did not name."""
    return f"Spot {index}"


def spot_index(counter: Any) -> int:
    """The window's spot counter as an index ``SpotRequest`` accepts."""
    try:
        return min(max(int(counter), 0), MAX_INDEX)
    except (TypeError, ValueError):
        return 0


def spot_label(text: Any, index: int) -> str:
    """``text`` as a label ``SpotRequest`` accepts, or the automatic name.

    The label goes into a ``# key: value`` header line, so anything that could
    end the line -- control characters, and the Unicode line and paragraph
    separators some readers also break on -- becomes a space. Runs of
    whitespace collapse, the ends are trimmed and the result is cut to the
    model's length. What is left of "   " or "\\n" is nothing, and nothing is
    named like a spot nobody named.
    """
    cleaned = ''.join(
        ' ' if unicodedata.category(char) in ('Cc', 'Zl', 'Zp') else char
        for char in str(text or '')
    )
    cleaned = ' '.join(cleaned.split())[:MAX_LABEL_LENGTH].strip()
    return cleaned or auto_label(index)


def new_map_id(sample_name: Any, now: Optional[datetime] = None,
               token: Optional[str] = None) -> str:
    """A fresh map id: ``<YYYYMMDD-HHMMSS>_<sample>_<token>``.

    The time (local, as the operator reads the clock) and the sample name make
    the id recognisable in a directory listing. The random token makes it
    unique: *Clear All* followed by a run within the same second, or two
    machines writing into one synchronised directory, must not share a map.

    The sample name is reduced to the characters ``MAP_ID_PATTERN`` allows and
    cut so the whole id fits; a name with none of them left is ``sample``.
    """
    stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
    if token is None or not _TOKEN.match(token):
        token = secrets.token_hex(2)
    room = MAX_MAP_ID_LENGTH - len(stamp) - len(token) - 2
    name = _NOT_A_TOKEN_CHARACTER.sub('_', str(sample_name or '')).strip('_-')
    name = name[:room].strip('_-') or 'sample'
    return f"{stamp}_{name}_{token}"


class MapSession:
    """The map the window's four-point runs currently belong to."""

    def __init__(self, clock: Callable[[], datetime] = datetime.now):
        self._clock = clock
        self._map_id: Optional[str] = None
        self._minted_for: Optional[Tuple[str, str]] = None

    @property
    def map_id(self) -> Optional[str]:
        """The current map, or None until a run has needed one."""
        return self._map_id

    def reset(self) -> None:
        """Forget the map: the next run starts a new one (*Clear All*)."""
        self._map_id = None
        self._minted_for = None

    def spot_for_run(self, username: Any, sample_name: Any, counter: Any,
                     label_text: Any) -> Dict[str, Any]:
        """The ``settings['spot']`` of a four-point run that starts now.

        ``counter`` and ``label_text`` are the window's spot counter and the
        text of its *Spot name* field. The run is started *for* the current
        spot; *Save Spot* archives it afterwards and moves the counter on, so
        the next run carries the next index. Starting again without saving
        repeats the index, which is how a spot is redone: the newer run of an
        index stands for the spot in the map.

        No position: the window has no map to take one from.
        """
        measured = (str(username or ''), str(sample_name or '').strip())
        if self._map_id is None or measured != self._minted_for:
            self._map_id = new_map_id(measured[1], now=self._clock())
            self._minted_for = measured
        index = spot_index(counter)
        spot = SpotRequest(map_id=self._map_id, index=index,
                           label=spot_label(label_text, index))
        return spot.model_dump()
