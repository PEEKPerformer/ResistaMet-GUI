import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas


class MplCanvas(FigureCanvas):
    def __init__(self, parent=None, width=8, height=5, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = self.fig.add_subplot(111)
        self.axes.ticklabel_format(useOffset=False, style='plain')
        super().__init__(self.fig)
        self.parent = parent

        self.line, = self.axes.plot([], [], 'r-', label='Measurement')
        self.min_text = self.axes.text(0.02, 0.95, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.max_text = self.axes.text(0.02, 0.90, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.avg_text = self.axes.text(0.02, 0.85, '', transform=self.axes.transAxes, ha='left', va='top', fontsize=9)
        self.info_text = self.axes.text(0.98, 0.95, '', transform=self.axes.transAxes, ha='right', va='top', fontsize=9)
        self.compliance_indicator = self.axes.text(0.5, 1.02, '', transform=self.axes.transAxes, ha='center', va='bottom', fontsize=10, color='red', weight='bold')

        bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="grey", alpha=0.7)
        for t in (self.min_text, self.max_text, self.avg_text, self.info_text):
            t.set_bbox(bbox_props)

        self.axes.legend(loc='upper right')
        self.fig.tight_layout(rect=[0, 0, 1, 0.95])
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
        self.min_text.set_text(f'Min: {min_val:.3f} {unit}' if np.isfinite(min_val) else 'Min: --')
        self.max_text.set_text(f'Max: {max_val:.3f} {unit}' if np.isfinite(max_val) else 'Max: --')
        self.avg_text.set_text(f'Avg: {avg_val:.3f} {unit}' if np.isfinite(avg_val) else 'Avg: --')
        self.info_text.set_text(f'User: {username}\nSample: {sample_name}')

        last_compliance = compliance_list[-1] if compliance_list else 'OK'
        comp_text = ""
        if last_compliance == 'V_COMP':
            comp_text = "VOLTAGE COMPLIANCE HIT!"
        elif last_compliance == 'I_COMP':
            comp_text = "CURRENT COMPLIANCE HIT!"
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

