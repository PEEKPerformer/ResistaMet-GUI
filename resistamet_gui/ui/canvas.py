import numpy as np
import pyqtgraph as pg
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

# pyqtgraph global config — set once, applies to every PlotWidget created
# afterwards. Cool off-white plot background (gentler than pure white at
# projector brightness) and a soft-black foreground tuned for a Nature-
# materials-paper aesthetic; antialiasing keeps the live trace crisp.
pg.setConfigOptions(antialias=True, background=(250, 250, 252),
                    foreground=(26, 26, 26), useOpenGL=False)


# Aesthetic constants shared with the live canvas — same palette + bg so
# the 4PP tab reads as a single instrument rather than two separate plots.
_HIST_BG       = '#fafafc'
_HIST_BAR      = '#2c5f8f'   # deep blue, matches PgLiveCanvas 'blue'
_HIST_MEAN     = '#c0392b'   # deep red, matches PgLiveCanvas 'red'
_HIST_AXIS     = '#666666'
_HIST_GRID     = '#e6e6e6'
_HIST_BBOX     = dict(boxstyle='round,pad=0.4', fc='white', ec='#cccccc', alpha=0.95)


def _style_histogram_axes(ax):
    """Apply the Nature-figure scaffolding used by both histogram + bar
    modes: medium-grey spines, no top/right frame, light dotted grid,
    sans-serif tick labels at presentation-readable size."""
    fig = ax.figure
    fig.patch.set_facecolor(_HIST_BG)
    ax.set_facecolor(_HIST_BG)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(_HIST_AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(axis='both', colors=_HIST_AXIS, labelsize=9, length=4, width=0.8)
    ax.xaxis.label.set_color('#333333'); ax.xaxis.label.set_size(10)
    ax.yaxis.label.set_color('#333333'); ax.yaxis.label.set_size(10)
    ax.title.set_size(12); ax.title.set_color('#222222'); ax.title.set_weight('bold')
    ax.grid(True, linestyle=':', linewidth=0.6, color=_HIST_GRID, zorder=0)
    ax.set_axisbelow(True)


class HistogramCanvas(FigureCanvas):
    """Histogram display for 4-Point Probe measurement distribution.

    Supports two modes:
    - Histogram: distribution of a single variable (Rs, rho, etc.)
    - Bar chart: spot-to-spot comparison with error bars
    """

    def __init__(self, parent=None, width=5, height=3, dpi=90):
        self.fig = Figure(figsize=(width, height), dpi=dpi, constrained_layout=True,
                          facecolor=_HIST_BG)
        self.axes = self.fig.add_subplot(111)
        super().__init__(self.fig)
        if parent is not None:
            try:
                self.setParent(parent)
            except Exception:
                pass
        self._mode = 'histogram'  # 'histogram' or 'bar_chart'
        self.clear_histogram()

    def update_histogram(self, values, label='Rs (Ω/□)', bins='auto'):
        """Draw histogram of values with stats annotations."""
        self.axes.clear()
        self._mode = 'histogram'

        valid = [v for v in values if np.isfinite(v)]
        if not valid:
            _style_histogram_axes(self.axes)
            self.axes.text(0.5, 0.5, 'No data', transform=self.axes.transAxes,
                           ha='center', va='center', fontsize=12, color='#888888')
            self.axes.set_xlabel(label)
            self.axes.set_ylabel('Count')
            self.axes.set_title('Distribution')
            self.draw_idle()
            return

        n = len(valid)
        mean = np.mean(valid)
        std = np.std(valid, ddof=1) if n > 1 else 0.0
        rsd = (std / mean * 100) if mean != 0 else 0.0

        # Adaptive bin count
        if bins == 'auto':
            bins = max(5, min(30, n // 3))

        self.axes.hist(valid, bins=bins, color=_HIST_BAR, edgecolor='white',
                       linewidth=0.6, alpha=0.92, zorder=2)
        # Mean line
        self.axes.axvline(mean, color=_HIST_MEAN, linewidth=1.8, linestyle='--',
                          label=f'Mean: {mean:.5g}', zorder=3)
        # Stats annotation — top-right, card-styled to match the live canvas pill
        stats_text = f'N = {n}\nMean = {mean:.5g}\nStd = {std:.3g}\nRSD = {rsd:.2f}%'
        self.axes.text(0.97, 0.95, stats_text, transform=self.axes.transAxes,
                       ha='right', va='top', fontsize=9, family='monospace',
                       bbox=_HIST_BBOX, zorder=4)

        self.axes.set_xlabel(label)
        self.axes.set_ylabel('Count')
        self.axes.set_title('Measurement Distribution')
        self.axes.legend(loc='upper left', fontsize=8, frameon=False)
        _style_histogram_axes(self.axes)
        self.draw_idle()

    def update_bar_chart(self, spot_names, means, stds):
        """Draw bar chart comparing Rs across spots with error bars."""
        self.axes.clear()
        self._mode = 'bar_chart'

        if not spot_names:
            self.clear_histogram()
            return

        x = np.arange(len(spot_names))
        bars = self.axes.bar(x, means, yerr=stds, capsize=4,
                             color=_HIST_BAR, edgecolor='white', linewidth=0.6,
                             alpha=0.92,
                             error_kw=dict(elinewidth=1.4, capthick=1.4,
                                           ecolor='#3a3a3a'))

        self.axes.set_xticks(x)
        self.axes.set_xticklabels(spot_names, rotation=30, ha='right', fontsize=9)
        self.axes.set_ylabel('Rs (Ω/□)')
        self.axes.set_title('Spot-to-Spot Uniformity')

        # Auto-zoom y-axis around the data range so small variation is visible
        finite = [m for m in means if np.isfinite(m)]
        if finite:
            lo = min(m - s for m, s in zip(means, stds) if np.isfinite(m))
            hi = max(m + s for m, s in zip(means, stds) if np.isfinite(m))
            span = hi - lo if hi > lo else max(abs(hi), 1.0) * 0.1
            self.axes.set_ylim(lo - span * 0.15, hi + span * 0.25)

        # Annotate overall stats
        if len(means) > 1:
            overall_mean = np.mean(means)
            overall_std = np.std(means, ddof=1)
            rsd = (overall_std / overall_mean * 100) if overall_mean != 0 else 0
            self.axes.axhline(overall_mean, color=_HIST_MEAN, linewidth=1.2,
                              linestyle='--', alpha=0.75)
            self.axes.text(0.02, 0.97, f'Inter-spot RSD: {rsd:.1f}%',
                           transform=self.axes.transAxes, ha='left', va='top', fontsize=9,
                           family='monospace', bbox=_HIST_BBOX)

        _style_histogram_axes(self.axes)
        self.draw_idle()

    def clear_histogram(self):
        """Reset to blank state."""
        self.axes.clear()
        _style_histogram_axes(self.axes)
        self.axes.text(0.5, 0.5, 'Waiting for data...', transform=self.axes.transAxes,
                       ha='center', va='center', fontsize=11, color='#888888')
        self.axes.set_xlabel('')
        self.axes.set_ylabel('')
        self.draw_idle()


# Refined palette: deeper, slightly desaturated versions of the matplotlib
# tab10 defaults that read better at projector brightness and look closer
# to a materials-journal figure than the bright primaries we used to ship.
_COLOR_MAP = {
    'red':    '#c0392b',
    'blue':   '#2c5f8f',
    'green':  '#27ae60',
    'orange': '#d97706',
    'purple': '#6d28d9',
    'black':  '#1a1a1a',
}


def _resolve_color(color):
    if isinstance(color, str):
        return _COLOR_MAP.get(color.lower(), color)
    return color


def _vertical_separator() -> QFrame:
    """A thin vertical rule used between stats labels (Min | Max | Avg)."""
    sep = QFrame()
    sep.setFrameShape(QFrame.VLine)
    sep.setFrameShadow(QFrame.Sunken)
    sep.setStyleSheet("color: #c0c0c0;")
    return sep


# Short variable symbol per y-axis label, used to title the live value pill.
# Order matters: more specific labels first (e.g. "Sheet Resistance" before
# "Resistance") so the longest match wins.
_YLABEL_TO_SYMBOL = (
    ('sheet resistance', 'Rs'),
    ('resistivity',      'ρ'),
    ('conductivity',     'σ'),
    ('resistance',       'R'),
    ('current',          'I'),
    ('voltage',          'V'),
    ('v/i',              'R'),
)


def _short_symbol_for_ylabel(ylabel: str) -> str:
    """Map the y-axis label to the short symbol used in the live value pill.

    Returns '' when nothing matches — the pill then drops the prefix and
    just shows the number, which is still readable.
    """
    if not ylabel:
        return ''
    low = ylabel.lower()
    for needle, symbol in _YLABEL_TO_SYMBOL:
        if needle in low:
            return symbol
    return ''


def _format_value_pill(symbol: str, value: float, unit: str) -> str:
    """Render '<symbol> = <value> <unit>' for the live value pill.

    Uses '.4g' so 102.3, 1.234e-7 and 1.235e+8 all render readably. Drops
    the symbol when we couldn't infer one, drops the unit when the y-axis
    label didn't carry parentheses.
    """
    if not np.isfinite(value):
        return ''
    body = f'{value:.4g}'
    if unit:
        body = f'{body} {unit}'
    if symbol:
        return f' {symbol} = {body} '
    return f' {body} '


class PgLiveCanvas(QWidget):
    """Live-streaming time-series canvas backed by pyqtgraph.

    Drives every time-series tab (resistance / source_v / source_i /
    Results Viewer). matplotlib redraws cost tens of milliseconds per
    frame and visibly stutter at >5 Hz sampling; pyqtgraph stays under a
    couple of ms even at 20–50 Hz, which is what a research demo actually
    wants to look like.

    Public API:
        ``set_plot_properties(xlabel, ylabel, title, color)``
        ``update_plot(timestamps, values, compliance_list, stats,
                       username, sample_name)``
        ``clear_plot()``
    """

    def __init__(self, parent=None, width=8, height=5, dpi=100):
        super().__init__(parent)
        # Match the rough on-screen footprint of the matplotlib canvas so
        # tab layouts don't visibly shift.
        self.setMinimumSize(int(width * dpi * 0.6), int(height * dpi * 0.6))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        # Compliance banner — hidden when no compliance event.
        self.compliance_label = QLabel('')
        self.compliance_label.setAlignment(Qt.AlignCenter)
        self.compliance_label.setStyleSheet(
            "background-color: #ffe5e5; color: #b00020; border: 1px solid #b00020;"
            " border-radius: 3px; padding: 2px; font-weight: bold;"
        )
        self.compliance_label.setVisible(False)
        outer.addWidget(self.compliance_label)

        # The plot itself.
        self.plot_widget = pg.PlotWidget()
        # Light dotted grid — present enough to read off values, restrained
        # enough that it doesn't compete with the trace.
        self.plot_widget.showGrid(x=True, y=True, alpha=0.15)
        self.plot_widget.setLabel('bottom', 'Time (s)')
        self.plot_widget.setLabel('left', 'Value')
        # Auto-range follows new points as they stream in — this is the
        # behavior matplotlib gave us with relim()+autoscale_view(). The
        # extra padding (default is 0.02) gives sudden transitions room
        # to breathe: when a foam-composite press makes resistance drop
        # 10x in 200 ms, the rescaled view shows the jump in context
        # instead of pancaking the pre-jump baseline against the axis.
        self.plot_widget.enableAutoRange(axis='xy', enable=True)
        self.plot_widget.getViewBox().setDefaultPadding(0.08)
        # SI prefix on y axis (k, M, m, µ, n) — matches the way the rest of
        # the lab thinks about resistance/current.
        self.plot_widget.getAxis('left').enableAutoSIPrefix(True)
        # Long runs (hour-plus at 20 Hz = 70k+ samples) keep their fidelity
        # while staying interactively smooth: 'peak' downsampling preserves
        # transient spikes that 'mean' would erase, and clipToView avoids
        # rendering samples outside the visible range during pan/zoom.
        self.plot_widget.setDownsampling(auto=True, mode='peak')
        self.plot_widget.setClipToView(True)

        # Restrained scaffolding: medium-grey axes + tick labels in a clean
        # sans-serif. Looks closer to a published figure than pyqtgraph's
        # default heavy-black axes.
        axis_pen = pg.mkPen(color=(102, 102, 102), width=1)
        tick_font = QFont()
        tick_font.setStyleHint(QFont.SansSerif)
        tick_font.setPointSize(10)
        label_font = QFont()
        label_font.setStyleHint(QFont.SansSerif)
        label_font.setPointSize(11)
        for axis_name in ('bottom', 'left'):
            ax = self.plot_widget.getAxis(axis_name)
            ax.setPen(axis_pen)
            ax.setTextPen(pg.mkPen(color=(60, 60, 60)))
            ax.setStyle(tickFont=tick_font)
            ax.label.setFont(label_font)
        outer.addWidget(self.plot_widget, 1)

        # Hairline separator between the plot and the stats row. Tiny visual
        # break so the numbers don't blend into the axis.
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #e0e0e0;")
        sep.setMaximumHeight(1)
        outer.addWidget(sep)

        # Stats row below the plot. Kept compact and monospaced so the
        # numbers don't jitter as digits change. A vertical separator
        # between each stat gives the eye a clear visual break so the
        # three values don't read as one long ribbon when the numbers
        # have similar widths.
        stat_font = QFont('Monospace')
        stat_font.setStyleHint(QFont.TypeWriter)
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(8, 0, 8, 0)
        stats_row.setSpacing(16)
        self.min_label = QLabel('Min: --')
        self.max_label = QLabel('Max: --')
        self.avg_label = QLabel('Avg: --')
        self.info_label = QLabel('User: --   Sample: --')
        for w in (self.min_label, self.max_label, self.avg_label, self.info_label):
            w.setFont(stat_font)
        stats_row.addWidget(self.min_label)
        stats_row.addWidget(_vertical_separator())
        stats_row.addWidget(self.max_label)
        stats_row.addWidget(_vertical_separator())
        stats_row.addWidget(self.avg_label)
        stats_row.addStretch(1)
        stats_row.addWidget(self.info_label)
        outer.addLayout(stats_row)

        # Curve — created once and updated in place via setData(). This is
        # the whole performance story: no axes.clear()/replot() per frame.
        # cosmetic=True locks the line width to screen pixels (it stays
        # crisp under zoom instead of scaling with the data range).
        self._pen_color = _resolve_color('red')
        self.curve = self.plot_widget.plot(
            [], [], pen=pg.mkPen(self._pen_color, width=2.5, cosmetic=True),
            name='Measurement',
        )
        self._title = 'Measurement'
        self._y_label = 'Value'
        self._y_unit = ''
        self._y_symbol = ''  # e.g. 'R', 'V', 'I' — derived from the title
        # Bold sans-serif title at presentation size — readable from across
        # the room when projected.
        self.plot_widget.setTitle(
            self._title, color=(40, 40, 40), size='13pt', bold=True,
        )

        # Big latest-value pill, anchored to the top-right corner of the
        # viewbox so the audience can read the live number from across the
        # room. ignoreBounds keeps it from disturbing the auto-range, and
        # the sigRangeChanged callback re-pins it whenever the view moves.
        # Card-like styling: near-opaque white with a thin grey border —
        # reads as a HUD badge rather than a sticky-note overlay.
        self._value_font = QFont()
        self._value_font.setStyleHint(QFont.TypeWriter)
        self._value_font.setFamily('Menlo')  # falls back to system mono
        self._value_font.setPointSize(18)
        self._value_font.setBold(True)
        self.value_label = pg.TextItem(
            text='', color=(26, 26, 26), anchor=(1.0, 0.0),
            fill=(255, 255, 255, 235),
            border={'color': (200, 200, 200), 'width': 1},
        )
        self.value_label.setFont(self._value_font)
        self.plot_widget.addItem(self.value_label, ignoreBounds=True)
        self._viewbox = self.plot_widget.getViewBox()
        self._viewbox.sigRangeChanged.connect(self._reposition_value_label)
        self._reposition_value_label()

    # --- public API ------------------------------------------------------

    def set_plot_properties(self, xlabel, ylabel, title, color='blue'):
        self._pen_color = _resolve_color(color)
        self.curve.setPen(pg.mkPen(self._pen_color, width=2))
        self.plot_widget.setLabel('bottom', xlabel)
        self.plot_widget.setLabel('left', ylabel)
        self._title = title
        self._y_label = ylabel
        # Extract unit from "Foo (Ω)" → "Ω" so stats labels can carry it.
        if '(' in ylabel and ')' in ylabel:
            self._y_unit = ylabel.split('(', 1)[1].split(')', 1)[0]
        else:
            self._y_unit = ''
        self._y_symbol = _short_symbol_for_ylabel(ylabel)
        self.plot_widget.setTitle(title)

    def update_plot(self, timestamps, values, compliance_list, stats, username, sample_name):
        if not timestamps:
            self.clear_plot()
            return
        start_time = timestamps[0]
        # numpy conversion keeps the masking + setData fast even at large
        # buffer sizes; lists would force per-element Python iteration in
        # the C extension.
        et = np.asarray([t - start_time for t in timestamps], dtype=float)
        vals = np.asarray(values, dtype=float)
        mask = np.isfinite(vals)
        if mask.any():
            self.curve.setData(et[mask], vals[mask])
            latest = float(vals[mask][-1])
            self.value_label.setText(_format_value_pill(self._y_symbol, latest, self._y_unit))
        else:
            self.curve.setData([], [])
            self.value_label.setText('')

        unit = self._y_unit
        min_v = stats.get('min', float('inf'))
        max_v = stats.get('max', float('-inf'))
        avg_v = stats.get('avg', 0)
        self.min_label.setText(f'Min: {min_v:.4g} {unit}' if np.isfinite(min_v) else 'Min: --')
        self.max_label.setText(f'Max: {max_v:.4g} {unit}' if np.isfinite(max_v) else 'Max: --')
        self.avg_label.setText(f'Avg: {avg_v:.4g} {unit}' if np.isfinite(avg_v) else 'Avg: --')
        self.info_label.setText(f'User: {username}   Sample: {sample_name}')

        last_compliance = compliance_list[-1] if compliance_list else 'OK'
        if last_compliance == 'V_COMP':
            self.compliance_label.setText('VOLTAGE COMPLIANCE HIT')
            self.compliance_label.setVisible(True)
        elif last_compliance == 'I_COMP':
            self.compliance_label.setText('CURRENT COMPLIANCE HIT')
            self.compliance_label.setVisible(True)
        else:
            self.compliance_label.setVisible(False)

    def clear_plot(self):
        self.curve.setData([], [])
        self.value_label.setText('')
        self.min_label.setText('Min: --')
        self.max_label.setText('Max: --')
        self.avg_label.setText('Avg: --')
        self.info_label.setText('User: --   Sample: --')
        self.compliance_label.setVisible(False)

    # --- internals -------------------------------------------------------

    def _reposition_value_label(self, *_args, **_kwargs):
        """Pin the value pill to the top-right of the current view range.

        Called on every viewbox range change (auto-range tick, user pan/
        zoom, programmatic setRange) so the pill stays glued to the corner
        regardless of axis scaling.
        """
        try:
            (x0, x1), (y0, y1) = self._viewbox.viewRange()
            # Inset a few percent so the pill doesn't kiss the right axis.
            x_pad = (x1 - x0) * 0.02
            y_pad = (y1 - y0) * 0.02
            self.value_label.setPos(x1 - x_pad, y1 - y_pad)
        except Exception:
            # During very-early init the viewbox may not have a range yet;
            # the next sigRangeChanged will give us a real one.
            pass


class IVCanvas(FigureCanvas):
    """X-Y plot for I-V sweep data (not time-series)."""

    def __init__(self, parent=None, width=8, height=5, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, constrained_layout=True)
        self.axes = self.fig.add_subplot(111)
        super().__init__(self.fig)
        if parent is not None:
            try:
                self.setParent(parent)
            except Exception:
                pass
        self.axes.set_xlabel('Voltage (V)')
        self.axes.set_ylabel('Current (A)')
        self.axes.set_title('I-V Characteristic')
        self.axes.grid(True)
        self._lines = []

    def plot_sweep(self, voltages, currents, label='Forward', color='blue'):
        """Plot one sweep trace."""
        line, = self.axes.plot(voltages, currents, '-o', markersize=3,
                               color=color, label=label, linewidth=1.5)
        self._lines.append(line)
        self.axes.legend(loc='best', fontsize=8)
        self.axes.relim()
        self.axes.autoscale_view(True, True, True)
        self.draw_idle()

    def set_labels(self, xlabel, ylabel, title):
        self.axes.set_xlabel(xlabel)
        self.axes.set_ylabel(ylabel)
        self.axes.set_title(title)
        self.draw_idle()

    def clear_plot(self):
        for line in self._lines:
            line.remove()
        self._lines.clear()
        self.axes.clear()
        self.axes.set_xlabel('Voltage (V)')
        self.axes.set_ylabel('Current (A)')
        self.axes.set_title('I-V Characteristic')
        self.axes.grid(True)
        self.draw_idle()
