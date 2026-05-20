import numpy as np
import pyqtgraph as pg
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

# pyqtgraph global config — set once, applies to every PlotWidget created
# afterwards. White background + black foreground matches the matplotlib
# look the rest of the app uses; antialiasing makes the live trace render
# cleanly at speed.
pg.setConfigOptions(antialias=True, background='w', foreground='k', useOpenGL=False)


class HistogramCanvas(FigureCanvas):
    """Histogram display for 4-Point Probe measurement distribution.

    Supports two modes:
    - Histogram: distribution of a single variable (Rs, rho, etc.)
    - Bar chart: spot-to-spot comparison with error bars
    """

    def __init__(self, parent=None, width=5, height=3, dpi=90):
        self.fig = Figure(figsize=(width, height), dpi=dpi, constrained_layout=True)
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
            self.axes.text(0.5, 0.5, 'No data', transform=self.axes.transAxes,
                           ha='center', va='center', fontsize=12, color='grey')
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

        self.axes.hist(valid, bins=bins, color='steelblue', edgecolor='white',
                       alpha=0.85, zorder=2)
        # Mean line
        self.axes.axvline(mean, color='red', linewidth=1.5, linestyle='--',
                          label=f'Mean: {mean:.5g}', zorder=3)
        # Stats annotation
        stats_text = f'N = {n}\nMean = {mean:.5g}\nStd = {std:.3g}\nRSD = {rsd:.2f}%'
        self.axes.text(0.97, 0.95, stats_text, transform=self.axes.transAxes,
                       ha='right', va='top', fontsize=8,
                       bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='grey', alpha=0.8))

        self.axes.set_xlabel(label)
        self.axes.set_ylabel('Count')
        self.axes.set_title('Measurement Distribution')
        self.axes.legend(loc='upper left', fontsize=8)
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
                             color='steelblue', edgecolor='white', alpha=0.85,
                             error_kw=dict(elinewidth=1.5, capthick=1.5))

        self.axes.set_xticks(x)
        self.axes.set_xticklabels(spot_names, rotation=30, ha='right', fontsize=8)
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
            self.axes.axhline(overall_mean, color='red', linewidth=1, linestyle='--', alpha=0.7)
            self.axes.text(0.02, 0.97, f'Inter-spot RSD: {rsd:.1f}%',
                           transform=self.axes.transAxes, ha='left', va='top', fontsize=9,
                           bbox=dict(boxstyle='round,pad=0.3', fc='white', ec='grey', alpha=0.8))

        self.draw_idle()

    def clear_histogram(self):
        """Reset to blank state."""
        self.axes.clear()
        self.axes.text(0.5, 0.5, 'Waiting for data...', transform=self.axes.transAxes,
                       ha='center', va='center', fontsize=11, color='grey')
        self.axes.set_xlabel('')
        self.axes.set_ylabel('')
        self.draw_idle()


