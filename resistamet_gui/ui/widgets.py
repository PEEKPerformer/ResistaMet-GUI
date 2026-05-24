"""
Custom widgets for instrument control UIs.

EngineeringSpinBox: Text input accepting engineering notation (e.g., "1mA", "100µA")
NoScrollSpinBox: QDoubleSpinBox that ignores scroll wheel unless focused
"""
from __future__ import annotations

import math
import re
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize, QRect, QPoint
from PySide6.QtGui import QValidator, QPainter, QPen, QBrush, QColor, QFont, QFontMetrics
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel, QDoubleSpinBox, QSpinBox

# SI prefix table: prefix → multiplier (ordered large to small for display selection)
_SI_PREFIXES = [
    ('T', 1e12), ('G', 1e9), ('M', 1e6), ('k', 1e3),
    ('', 1.0),
    ('m', 1e-3), ('µ', 1e-6), ('n', 1e-9), ('p', 1e-12),
]

# For parsing input: accept both 'u' and 'µ' for micro
_PARSE_PREFIXES = {
    'T': 1e12, 'G': 1e9, 'M': 1e6, 'k': 1e3, 'K': 1e3,
    '': 1.0,
    'm': 1e-3, 'u': 1e-6, 'µ': 1e-6, '\u00b5': 1e-6,
    'n': 1e-9, 'p': 1e-12,
}

# Regex: optional sign, number (int or float or scientific), optional SI prefix, optional unit
_PARSE_RE = re.compile(
    r'^\s*'
    r'(?P<sign>[+-]?)\s*'
    r'(?P<number>\d+\.?\d*(?:[eE][+-]?\d+)?)'
    r'\s*'
    r'(?P<prefix>[TGMkKmuµnp]?)'
    r'\s*'
    r'(?P<unit>[A-Za-z/Ω□·]*)'
    r'\s*$'
)


def format_engineering(value: float, unit: str, precision: int = 4) -> str:
    """Format a value with the best SI prefix.

    Args:
        value: Value in base units (e.g., 0.001 for 1 mA)
        unit: Base unit string (e.g., 'A', 'V', 'Ω')
        precision: Significant digits to show

    Returns:
        Formatted string like "1.000 mA" or "-100.0 µA"
    """
    if not math.isfinite(value):
        return f"-- {unit}"

    abs_val = abs(value)
    if abs_val == 0:
        return f"0 {unit}"

    # Find the best prefix: largest prefix where the number is >= 1
    for prefix, multiplier in _SI_PREFIXES:
        if abs_val >= multiplier * 0.9999:
            scaled = value / multiplier
            # Determine decimal places based on magnitude
            if abs(scaled) >= 100:
                decimals = max(0, precision - 3)
            elif abs(scaled) >= 10:
                decimals = max(0, precision - 2)
            else:
                decimals = max(0, precision - 1)
            return f"{scaled:.{decimals}f} {prefix}{unit}"

    # Very small: use smallest prefix
    prefix, multiplier = _SI_PREFIXES[-1]
    scaled = value / multiplier
    return f"{scaled:.{precision-1}f} {prefix}{unit}"


def precision_for_nplc(nplc: float) -> int:
    """Return a sensible sig-fig count for the Keithley's resolution at NPLC.

    The 2400 SCPI runs at 6½-digit resolution at NPLC=1 (Speed = Normal),
    drops to 5½ at 0.1 PLC, and 4½ at 0.01 PLC (datasheet p. 8, System
    Speeds → Single Reading rates). Displaying more sig figs than the
    instrument's resolution implies precision we don't have; fewer
    throws away digits the user paid integration time for.

    Boundaries are continuous (matches accuracy._nplc_modifier's bucketing).
    """
    if not math.isfinite(nplc):
        return 4
    if nplc >= 0.5:
        return 6
    if nplc >= 0.05:
        return 5
    return 4


