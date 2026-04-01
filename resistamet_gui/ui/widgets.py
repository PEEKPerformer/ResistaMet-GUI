"""
Custom widgets for instrument control UIs.

EngineeringSpinBox: Text input accepting engineering notation (e.g., "1mA", "100µA")
NoScrollSpinBox: QDoubleSpinBox that ignores scroll wheel unless focused
"""
import math
import re
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QValidator
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel, QDoubleSpinBox, QSpinBox

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

    valueChanged = pyqtSignal(float)

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