class MplCanvas(FigureCanvas):
    def __init__(self, parent=None, width=8, height=5, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, constrained_layout=True)
        self.axes = self.fig.add_subplot(111)
        # Use scientific notation for extreme magnitudes; plain in the middle range
        self.axes.ticklabel_format(useOffset=False, style='sci', scilimits=(-3, 4))
        super().__init__(self.fig)
        # Set Qt parent properly and avoid shadowing QWidget.parent()
        if parent is not None:
            try:
                self.setParent(parent)
            except Exception:
                pass

        self.line, = self.axes.plot([], [], 'r-', label='Measurement')
        # Stats stack: upper-left, inside axes
        self.min_text = self.axes.text(0.02, 0.97, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.max_text = self.axes.text(0.02, 0.90, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.avg_text = self.axes.text(0.02, 0.83, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        # User/sample: bottom-right inside axes, out of legend's way
        self.info_text = self.axes.text(0.98, 0.03, '', transform=self.axes.transAxes, ha='right', va='bottom', fontsize=8)
        # Compliance banner: top-center inside axes, below the title
        self.compliance_indicator = self.axes.text(
            0.5, 0.97, '', transform=self.axes.transAxes, ha='center', va='top',
            fontsize=10, color='red', weight='bold',
        )

        bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.7)
        for t in (self.min_text, self.max_text, self.avg_text, self.info_text):
            t.set_bbox(bbox_props)

        self.axes.legend(loc='upper right')
        self.set_plot_properties('Time (s)', 'Value', 'Measurement')

    def set_plot_properties(self, xlabel, ylabel, title, color='blue'):
        self.axes.set_xlabel(xlabel)
        self.axes.set_ylabel(ylabel)
        self.axes.set_title(title)
        self.line.set_label(title)
        self.line.set_color(color)
        self.axes.legend(loc='upper right')
        self.axes.grid(True)
        self.draw_idle()

    def update_plot(self, timestamps, values, compliance_list, stats, username, sample_name):
        if not timestamps:
            self.clear_plot()
            return
        start_time = timestamps[0]
        elapsed_times = [t - start_time for t in timestamps]
        valid_indices = [i for i, v in enumerate(values) if np.isfinite(v)]
        if not valid_indices:
            self.line.set_data([], [])
        else:
            plot_times = [elapsed_times[i] for i in valid_indices]
            plot_values = [values[i] for i in valid_indices]
            self.line.set_data(plot_times, plot_values)
        self.axes.relim()
        self.axes.autoscale_view(True, True, True)
        unit = self.axes.get_ylabel()
        unit = unit.split('(')[-1].split(')')[0] if '(' in unit else ''
        min_val = stats.get('min', float('inf'))
        max_val = stats.get('max', float('-inf'))
        avg_val = stats.get('avg', 0)
        # .4g adapts to magnitude: 102.3, 1.234e-7, 1.235e+8 all render readably
        self.min_text.set_text(f'Min: {min_val:.4g} {unit}' if np.isfinite(min_val) else 'Min: --')
        self.max_text.set_text(f'Max: {max_val:.4g} {unit}' if np.isfinite(max_val) else 'Max: --')
        self.avg_text.set_text(f'Avg: {avg_val:.4g} {unit}' if np.isfinite(avg_val) else 'Avg: --')
        self.info_text.set_text(f'User: {username}  Sample: {sample_name}')

        last_compliance = compliance_list[-1] if compliance_list else 'OK'
        comp_text = ""
        if last_compliance == 'V_COMP':
            comp_text = "VOLTAGE COMPLIANCE HIT!"
        elif last_compliance == 'I_COMP':
            comp_text = "CURRENT COMPLIANCE HIT!"
        if comp_text:
            self.compliance_indicator.set_bbox(dict(boxstyle='round,pad=0.3', fc='#ffe5e5', ec='red', alpha=0.95))
        else:
            self.compliance_indicator.set_bbox(None)
        self.compliance_indicator.set_text(comp_text)
        self.draw_idle()

    def clear_plot(self):
        self.line.set_data([], [])
        self.min_text.set_text('Min: --')
        self.max_text.set_text('Max: --')
        self.avg_text.set_text('Avg: --')
        self.info_text.set_text('User: --\nSample: --')
        self.compliance_indicator.set_text('')
        self.axes.relim()
        self.axes.autoscale_view(True, True, True)
        self.draw_idle()


_COLOR_MAP = {
    'red': '#d62728',
    'blue': '#1f77b4',
    'green': '#2ca02c',
    'orange': '#ff7f0e',
    'purple': '#9467bd',
    'black': '#000000',
}


def _resolve_color(color):
    if isinstance(color, str):
        return _COLOR_MAP.get(color.lower(), color)
    return color


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

    Drop-in replacement for ``MplCanvas`` on the high-update-rate tabs.
    matplotlib redraws cost tens of milliseconds per frame and visibly
    stutter at >5 Hz sampling; pyqtgraph stays under a couple of ms even at
    20–50 Hz, which is what a research demo actually wants to look like.

    Public API mirrors ``MplCanvas``:
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
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel('bottom', 'Time (s)')
        self.plot_widget.setLabel('left', 'Value')
        # Auto-range follows new points as they stream in — this is the
        # behavior matplotlib gave us with relim()+autoscale_view().
        self.plot_widget.enableAutoRange(axis='xy', enable=True)
        # SI prefix on y axis (k, M, m, µ, n) — matches the way the rest of
        # the lab thinks about resistance/current.
        self.plot_widget.getAxis('left').enableAutoSIPrefix(True)
        # Long runs (hour-plus at 20 Hz = 70k+ samples) keep their fidelity
        # while staying interactively smooth: 'peak' downsampling preserves
        # transient spikes that 'mean' would erase, and clipToView avoids
        # rendering samples outside the visible range during pan/zoom.
        self.plot_widget.setDownsampling(auto=True, mode='peak')
        self.plot_widget.setClipToView(True)
        outer.addWidget(self.plot_widget, 1)

        # Stats row below the plot. Kept compact and monospaced so the
        # numbers don't jitter as digits change.
        stat_font = QFont('Monospace')
        stat_font.setStyleHint(QFont.TypeWriter)
        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(4, 0, 4, 0)
        self.min_label = QLabel('Min: --')
        self.max_label = QLabel('Max: --')
        self.avg_label = QLabel('Avg: --')
        self.info_label = QLabel('User: --   Sample: --')
        for w in (self.min_label, self.max_label, self.avg_label, self.info_label):
            w.setFont(stat_font)
        stats_row.addWidget(self.min_label)
        stats_row.addWidget(self.max_label)
        stats_row.addWidget(self.avg_label)
        stats_row.addStretch(1)
        stats_row.addWidget(self.info_label)
        outer.addLayout(stats_row)

        # Curve — created once and updated in place via setData(). This is
        # the whole performance story: no axes.clear()/replot() per frame.
        self._pen_color = _resolve_color('red')
        self.curve = self.plot_widget.plot(
            [], [], pen=pg.mkPen(self._pen_color, width=2), name='Measurement'
        )
        self._title = 'Measurement'
        self._y_label = 'Value'
        self._y_unit = ''
        self._y_symbol = ''  # e.g. 'R', 'V', 'I' — derived from the title
        self.plot_widget.setTitle(self._title)

        # Big latest-value pill, anchored to the top-right corner of the
        # viewbox so the audience can read the live number from across the
        # room. ignoreBounds keeps it from disturbing the auto-range, and
        # the sigRangeChanged callback re-pins it whenever the view moves.
        self._value_font = QFont('Monospace', 16, QFont.Bold)
        self._value_font.setStyleHint(QFont.TypeWriter)
        self.value_label = pg.TextItem(
            text='', color=(20, 20, 20), anchor=(1.0, 0.0),
            fill=(255, 255, 220, 220), border={'color': (120, 120, 120), 'width': 1},
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