def format_with_uncertainty(
    value: float, uncertainty: float, unit: str, unc_sig_figs: int = 2
) -> str:
    """Format ``value ± uncertainty unit`` with a shared SI prefix.

    Rounds the uncertainty to ``unc_sig_figs`` significant figures (default
    2 per GUM §7.2.6) and the value to the same decimal place — so the
    number of digits in the value never implies more precision than the
    uncertainty justifies. Both share the engineering prefix picked from
    the value's magnitude, which keeps the pair readable at a glance.

    Falls back to plain ``format_engineering(value, unit)`` when the
    uncertainty isn't finite or is non-positive.

    Examples (Ω):
        value=1487,    σ=1.3   →  "1.4870 ± 0.0013 kΩ"
        value=0.0995,  σ=0.001 →  "99.50 ± 1.00 mΩ"
        value=2.5e-3,  σ=4e-5  →  "2.500 ± 0.040 mΩ"
    """
    if not math.isfinite(value):
        return f"-- {unit}"
    if not math.isfinite(uncertainty) or uncertainty <= 0:
        return format_engineering(value, unit)

    # Prefix chosen from the value (not the uncertainty) so the live
    # readout's "magnitude" stays anchored to what the user is measuring.
    abs_val = abs(value)
    chosen_prefix = ''
    chosen_mult = 1.0
    if abs_val > 0:
        for prefix, multiplier in _SI_PREFIXES:
            if abs_val >= multiplier * 0.9999:
                chosen_prefix = prefix
                chosen_mult = multiplier
                break
        else:
            chosen_prefix, chosen_mult = _SI_PREFIXES[-1]

    v_scaled = value / chosen_mult
    u_scaled = uncertainty / chosen_mult

    # Decimal place where the uncertainty's leading sig fig sits, in the
    # scaled units. Floor(log10) gives the exponent of the leading digit.
    order = math.floor(math.log10(u_scaled)) if u_scaled > 0 else 0
    round_decimals = max(0, -(order - (unc_sig_figs - 1)))

    return (f"{v_scaled:.{round_decimals}f} ± "
            f"{u_scaled:.{round_decimals}f} {chosen_prefix}{unit}")


def parse_engineering(text: str, unit: str = '') -> Optional[float]:
    """Parse engineering notation text to a float value in base units.

    Args:
        text: Input string like "1mA", "100µA", "0.5", "-10mV"
        unit: Expected base unit (used to strip from input, not required)

    Returns:
        Float value in base units, or None if unparseable
    """
    text = text.strip()
    if not text:
        return None

    match = _PARSE_RE.match(text)
    if not match:
        # Try as plain number
        try:
            return float(text)
        except ValueError:
            return None

    sign_str = match.group('sign')
    number_str = match.group('number')
    prefix_str = match.group('prefix')

    try:
        number = float(number_str)
    except ValueError:
        return None

    sign = -1.0 if sign_str == '-' else 1.0
    multiplier = _PARSE_PREFIXES.get(prefix_str, 1.0)

    return sign * number * multiplier


class EngineeringSpinBox(QWidget):
    """Text input that accepts engineering notation for physical quantities.

    Displays values like "1.000 mA" and accepts inputs like "1mA", "100µA",
    "0.001", or "-1.5mA". Drop-in replacement for QDoubleSpinBox.

    Usage:
        box = EngineeringSpinBox(unit='A', minimum=1e-7, maximum=3.0, default=1e-3)
        box.setValue(0.001)  # displays "1.000 mA"
        val = box.value()    # returns 0.001
    """

    valueChanged = Signal(float)

    def __init__(self, unit: str = 'A', minimum: float = 0, maximum: float = 1,
                 default: float = 0, allow_negative: bool = False, parent: QWidget = None):
        super().__init__(parent)
        self._unit = unit
        self._min = minimum
        self._max = maximum
        self._value = default
        self._allow_negative = allow_negative

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._line_edit = QLineEdit()
        self._line_edit.setAlignment(Qt.AlignRight)
        self._line_edit.setMinimumWidth(100)
        self._line_edit.setMaximumWidth(160)
        layout.addWidget(self._line_edit)

        self._display_value()

        self._line_edit.editingFinished.connect(self._on_editing_finished)

        # Disable scroll wheel
        self._line_edit.wheelEvent = lambda e: e.ignore()

    def value(self) -> float:
        """Return current value in base units."""
        return self._value

    def setValue(self, val: float) -> None:
        """Set value in base units."""
        val = self._clamp(val)
        if val != self._value:
            self._value = val
            self._display_value()
            self.valueChanged.emit(self._value)
        else:
            self._display_value()

    def setToolTip(self, text: str) -> None:
        """Forward tooltip to the internal line edit."""
        self._line_edit.setToolTip(text)
        super().setToolTip(text)

    def setEnabled(self, enabled: bool) -> None:
        self._line_edit.setEnabled(enabled)
        super().setEnabled(enabled)

    def setMaximumWidth(self, w: int) -> None:
        self._line_edit.setMaximumWidth(w)

    def _clamp(self, val: float) -> float:
        if not self._allow_negative and val < 0:
            val = max(self._min, val)
        return max(self._min, min(self._max, val))

    def _display_value(self):
        """Format and display current value with engineering prefix."""
        text = format_engineering(self._value, self._unit)
        self._line_edit.setText(text)
        self._line_edit.setStyleSheet("")

    def _on_editing_finished(self):
        """Parse user input and update value."""
        text = self._line_edit.text()
        parsed = parse_engineering(text, self._unit)

        if parsed is None:
            # Invalid input: flash red and revert
            self._line_edit.setStyleSheet("border: 2px solid red;")
            self._display_value()
            return

        # Range check
        clamped = self._clamp(parsed)
        if clamped != parsed:
            self._line_edit.setStyleSheet("border: 2px solid orange;")
        else:
            self._line_edit.setStyleSheet("")

        old = self._value
        self._value = clamped
        self._display_value()
        if self._value != old:
            self.valueChanged.emit(self._value)


class NoScrollSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that ignores scroll wheel events unless explicitly focused."""

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollIntSpinBox(QSpinBox):
    """QSpinBox that ignores scroll wheel events unless explicitly focused."""

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class VdpSampleDiagram(QWidget):
    """Schematic of a van der Pauw sample with the 4 Keithley leads attached.

    Shows a centered square representing the sample with the 4 contacts
    labeled 1-4 in counter-clockwise order starting from the top-left
    corner. Each of the 4 Keithley terminals (Force HI / Force LO /
    Sense HI / Sense LO) is rendered as a colored stub extending
    outward from whichever corner it connects to in the current
    measurement geometry:

        red, thick   = Force HI  (source current entry, top red banana)
        black, thick = Force LO  (source current exit, top black banana)
        red, thin    = Sense HI  (voltmeter +, bottom red banana)
        black, thin  = Sense LO  (voltmeter -, bottom black banana)

    Call ``set_configuration(geom)`` with a dict containing
    ``source_high``, ``source_low``, ``sense_high``, ``sense_low``
    (contact numbers 1-4) to update the rendering.
    """

    # Corner positions on the unit square, indexed by F76 contact number
    # (1-4). Counter-clockwise starting top-left so the labels increase
    # the way the F76 protocol counts them.
    _CORNER_UNIT_POS = {
        1: (0.0, 0.0),  # top-left
        2: (0.0, 1.0),  # bottom-left
        3: (1.0, 1.0),  # bottom-right
        4: (1.0, 0.0),  # top-right
    }

    # Lead style: (color, line width). Force = thick (current carrier);
    # Sense = thin (high-impedance voltmeter).
    _LEAD_STYLES = {
        "Force HI": (QColor("#c0392b"), 3),   # bright red
        "Force LO": (QColor("#1a1a1a"), 3),   # black
        "Sense HI": (QColor("#c0392b"), 1),   # bright red, thin
        "Sense LO": (QColor("#1a1a1a"), 1),   # black, thin
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        # No active configuration -> render an idle "waiting" diagram with
        # just the sample and corner labels.
        self._geom: dict | None = None

    def sizeHint(self) -> QSize:
        return QSize(260, 260)

    def set_configuration(self, geom: dict | None) -> None:
        """Update the rendered geometry. Pass None to clear back to idle."""
        self._geom = geom
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        # Hard clip to widget bounds so antialiased glyphs or any future
        # math bug can't leak labels onto adjacent widgets in the layout.
        painter.setClipRect(0, 0, w, h)

        # Geometry: leave a margin so lead stubs + labels fit outside the
        # sample square. Square is centered, side = min(w,h) * 0.42 so
        # the bottom labels have headroom against the widget bottom even
        # at the minimum 220x220 size.
        size = int(min(w, h) * 0.42)
        cx, cy = w // 2, h // 2
        x0 = cx - size // 2
        y0 = cy - size // 2
        x1 = x0 + size
        y1 = y0 + size
        sample_rect = QRect(x0, y0, size, size)

        # Sample background
        painter.fillRect(sample_rect, QColor("#fafafa"))
        painter.setPen(QPen(QColor("#666"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(sample_rect)

        # Corner positions on screen (pixel coords), indexed by contact number.
        corners = {
            1: QPoint(x0, y0),
            2: QPoint(x0, y1),
            3: QPoint(x1, y1),
            4: QPoint(x1, y0),
        }
        # Direction each corner's lead stub points (outward).
        outward = {
            1: (-1, -1),
            2: (-1, +1),
            3: (+1, +1),
            4: (+1, -1),
        }

        # Find which lead lives at which corner for the current geom.
        # If no geom, leave the lead -> corner map empty (idle diagram).
        corner_to_leads: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
        if self._geom is not None:
            corner_to_leads[self._geom["source_high"]].append("Force HI")
            corner_to_leads[self._geom["source_low"]].append("Force LO")
            corner_to_leads[self._geom["sense_high"]].append("Sense HI")
            corner_to_leads[self._geom["sense_low"]].append("Sense LO")

        # Draw lead stubs first (so dots draw on top). Stub length plus
        # label height must fit within ((widget_dim - sample) / 2) on each
        # side; 0.40 of sample keeps the labels comfortably inside the
        # widget bounds at minimum size.
        stub_len = int(size * 0.40)
        label_font = QFont()
        label_font.setPointSize(9)
        label_font.setBold(True)
        fm = QFontMetrics(label_font)
        painter.setFont(label_font)

        for corner_num, leads in corner_to_leads.items():
            if not leads:
                continue
            base = corners[corner_num]
            dx, dy = outward[corner_num]
            # If two leads share a corner (shouldn't happen in F76 but
            # be defensive), fan them out a little.
            n = len(leads)
            for i, lead in enumerate(leads):
                color, lw = self._LEAD_STYLES[lead]
                # Perpendicular offset for fan-out when n > 1; zero in
                # normal F76 configs (each terminal goes to one corner).
                offset = int(round((2 * i - (n - 1)) * 4))
                # Outward vector along the corner's diagonal; the offset
                # is applied perpendicular to it.
                end_x = int(base.x() + dx * stub_len + (-dy) * offset)
                end_y = int(base.y() + dy * stub_len + (dx) * offset)
                pen = QPen(color, lw)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawLine(base.x(), base.y(), end_x, end_y)

                # Label near the end of the stub.
                text = lead
                tw = fm.horizontalAdvance(text)
                th = fm.height()
                lx = end_x + (dx * 4) - tw // 2
                ly = end_y + (dy * 4) + (th // 2 if dy > 0 else 0)
                lx = int(max(2, min(w - tw - 2, lx)))
                ly = int(max(th + 2, min(h - 2, ly)))
                painter.setPen(QPen(color, 1))
                painter.drawText(lx, ly, text)

        # Draw corner dots and their numeric labels last.
        dot_radius = 6
        num_font = QFont()
        num_font.setPointSize(11)
        num_font.setBold(True)
        painter.setFont(num_font)
        for corner_num, pt in corners.items():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#222"))
            painter.drawEllipse(pt, dot_radius, dot_radius)
            # Number label INSIDE the sample square (offset toward center).
            dx, dy = outward[corner_num]
            num_x = pt.x() - dx * 16
            num_y = pt.y() - dy * 16
            painter.setPen(QPen(QColor("#222"), 1))
            painter.drawText(
                num_x - 6, num_y - 8, 16, 16,
                Qt.AlignCenter, str(corner_num),
            )

        # Idle state: print a hint in the center of the sample. Use a
        # padded rect + TextWordWrap so the message reflows cleanly when
        # the widget (and therefore the sample square) is narrow.
        if self._geom is None:
            painter.setPen(QPen(QColor("#888"), 1))
            hint_font = QFont()
            hint_font.setPointSize(8)
            hint_font.setItalic(True)
            painter.setFont(hint_font)
            hint_rect = sample_rect.adjusted(6, 6, -6, -6)
            painter.drawText(
                hint_rect,
                Qt.AlignCenter | Qt.TextWordWrap,
                "Press Start\nto see lead routing",
            )
        painter.end()


class VdpProtocolFilmstrip(QWidget):
    """Progress filmstrip of all 4 F76 vdP geometries.

    Reads left-to-right: G1, G2, G3, G4. Each cell renders a mini
    schematic of that geometry's lead routing. Status states:

        current:   blue border + blue title bar (with a '*' marker)
        completed: green border + green title bar (with 'OK' marker)
        pending:   light-gray border + gray title bar

    The visual relationship between consecutive cells makes the
    one-corner counter-clockwise rotation pattern self-evident, so the
    user can see at a glance what's coming next without rewiring in
    their head.
    """

    _CORNER_UNIT_POS = {1: (-1, -1), 2: (-1, 1), 3: (1, 1), 4: (1, -1)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(440, 110)
        self._current = -1
        self._completed: set[int] = set()
        self._geometries: list = []

    def sizeHint(self) -> QSize:
        return QSize(520, 120)

    def set_current(self, idx: int) -> None:
        self._current = idx
        self.update()

    def mark_completed(self, idx: int) -> None:
        self._completed.add(idx)
        self.update()

    def reset(self) -> None:
        self._current = -1
        self._completed.clear()
        self.update()

    def paintEvent(self, event) -> None:
        # Lazy-load the geometry list once.
        if not self._geometries:
            from ..calculations_vdp import f76_geometries
            self._geometries = f76_geometries()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        painter.setClipRect(0, 0, w, h)
        n = len(self._geometries)
        cell_w = w // n
        for i, geom in enumerate(self._geometries):
            cell_rect = QRect(i * cell_w, 0, cell_w, h)
            self._paint_cell(painter, cell_rect, geom, i)
        painter.end()

    def _paint_cell(self, painter: QPainter, cell_rect: QRect,
                     geom, idx: int) -> None:
        is_current = (idx == self._current)
        is_done = (idx in self._completed)
        if is_current:
            border = QColor("#1976d2"); border_w = 3
            title_bg = QColor("#e3f2fd"); marker = "*"
        elif is_done:
            border = QColor("#388e3c"); border_w = 2
            title_bg = QColor("#e8f5e9"); marker = "OK"
        else:
            border = QColor("#bdbdbd"); border_w = 1
            title_bg = QColor("#f5f5f5"); marker = ""

        pad = 4
        rect = cell_rect.adjusted(pad, pad, -pad, -pad)
        title_h = 18
        title_rect = QRect(rect.x(), rect.y(), rect.width(), title_h)
        painter.fillRect(title_rect, title_bg)
        painter.setPen(QPen(border, border_w))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)

        title_font = QFont(); title_font.setPointSize(8); title_font.setBold(is_current)
        painter.setFont(title_font)
        painter.setPen(QColor("#222"))
        title_text = f"{marker} {geom.name}".strip()
        painter.drawText(title_rect, Qt.AlignCenter, title_text)

        body = QRect(rect.x(), rect.y() + title_h,
                     rect.width(), rect.height() - title_h)
        side = int(min(body.width(), body.height()) * 0.50)
        cx = body.x() + body.width() // 2
        cy = body.y() + body.height() // 2
        sx = cx - side // 2
        sy = cy - side // 2

        # Sample
        painter.setPen(QPen(QColor("#888"), 1))
        painter.setBrush(QColor("#fafafa"))
        painter.drawRect(sx, sy, side, side)

        corners = {
            1: QPoint(sx, sy),
            2: QPoint(sx, sy + side),
            3: QPoint(sx + side, sy + side),
            4: QPoint(sx + side, sy),
        }
        stub_len = int(side * 0.40)
        lead_map = {
            geom.source_high: (QColor("#c0392b"), 2),  # Force HI: red thick
            geom.source_low:  (QColor("#1a1a1a"), 2),  # Force LO: black thick
            geom.sense_high:  (QColor("#c0392b"), 1),  # Sense HI: red thin
            geom.sense_low:   (QColor("#1a1a1a"), 1),  # Sense LO: black thin
        }
        for corner_num, (color, lw) in lead_map.items():
            base = corners[corner_num]
            dx, dy = self._CORNER_UNIT_POS[corner_num]
            end_x = int(base.x() + dx * stub_len)
            end_y = int(base.y() + dy * stub_len)
            pen = QPen(color, lw); pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(base.x(), base.y(), end_x, end_y)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#222"))
        for pt in corners.values():
            painter.drawEllipse(pt, 3, 3)


class VdpPerGeometryBarChart(QWidget):
    """4-bar comparison of per-geometry resistance for a vdP measurement.

    On a uniform isotropic sample the 4 R values (one per F76 geometry)
    should be near-identical. This widget draws them as bars with a
    dashed mean line, so the user can see the homogeneity at a glance.
    Bar color encodes deviation from the mean:

        green:  |R_i - mean| / mean < 3 %     (clearly uniform)
        orange: |R_i - mean| / mean < 10 %    (within F76 gate but watch)
        red:    |R_i - mean| / mean >= 10 %   (outlier; sample probably bad)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 110)
        self._values: list[float] = []
        self._labels: list[str] = []

    def sizeHint(self) -> QSize:
        return QSize(420, 130)

    def set_data(self, values, labels=None) -> None:
        self._values = list(values)
        self._labels = list(labels) if labels else [f"G{i + 1}" for i in range(len(values))]
        self.update()

    def clear(self) -> None:
        self._values = []
        self._labels = []
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        painter.setClipRect(0, 0, w, h)

        if not self._values:
            painter.setPen(QColor("#888"))
            f = QFont(); f.setPointSize(9); f.setItalic(True)
            painter.setFont(f)
            painter.drawText(QRect(0, 0, w, h), Qt.AlignCenter,
                             "Per-geometry R chart will appear after measurement")
            painter.end()
            return

        n = len(self._values)
        margin_l = 40
        margin_r = 12
        margin_top = 12
        margin_bottom = 22
        plot_w = w - margin_l - margin_r
        plot_h = h - margin_top - margin_bottom

        vmin = min(self._values)
        vmax = max(self._values)
        mean = sum(self._values) / n
        # If readings are tight (uniform sample), still want visible bars.
        # Pad the y range to 30 % above and below the data span, with a
        # floor tied to 5 % of the mean for the "perfectly uniform" case.
        span = max(vmax - vmin, abs(mean) * 0.05)
        y_lo = vmin - span * 0.3
        y_hi = vmax + span * 0.3
        if y_hi == y_lo:
            y_hi = y_lo + 1.0

        baseline_y = margin_top + plot_h
        painter.setPen(QPen(QColor("#666"), 1))
        painter.drawLine(margin_l, baseline_y, margin_l + plot_w, baseline_y)

        bar_gap = plot_w / n
        bar_w = int(bar_gap * 0.55)

        for i, val in enumerate(self._values):
            ratio = (val - y_lo) / (y_hi - y_lo)
            bar_h = int(ratio * plot_h)
            bar_x = int(margin_l + bar_gap * (i + 0.5) - bar_w / 2)
            bar_y = baseline_y - bar_h

            dev_pct = abs(val - mean) / abs(mean) * 100 if mean != 0 else 0
            if dev_pct < 3.0:
                bar_color = QColor("#43a047")  # green
            elif dev_pct < 10.0:
                bar_color = QColor("#fb8c00")  # orange
            else:
                bar_color = QColor("#e53935")  # red
            painter.setBrush(bar_color)
            painter.setPen(Qt.NoPen)
            painter.drawRect(bar_x, bar_y, bar_w, bar_h)

            # Geometry label below bar
            lf = QFont(); lf.setPointSize(9); lf.setBold(True)
            painter.setFont(lf)
            painter.setPen(QColor("#222"))
            label = self._labels[i] if i < len(self._labels) else f"G{i + 1}"
            painter.drawText(
                int(margin_l + bar_gap * (i + 0.5) - 20),
                baseline_y + 4,
                40, 18, Qt.AlignCenter, label,
            )

        # Mean line + label
        mean_ratio = (mean - y_lo) / (y_hi - y_lo)
        mean_y = int(baseline_y - mean_ratio * plot_h)
        pen = QPen(QColor("#1565c0"), 1)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(margin_l, mean_y, margin_l + plot_w, mean_y)
        mf = QFont(); mf.setPointSize(8); mf.setItalic(True)
        painter.setFont(mf)
        painter.setPen(QColor("#1565c0"))
        painter.drawText(margin_l + 4, max(mean_y - 4, 12), "mean")

        # Tiny axis label on the left edge: "R" with units
        painter.setPen(QColor("#666"))
        af = QFont(); af.setPointSize(8)
        painter.setFont(af)
        painter.drawText(2, margin_top + plot_h // 2, "R (Ω)")

        painter.end()
