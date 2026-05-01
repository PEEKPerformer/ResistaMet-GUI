import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas


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
