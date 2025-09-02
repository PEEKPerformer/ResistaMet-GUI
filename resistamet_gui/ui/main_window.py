import time
from datetime import datetime
from typing import Dict, Optional

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAction, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QShortcut, QTextEdit,
    QTabWidget, QVBoxLayout, QWidget, QFileDialog, QSplitter, QTableWidget, QTableWidgetItem, QDialog
)
from PyQt5.QtGui import QIcon
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar

from ..buffers import EnhancedDataBuffer
from ..config import ConfigManager
from ..constants import __version__
from ..workers import MeasurementWorker
from .canvas import MplCanvas
from .dialogs import SettingsDialog, UserSelectionDialog


class ResistanceMeterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self.data_buffers = {
            'resistance': EnhancedDataBuffer(),
            'source_v': EnhancedDataBuffer(),
            'source_i': EnhancedDataBuffer(),
            'four_point': EnhancedDataBuffer(),
        }
        self.measurement_worker = None
        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.update_active_plot)
        self.current_user = None
        self.user_settings = None
        self.measurement_running = False
        self.active_mode = None
        self.setWindowTitle(f"ResistaMet GUI v{__version__}")
        self.setMinimumSize(900, 700)
        self.setWindowIcon(QIcon.fromTheme("accessories-voltmeter"))
        self.init_ui()
        self.select_user()

    def init_ui(self):
        central_widget = QWidget(); self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        top_panel = QHBoxLayout()
        user_group = QGroupBox("User"); user_layout = QHBoxLayout()
        self.user_label = QLabel("User: <None Selected>")
        self.change_user_button = QPushButton(QIcon.fromTheme("system-users"), "Change User")
        self.change_user_button.clicked.connect(self.select_user)
        user_layout.addWidget(self.user_label); user_layout.addWidget(self.change_user_button)
        user_group.setLayout(user_layout)
        sample_group = QGroupBox("Sample"); sample_layout = QHBoxLayout()
        self.sample_input = QLineEdit(); self.sample_input.setPlaceholderText("Enter sample name before starting")
        sample_layout.addWidget(self.sample_input); sample_group.setLayout(sample_layout)
        top_panel.addWidget(user_group); top_panel.addWidget(sample_group, 1)
        main_layout.addLayout(top_panel)

        self.main_tabs = QTabWidget(); self.main_tabs.currentChanged.connect(self.handle_tab_change)
        self.tab_resistance = self.create_resistance_tab()
        self.tab_voltage_source = self.create_voltage_source_tab()
        self.tab_current_source = self.create_current_source_tab()
        self.tab_four_point = self.create_four_point_tab()
        self.main_tabs.addTab(self.tab_resistance, "Resistance Measurement")
        self.main_tabs.addTab(self.tab_voltage_source, "Voltage Source")
        self.main_tabs.addTab(self.tab_current_source, "Current Source")
        self.main_tabs.addTab(self.tab_four_point, "4-Point Probe")

        # Status log
        self.status_group = QGroupBox("Status Log"); status_layout = QVBoxLayout()
        self.status_display = QTextEdit(); self.status_display.setReadOnly(True); self.status_display.setAcceptRichText(True)
        self.status_display.setMaximumHeight(150); status_layout.addWidget(self.status_display)
        self.status_group.setLayout(status_layout)

        # Splitter to allow resizing between tabs and status log
        self.main_splitter = QSplitter()
        self.main_splitter.setOrientation(Qt.Vertical)
        self.main_splitter.addWidget(self.main_tabs)
        self.main_splitter.addWidget(self.status_group)
        self.main_splitter.setStretchFactor(0, 5)
        self.main_splitter.setStretchFactor(1, 1)
        main_layout.addWidget(self.main_splitter, 1)
        self.statusBar().showMessage("Ready")
        self.create_menus()
        # Sync initial hide/show button text
        self.update_hide_show_buttons()
        self.shortcut_mark = QShortcut(Qt.Key_M, self); self.shortcut_mark.activated.connect(self.mark_event_shortcut)
        self.shortcut_mark.setEnabled(False)

    def create_tab_widget(self, mode: str) -> QWidget:
        tab_widget = QWidget(); tab_layout = QVBoxLayout(tab_widget)
        param_group = QGroupBox("Parameters"); param_layout = QFormLayout(); param_group.setLayout(param_layout)
        plot_group = QGroupBox("Real-time Data"); plot_layout = QVBoxLayout()
        canvas = MplCanvas(self, width=8, height=5, dpi=90); toolbar = NavigationToolbar(canvas, self)
        plot_layout.addWidget(toolbar); plot_layout.addWidget(canvas); plot_group.setLayout(plot_layout)
        control_group = QGroupBox("Control"); control_layout = QHBoxLayout()
        start_button = QPushButton(QIcon.fromTheme("media-playback-start"), "Start")
        stop_button = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop"); stop_button.setEnabled(False)
        pause_button = QPushButton(QIcon.fromTheme("media-playback-pause"), "Pause"); pause_button.setEnabled(False); pause_button.setCheckable(True)
        status_label = QLabel("Status: Idle"); status_label.setStyleSheet("font-weight: bold;")
        control_layout.addWidget(start_button); control_layout.addWidget(stop_button); control_layout.addWidget(pause_button)
        # Hide buttons for quicker collapsing
        hide_params_btn = QPushButton("Hide Params")
        hide_params_btn.setToolTip("Hide/show parameters section")
        hide_params_btn.clicked.connect(self._toggle_params_action)
        hide_controls_btn = QPushButton("Hide Controls")
        hide_controls_btn.setToolTip("Hide/show controls section")
        hide_controls_btn.clicked.connect(self._toggle_controls_action)
        control_layout.addWidget(hide_params_btn)
        control_layout.addWidget(hide_controls_btn)
        control_layout.addStretch(); control_layout.addWidget(status_label); control_group.setLayout(control_layout)

        # Vertical splitter to resize/collapse sections per tab
        tab_splitter = QSplitter(); tab_splitter.setOrientation(Qt.Vertical)
        tab_splitter.addWidget(param_group)
        tab_splitter.addWidget(plot_group)
        tab_splitter.addWidget(control_group)
        tab_splitter.setStretchFactor(0, 1)
        tab_splitter.setStretchFactor(1, 5)
        tab_splitter.setStretchFactor(2, 1)
        tab_layout.addWidget(tab_splitter)
        tab_widget.mode = mode; tab_widget.param_layout = param_layout; tab_widget.canvas = canvas
        tab_widget.start_button = start_button; tab_widget.stop_button = stop_button; tab_widget.pause_button = pause_button
        tab_widget.status_label = status_label
        tab_widget.param_group = param_group
        tab_widget.plot_group = plot_group
        tab_widget.control_group = control_group
        tab_widget.splitter = tab_splitter
        tab_widget.hide_params_btn = hide_params_btn
        tab_widget.hide_controls_btn = hide_controls_btn
        return tab_widget

    def create_resistance_tab(self):
        widget = self.create_tab_widget('resistance'); layout = widget.param_layout
        widget.res_test_current = QDoubleSpinBox(decimals=6, minimum=1e-7, maximum=1.0, singleStep=1e-3, suffix=" A"); layout.addRow("Test Current:", widget.res_test_current)
        widget.res_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=100.0, singleStep=0.1, suffix=" V"); layout.addRow("Voltage Compliance:", widget.res_voltage_compliance)
        widget.res_measurement_type = QComboBox(); widget.res_measurement_type.addItems(["2-wire", "4-wire"]); layout.addRow("Measurement Type:", widget.res_measurement_type)
        widget.res_auto_range = QCheckBox("Auto Range Resistance"); layout.addRow(widget.res_auto_range)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)"); widget.mark_event_button.setEnabled(False); layout.addRow(widget.mark_event_button)
        widget.start_button.clicked.connect(lambda: self.start_measurement('resistance'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        return widget

    def create_voltage_source_tab(self):
        widget = self.create_tab_widget('source_v'); layout = widget.param_layout
        widget.vsource_voltage = QDoubleSpinBox(decimals=3, minimum=-100.0, maximum=100.0, singleStep=0.1, suffix=" V"); layout.addRow("Source Voltage:", widget.vsource_voltage)
        widget.vsource_current_compliance = QDoubleSpinBox(decimals=6, minimum=1e-7, maximum=1.0, singleStep=1e-3, suffix=" A"); layout.addRow("Current Compliance:", widget.vsource_current_compliance)
        widget.vsource_current_range_auto = QCheckBox("Auto Range Current Measurement"); layout.addRow(widget.vsource_current_range_auto)
        widget.vsource_duration = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h"); layout.addRow("Duration (hours):", widget.vsource_duration)
        widget.v_plot_var = QComboBox(); widget.v_plot_var.addItems(["current", "voltage", "resistance"]); layout.addRow("Plot Variable:", widget.v_plot_var)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)"); widget.mark_event_button.setEnabled(False); layout.addRow(widget.mark_event_button)
        widget.start_button.clicked.connect(lambda: self.start_measurement('source_v'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.v_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('source_v'))
        return widget

    def create_current_source_tab(self):
        widget = self.create_tab_widget('source_i'); layout = widget.param_layout
        widget.isource_current = QDoubleSpinBox(decimals=6, minimum=-1.0, maximum=1.0, singleStep=1e-3, suffix=" A"); layout.addRow("Source Current:", widget.isource_current)
        widget.isource_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=100.0, singleStep=0.1, suffix=" V"); layout.addRow("Voltage Compliance:", widget.isource_voltage_compliance)
        widget.isource_voltage_range_auto = QCheckBox("Auto Range Voltage Measurement"); layout.addRow(widget.isource_voltage_range_auto)
        widget.isource_duration = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h"); layout.addRow("Duration (hours):", widget.isource_duration)
        widget.i_plot_var = QComboBox(); widget.i_plot_var.addItems(["voltage", "current", "resistance"]); layout.addRow("Plot Variable:", widget.i_plot_var)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)"); widget.mark_event_button.setEnabled(False); layout.addRow(widget.mark_event_button)
        widget.start_button.clicked.connect(lambda: self.start_measurement('source_i'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.i_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('source_i'))
        return widget

    def create_four_point_tab(self):
        widget = self.create_tab_widget('four_point')
        layout = widget.param_layout
        # Instrument parameters
        widget.fpp_current = QDoubleSpinBox(decimals=6, minimum=-1.0, maximum=1.0, singleStep=1e-3, suffix=" A")
        layout.addRow("Source Current:", widget.fpp_current)
        widget.fpp_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=200.0, singleStep=0.1, suffix=" V")
        layout.addRow("Voltage Compliance:", widget.fpp_voltage_compliance)
        widget.fpp_voltage_range_auto = QCheckBox("Auto Range Voltage Measurement")
        # Probe geometry & calc params
        widget.fpp_spacing_cm = QDoubleSpinBox(decimals=5, minimum=0.001, maximum=5.0, singleStep=0.001, suffix=" cm")
        layout.addRow("Probe Spacing s:", widget.fpp_spacing_cm)
        widget.fpp_thickness_um = QDoubleSpinBox(decimals=3, minimum=0.0, maximum=5000.0, singleStep=0.1, suffix=" µm")
        layout.addRow("Thickness t (optional):", widget.fpp_thickness_um)
        widget.fpp_alpha = QDoubleSpinBox(decimals=4, minimum=0.0, maximum=10.0, singleStep=0.01)
        widget.fpp_k_factor = QDoubleSpinBox(decimals=4, minimum=0.1, maximum=50.0, singleStep=0.001)
        widget.fpp_model = QComboBox()
        widget.fpp_model.addItems(["thin_film", "semi_infinite", "finite_thin", "finite_alpha"])
        layout.addRow("Model:", widget.fpp_model)
        # Plot variable and plot visibility
        widget.fpp_plot_var = QComboBox()
        widget.fpp_plot_var.addItems(["voltage", "current", "V/I", "sheet_Rs", "rho"])
        layout.addRow("Plot Variable:", widget.fpp_plot_var)
        widget.fpp_show_plot = QCheckBox("Show Plot")
        widget.fpp_show_plot.setChecked(False)
        # The plot section will be hidden by default; checkbox toggles it
        widget.fpp_show_plot.toggled.connect(lambda v: widget.plot_group.setVisible(v))
        layout.addRow(widget.fpp_show_plot)

        # Model info
        widget.fpp_model_info = QLabel("")
        widget.fpp_model_info.setWordWrap(True)
        layout.addRow("Model Info:", widget.fpp_model_info)
        # Advanced collapsible
        adv_toggle = QCheckBox("Show Advanced")
        layout.addRow(adv_toggle)
        adv_group = QGroupBox("Advanced")
        adv_form = QFormLayout()
        adv_form.addRow("Auto Range Voltage:", widget.fpp_voltage_range_auto)
        adv_form.addRow("Correction Factor α:", widget.fpp_alpha)
        adv_form.addRow("K Factor:", widget.fpp_k_factor)
        adv_group.setLayout(adv_form)
        adv_group.setVisible(False)
        adv_toggle.toggled.connect(adv_group.setVisible)
        layout.addRow(adv_group)
        # Mark event
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setEnabled(False)
        layout.addRow(widget.mark_event_button)
        # Quick report button
        widget.report_button = QPushButton(QIcon.fromTheme("document-save"), "Export Summary...")
        widget.report_button.clicked.connect(self.export_fpp_summary)
        layout.addRow(widget.report_button)

        # Connect events
        widget.start_button.clicked.connect(lambda: self.start_measurement('four_point'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.fpp_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('four_point'))
        widget.fpp_model.currentTextChanged.connect(lambda *_: self.update_four_point_model_info())
        widget.fpp_alpha.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        widget.fpp_k_factor.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        widget.fpp_spacing_cm.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        widget.fpp_thickness_um.valueChanged.connect(lambda *_: self.update_four_point_model_info())

        # Data table and summary for 4PP
        widget.fpp_table = QTableWidget(0, 9)
        widget.fpp_table.setHorizontalHeaderLabels([
            'Time (s)', 'V (V)', 'I (A)', 'V/I (Ω)', 'Rs (Ω/□)', 'ρ (Ω·cm)', 'σ (S/cm)', 'Comp', 'Event'
        ])
        layout.addRow(QLabel("Measurements:"))
        layout.addRow(widget.fpp_table)

        widget.fpp_summary = QGroupBox("Summary Stats")
        sum_layout = QFormLayout()
        widget.fpp_n_label = QLabel("0")
        widget.fpp_rs_label = QLabel("--")
        widget.fpp_rho_label = QLabel("--")
        widget.fpp_sigma_label = QLabel("--")
        sum_layout.addRow("N:", widget.fpp_n_label)
        sum_layout.addRow("Rs mean±std (Ω/□; RSD%):", widget.fpp_rs_label)
        sum_layout.addRow("ρ mean±std (Ω·cm; RSD%):", widget.fpp_rho_label)
        sum_layout.addRow("σ mean±std (S/cm; RSD%):", widget.fpp_sigma_label)
        widget.fpp_summary.setLayout(sum_layout)
        layout.addRow(widget.fpp_summary)

        # Make parameter inputs compact
        for sb in [widget.fpp_current, widget.fpp_voltage_compliance, widget.fpp_spacing_cm, widget.fpp_thickness_um, widget.fpp_alpha, widget.fpp_k_factor]:
            sb.setMaximumWidth(140)
        widget.fpp_plot_var.setMaximumWidth(140)
        widget.fpp_model.setMaximumWidth(160)
        widget.fpp_show_plot.setMaximumWidth(120)

        # Hide plot pane by default
        widget.plot_group.setVisible(False)
        # Internal storage for quick stats
        widget._fpp_rows = []  # list of tuples (time, v, i, ratio, rs, rho, sigma, comp, event)
        # Initialize model info text using this widget (before self.tab_four_point is assigned)
        self.update_four_point_model_info(widget)

        return widget

    def create_menus(self):
        menu_bar = self.menuBar()
        # File
        file_menu = menu_bar.addMenu("&File")
        save_plot_action = QAction(QIcon.fromTheme("document-save"), "Save Plot...", self)
        save_plot_action.triggered.connect(self.save_active_plot)
        open_result_action = QAction(QIcon.fromTheme("document-open"), "Open Result (CSV)...", self)
        open_result_action.triggered.connect(self.open_result_csv)
        exit_action = QAction(QIcon.fromTheme("application-exit"), "Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(save_plot_action)
        file_menu.addAction(open_result_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)
        # Profiles
        profiles_menu = menu_bar.addMenu("&Profiles")
        save_prof_action = QAction("Save Profile for Current Mode...", self)
        save_prof_action.triggered.connect(self.save_profile_for_mode)
        load_prof_action = QAction("Load Profile to Current Mode...", self)
        load_prof_action.triggered.connect(self.load_profile_to_mode)
        profiles_menu.addAction(save_prof_action)
        profiles_menu.addAction(load_prof_action)
        # Settings
        settings_menu = menu_bar.addMenu("&Settings")
        user_settings_action = QAction(QIcon.fromTheme("preferences-system"), "User Settings...", self)
        user_settings_action.triggered.connect(self.open_user_settings)
        global_settings_action = QAction(QIcon.fromTheme("preferences-system-windows"), "Global Settings...", self)
        global_settings_action.triggered.connect(self.open_global_settings)
        settings_menu.addAction(user_settings_action)
        settings_menu.addAction(global_settings_action)
        # Help
        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction(QIcon.fromTheme("help-about"), "About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

        # View
        view_menu = menu_bar.addMenu("&View")
        self.action_show_params = QAction("Show Parameters", self, checkable=True)
        self.action_show_params.setChecked(True)
        self.action_show_params.toggled.connect(lambda v: self.toggle_section_visibility('params', v))
        self.action_show_controls = QAction("Show Controls", self, checkable=True)
        self.action_show_controls.setChecked(True)
        self.action_show_controls.toggled.connect(lambda v: self.toggle_section_visibility('controls', v))
        self.action_show_status = QAction("Show Status Log", self, checkable=True)
        self.action_show_status.setChecked(True)
        self.action_show_status.toggled.connect(lambda v: self.toggle_status_visibility(v))
        view_menu.addAction(self.action_show_params)
        view_menu.addAction(self.action_show_controls)
        view_menu.addSeparator()
        view_menu.addAction(self.action_show_status)

        # Results viewer tab (ensure only one is added)
        has_results = False
        for i in range(self.main_tabs.count()):
            if self.main_tabs.tabText(i) == "Results Viewer":
                has_results = True
                break
        if not has_results:
            self.tab_results = self.create_results_tab()
            self.main_tabs.addTab(self.tab_results, "Results Viewer")

    def toggle_status_visibility(self, visible: bool):
        if hasattr(self, 'status_group') and self.status_group:
            self.status_group.setVisible(visible)
        self.update_hide_show_buttons()

    def toggle_section_visibility(self, section: str, visible: bool):
        # section in {'params','controls'}
        for mode in ['resistance', 'source_v', 'source_i', 'four_point']:
            w = self.get_widget_for_mode(mode)
            if not w:
                continue
            if section == 'params' and hasattr(w, 'param_group'):
                w.param_group.setVisible(visible)
            if section == 'controls' and hasattr(w, 'control_group'):
                w.control_group.setVisible(visible)
        self.update_hide_show_buttons()

    def _toggle_params_action(self):
        # invert global action and let handlers do the rest
        if hasattr(self, 'action_show_params'):
            self.action_show_params.setChecked(not self.action_show_params.isChecked())

    def _toggle_controls_action(self):
        if hasattr(self, 'action_show_controls'):
            self.action_show_controls.setChecked(not self.action_show_controls.isChecked())

    def update_hide_show_buttons(self):
        # Sync button text with current visibility state
        for mode in ['resistance', 'source_v', 'source_i', 'four_point']:
            w = self.get_widget_for_mode(mode)
            if not w:
                continue
            if hasattr(w, 'param_group') and hasattr(w, 'hide_params_btn'):
                w.hide_params_btn.setText('Hide Params' if w.param_group.isVisible() else 'Show Params')
            if hasattr(w, 'control_group') and hasattr(w, 'hide_controls_btn'):
                w.hide_controls_btn.setText('Hide Controls' if w.control_group.isVisible() else 'Show Controls')

    def create_results_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        # Controls
        controls = QHBoxLayout()
        open_btn = QPushButton(QIcon.fromTheme("document-open"), "Open CSV...")
        open_btn.clicked.connect(self.open_result_csv)
        controls.addWidget(open_btn)
        controls.addStretch()
        layout.addLayout(controls)
        # Plot variable selector
        form = QFormLayout()
        self.results_var = QComboBox(); self.results_var.currentTextChanged.connect(self.update_results_plot)
        form.addRow("Y Variable:", self.results_var)
        layout.addLayout(form)
        # Plot canvas
        self.results_canvas = MplCanvas(self, width=8, height=5, dpi=90)
        layout.addWidget(NavigationToolbar(self.results_canvas, self))
        layout.addWidget(self.results_canvas)
        # Storage
        self.results_data = {"time": [], "columns": {}, "order": []}
        return tab

    def open_result_csv(self):
        # Ensure Results Viewer tab is available and its widgets exist
        if not hasattr(self, 'results_var') or not hasattr(self, 'results_canvas'):
            # Try to add/create the tab
            has_results = False
            for i in range(self.main_tabs.count()):
                if self.main_tabs.tabText(i) == "Results Viewer":
                    has_results = True
                    break
            if not has_results:
                self.tab_results = self.create_results_tab()
                self.main_tabs.addTab(self.tab_results, "Results Viewer")
        filename, _ = QFileDialog.getOpenFileName(self, "Open Result CSV", self.user_settings['file']['data_directory'] if self.user_settings else ".", "CSV Files (*.csv);;All Files (*)")
        if not filename:
            return
        try:
            import csv
            times = []
            columns = {}
            order = []
            with open(filename, 'r') as f:
                reader = csv.reader(f)
                headers = None
                for row in reader:
                    if not row:
                        continue
                    if row[0].startswith('###') or row[0].startswith('#') or row[0] in ('Test Parameters',):
                        continue
                    if headers is None:
                        headers = row
                        # Build indices of known columns
                        # Expect at least: 'Elapsed Time (s)'
                        for h in headers:
                            columns[h] = []
                            order.append(h)
                        continue
                    # Data row length mismatch guard
                    if headers and len(row) == len(headers):
                        for i, h in enumerate(headers):
                            val = row[i]
                            try:
                                valf = float(val)
                            except Exception:
                                valf = float('nan')
                            columns[h].append(valf)
            # Map elapsed time
            tkey = None
            for k in columns.keys():
                if 'Elapsed Time' in k:
                    tkey = k; break
            if not tkey:
                QMessageBox.warning(self, "Open Result", "Could not find 'Elapsed Time' column in CSV.")
                return
            self.results_data = {"time": columns[tkey], "columns": columns, "order": order}
            # Populate variable choices (exclude time)
            y_choices = [k for k in order if k != tkey]
            self.results_var.blockSignals(True)
            self.results_var.clear(); self.results_var.addItems(y_choices)
            self.results_var.blockSignals(False)
            if y_choices:
                self.results_var.setCurrentIndex(0)
            self.update_results_plot()
            self.log_status(f"Loaded results from: {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Open Error", f"Failed to open CSV: {e}")

    def update_results_plot(self):
        data = self.results_data
        if not data or not data.get('time'):
            self.results_canvas.clear_plot(); return
        var = self.results_var.currentText()
        if not var or var not in data['columns']:
            return
        t = data['time']; y = data['columns'][var]
        # Update labels heuristically
        ylabel = var
        title = "Results Viewer"
        color = 'blue'
        self.results_canvas.set_plot_properties('Elapsed Time (s)', ylabel, title, color)
        # Canvas expects absolute timestamps; give it time baseline
        # Wrap to the API used elsewhere
        timestamps = list(range(len(t)))
        # Override timestamps with actual elapsed seconds as 'values' are used with elapsed offset, but canvas subtracts first value
        # So we pass t directly as 'timestamps' and canvas does elapsed (t - t0)
        timestamps = t
        compliance = ['OK'] * len(t)
        stats = {
            'min': min([v for v in y if isinstance(v, (int, float)) and not np.isnan(v)], default=float('inf')),
            'max': max([v for v in y if isinstance(v, (int, float)) and not np.isnan(v)], default=float('-inf')),
            'avg': (sum([v for v in y if isinstance(v, (int, float)) and not np.isnan(v)]) / max(1, len([v for v in y if isinstance(v, (int, float)) and not np.isnan(v)]))) if y else 0,
        }
        self.results_canvas.update_plot(timestamps, y, compliance, stats, self.current_user or '-', self.sample_input.text() or '-')

    def update_canvas_labels_for_mode(self, mode: str):
        # Update current tab canvas labels based on selected plot variable
        if not self.user_settings:
            return
        d_cfg = self.user_settings['display']
        w = self.get_widget_for_mode(mode)
        if not w or not hasattr(w, 'canvas'):
            return
        if mode == 'source_v':
            var = w.v_plot_var.currentText() if hasattr(w, 'v_plot_var') else 'current'
            color = d_cfg.get('plot_color_v', 'blue')
            if var == 'current':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', 'Voltage Source Output', color)
            elif var == 'voltage':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Voltage (V)', 'Voltage Source Output', color)
            else:
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Resistance (Ω)', 'Voltage Source Output', color)
        elif mode == 'source_i':
            var = w.i_plot_var.currentText() if hasattr(w, 'i_plot_var') else 'voltage'
            color = d_cfg.get('plot_color_i', 'green')
            if var == 'voltage':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Voltage (V)', 'Current Source Output', color)
            elif var == 'current':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', 'Current Source Output', color)
            else:
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Resistance (Ω)', 'Current Source Output', color)
        elif mode == 'four_point':
            var = w.fpp_plot_var.currentText() if hasattr(w, 'fpp_plot_var') else 'sheet_Rs'
            color = d_cfg.get('plot_color_r', 'red')
            if var == 'sheet_Rs':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Sheet Resistance (Ω/□)', '4-Point Probe', color)
            elif var == 'rho':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Resistivity (Ω·cm)', '4-Point Probe', color)
            elif var == 'V/I':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'V/I (Ω)', '4-Point Probe', color)
            elif var == 'voltage':
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Voltage (V)', '4-Point Probe', color)
            else:
                w.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', '4-Point Probe', color)

    def save_profile_for_mode(self):
        mode_widget = self.main_tabs.currentWidget()
        mode = getattr(mode_widget, 'mode', None)
        if mode not in ('resistance', 'source_v', 'source_i', 'four_point'):
            QMessageBox.warning(self, "Save Profile", "Please switch to a measurement tab to save a profile.")
            return
        settings = self.gather_settings_for_mode(mode)
        filename, _ = QFileDialog.getSaveFileName(self, "Save Profile", f"{mode}_profile.json", "JSON Files (*.json)")
        if not filename:
            return
        try:
            import json
            with open(filename, 'w') as f:
                json.dump(settings['measurement'], f, indent=2)
            self.log_status(f"Profile saved: {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Save Profile", f"Failed to save profile: {e}")

    def load_profile_to_mode(self):
        mode_widget = self.main_tabs.currentWidget()
        mode = getattr(mode_widget, 'mode', None)
        if mode not in ('resistance', 'source_v', 'source_i', 'four_point'):
            QMessageBox.warning(self, "Load Profile", "Please switch to a measurement tab to load a profile.")
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Load Profile", "", "JSON Files (*.json)")
        if not filename:
            return
        try:
            import json
            with open(filename, 'r') as f:
                prof = json.load(f)
            # Apply known fields to current tab UI
            w = mode_widget
            if mode == 'resistance':
                if 'res_test_current' in prof: w.res_test_current.setValue(float(prof['res_test_current']))
                if 'res_voltage_compliance' in prof: w.res_voltage_compliance.setValue(float(prof['res_voltage_compliance']))
                if 'res_measurement_type' in prof: w.res_measurement_type.setCurrentText(str(prof['res_measurement_type']))
                if 'res_auto_range' in prof: w.res_auto_range.setChecked(bool(prof['res_auto_range']))
            elif mode == 'source_v':
                if 'vsource_voltage' in prof: w.vsource_voltage.setValue(float(prof['vsource_voltage']))
                if 'vsource_current_compliance' in prof: w.vsource_current_compliance.setValue(float(prof['vsource_current_compliance']))
                if 'vsource_current_range_auto' in prof: w.vsource_current_range_auto.setChecked(bool(prof['vsource_current_range_auto']))
                if 'vsource_duration_hours' in prof: w.vsource_duration.setValue(float(prof['vsource_duration_hours']))
            elif mode == 'source_i':
                if 'isource_current' in prof: w.isource_current.setValue(float(prof['isource_current']))
                if 'isource_voltage_compliance' in prof: w.isource_voltage_compliance.setValue(float(prof['isource_voltage_compliance']))
                if 'isource_voltage_range_auto' in prof: w.isource_voltage_range_auto.setChecked(bool(prof['isource_voltage_range_auto']))
                if 'isource_duration_hours' in prof: w.isource_duration.setValue(float(prof['isource_duration_hours']))
            elif mode == 'four_point':
                if 'fpp_current' in prof: w.fpp_current.setValue(float(prof['fpp_current']))
                if 'fpp_voltage_compliance' in prof: w.fpp_voltage_compliance.setValue(float(prof['fpp_voltage_compliance']))
                if 'fpp_voltage_range_auto' in prof: w.fpp_voltage_range_auto.setChecked(bool(prof['fpp_voltage_range_auto']))
                if 'fpp_spacing_cm' in prof: w.fpp_spacing_cm.setValue(float(prof['fpp_spacing_cm']))
                # Accept either µm or legacy cm
                if 'fpp_thickness_um' in prof:
                    w.fpp_thickness_um.setValue(float(prof['fpp_thickness_um']))
                elif 'fpp_thickness_cm' in prof:
                    w.fpp_thickness_um.setValue(float(prof['fpp_thickness_cm']) * 1e4)
                if 'fpp_alpha' in prof: w.fpp_alpha.setValue(float(prof['fpp_alpha']))
                if 'fpp_k_factor' in prof: w.fpp_k_factor.setValue(float(prof['fpp_k_factor']))
                if 'fpp_model' in prof: w.fpp_model.setCurrentText(str(prof['fpp_model']))
            self.log_status(f"Profile loaded: {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Load Profile", f"Failed to load profile: {e}")

    def select_user(self):
        if self.measurement_running:
            QMessageBox.warning(self, "Action Denied", "Cannot change user while a measurement is running.")
            return
        dialog = UserSelectionDialog(self.config_manager, self)
        if dialog.exec_():
            username = dialog.selected_user
            if username:
                self.current_user = username
                self.user_label.setText(f"User: <b>{username}</b>")
                self.user_settings = self.config_manager.get_user_settings(username)
                self.log_status(f"User selected: {username}")
                self.statusBar().showMessage(f"User: {username} | Ready")
                self.update_ui_from_settings()
                for buffer in self.data_buffers.values():
                    buffer.clear()
                self.clear_all_plots()
        else:
            if not self.current_user:
                self.log_status("No user selected. Please select or create a user.")
                self.set_all_controls_enabled(False)

    def update_ui_from_settings(self):
        if not self.user_settings:
            return
        m_cfg = self.user_settings['measurement']; d_cfg = self.user_settings['display']
        self.tab_resistance.res_test_current.setValue(m_cfg['res_test_current'])
        self.tab_resistance.res_voltage_compliance.setValue(m_cfg['res_voltage_compliance'])
        self.tab_resistance.res_measurement_type.setCurrentText(m_cfg['res_measurement_type'])
        self.tab_resistance.res_auto_range.setChecked(m_cfg['res_auto_range'])
        self.tab_resistance.canvas.set_plot_properties('Elapsed Time (s)', 'Resistance (Ohms)', 'Resistance Measurement', d_cfg['plot_color_r'])
        self.tab_voltage_source.vsource_voltage.setValue(m_cfg['vsource_voltage'])
        self.tab_voltage_source.vsource_current_compliance.setValue(m_cfg['vsource_current_compliance'])
        self.tab_voltage_source.vsource_current_range_auto.setChecked(m_cfg['vsource_current_range_auto'])
        self.tab_voltage_source.vsource_duration.setValue(m_cfg.get('vsource_duration_hours', 0.0))
        self.tab_voltage_source.v_plot_var.setCurrentText('current')
        self.tab_voltage_source.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', 'Voltage Source Output', d_cfg['plot_color_v'])
        self.tab_current_source.isource_current.setValue(m_cfg['isource_current'])
        self.tab_current_source.isource_voltage_compliance.setValue(m_cfg['isource_voltage_compliance'])
        self.tab_current_source.isource_voltage_range_auto.setChecked(m_cfg['isource_voltage_range_auto'])
        self.tab_current_source.isource_duration.setValue(m_cfg.get('isource_duration_hours', 0.0))
        self.tab_current_source.i_plot_var.setCurrentText('voltage')
        self.tab_current_source.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Voltage (V)', 'Current Source Output', d_cfg['plot_color_i'])
        # Four-Point Probe
        self.tab_four_point.fpp_current.setValue(m_cfg['fpp_current'])
        self.tab_four_point.fpp_voltage_compliance.setValue(m_cfg['fpp_voltage_compliance'])
        self.tab_four_point.fpp_voltage_range_auto.setChecked(m_cfg['fpp_voltage_range_auto'])
        self.tab_four_point.fpp_spacing_cm.setValue(m_cfg['fpp_spacing_cm'])
        # Support legacy cm setting if present
        t_um = m_cfg.get('fpp_thickness_um', None)
        if t_um is None:
            t_um = float(m_cfg.get('fpp_thickness_cm', 0.0)) * 1e4
        self.tab_four_point.fpp_thickness_um.setValue(t_um)
        self.tab_four_point.fpp_alpha.setValue(m_cfg.get('fpp_alpha', 1.0))
        self.tab_four_point.fpp_model.setCurrentText(m_cfg.get('fpp_model', 'thin_film'))
        self.tab_four_point.fpp_k_factor.setValue(m_cfg.get('fpp_k_factor', 4.532))
        self.tab_four_point.fpp_plot_var.setCurrentText('sheet_Rs')
        self.tab_four_point.canvas.set_plot_properties('Elapsed Time (s)', 'Sheet Resistance (Ω/□)', '4-Point Probe', d_cfg['plot_color_r'])
        buffer_size = d_cfg.get('buffer_size')
        new_size = None if buffer_size is None or buffer_size <= 0 else buffer_size
        for mode, buffer in list(self.data_buffers.items()):
            if buffer.size != new_size:
                self.data_buffers[mode] = EnhancedDataBuffer(size=new_size)
        self.clear_all_plots(); self.log_status("User settings loaded into UI.")

    def open_user_settings(self):
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select a user first to edit their settings.")
            return
        if self.measurement_running:
            QMessageBox.warning(self, "Action Denied", "Cannot change settings while a measurement is running.")
            return
        dialog = SettingsDialog(self.config_manager, self.current_user, self)
        if dialog.exec_():
            self.log_status(f"User settings for {self.current_user} updated.")
            self.user_settings = self.config_manager.get_user_settings(self.current_user)
            self.update_ui_from_settings()

    def open_global_settings(self):
        if self.measurement_running:
            QMessageBox.warning(self, "Action Denied", "Cannot change settings while a measurement is running.")
            return
        dialog = SettingsDialog(self.config_manager, parent=self)
        if dialog.exec_():
            self.log_status("Global settings updated.")
            if self.current_user:
                self.user_settings = self.config_manager.get_user_settings(self.current_user)
                self.update_ui_from_settings()

    def get_widget_for_mode(self, mode: str) -> Optional[QWidget]:
        if mode == 'resistance': return self.tab_resistance
        if mode == 'source_v': return self.tab_voltage_source
        if mode == 'source_i': return self.tab_current_source
        if mode == 'four_point': return self.tab_four_point
        return None

    def gather_settings_for_mode(self, mode:str) -> Dict:
        if not self.user_settings:
            raise ValueError("User settings not loaded.")
        effective_settings = {
            'measurement': dict(self.user_settings['measurement']),
            'display': dict(self.user_settings['display']),
            'file': dict(self.user_settings['file'])
        }
        m_cfg = effective_settings['measurement']
        widget = self.get_widget_for_mode(mode)
        if not widget:
            raise ValueError(f"Invalid mode specified: {mode}")
        try:
            if mode == 'resistance':
                m_cfg['res_test_current'] = widget.res_test_current.value()
                m_cfg['res_voltage_compliance'] = widget.res_voltage_compliance.value()
                m_cfg['res_measurement_type'] = widget.res_measurement_type.currentText()
                m_cfg['res_auto_range'] = widget.res_auto_range.isChecked()
            elif mode == 'source_v':
                m_cfg['vsource_voltage'] = widget.vsource_voltage.value()
                m_cfg['vsource_current_compliance'] = widget.vsource_current_compliance.value()
                m_cfg['vsource_current_range_auto'] = widget.vsource_current_range_auto.isChecked()
                m_cfg['vsource_duration_hours'] = widget.vsource_duration.value()
            elif mode == 'source_i':
                m_cfg['isource_current'] = widget.isource_current.value()
                m_cfg['isource_voltage_compliance'] = widget.isource_voltage_compliance.value()
                m_cfg['isource_voltage_range_auto'] = widget.isource_voltage_range_auto.isChecked()
                m_cfg['isource_duration_hours'] = widget.isource_duration.value()
            elif mode == 'four_point':
                m_cfg['fpp_current'] = widget.fpp_current.value()
                m_cfg['fpp_voltage_compliance'] = widget.fpp_voltage_compliance.value()
                m_cfg['fpp_voltage_range_auto'] = widget.fpp_voltage_range_auto.isChecked()
                m_cfg['fpp_spacing_cm'] = widget.fpp_spacing_cm.value()
                m_cfg['fpp_thickness_um'] = widget.fpp_thickness_um.value()
                m_cfg['fpp_alpha'] = widget.fpp_alpha.value()
                m_cfg['fpp_model'] = widget.fpp_model.currentText()
                m_cfg['fpp_k_factor'] = widget.fpp_k_factor.value()
        except AttributeError as e:
            raise ValueError(f"UI Widgets not found for mode {mode}: {e}")
        m_cfg['sampling_rate'] = self.user_settings['measurement']['sampling_rate']
        m_cfg['nplc'] = self.user_settings['measurement']['nplc']
        m_cfg['settling_time'] = self.user_settings['measurement']['settling_time']
        m_cfg['gpib_address'] = self.user_settings['measurement']['gpib_address']
        return effective_settings

    def start_measurement(self, mode: str):
        if self.measurement_running:
            QMessageBox.warning(self, "Measurement Active", f"A measurement ({self.active_mode}) is already running. Please stop it first.")
            return
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select or create a user first.")
            return
        sample_name = self.sample_input.text().strip()
        if not sample_name:
            self.sample_input.setFocus(); QMessageBox.warning(self, "Sample Name Required", "Please enter a sample name.")
            return
        widget = self.get_widget_for_mode(mode)
        if not widget:
            self.log_status(f"Error: Could not find UI for mode {mode}"); return
        try:
            current_settings = self.gather_settings_for_mode(mode)
        except ValueError as e:
            QMessageBox.critical(self, "Settings Error", f"Failed to gather settings: {e}")
            return
        self.active_mode = mode; self.measurement_running = True
        self.set_controls_for_mode(mode, running=True)
        self.set_all_controls_enabled(False, except_mode=mode)
        self.sample_input.setEnabled(False); self.change_user_button.setEnabled(False)
        self.shortcut_mark.setEnabled(True)
        self.data_buffers[mode].clear(); widget.canvas.clear_plot()
        widget.status_label.setText("Status: Running"); widget.status_label.setStyleSheet("font-weight: bold; color: green;")
        if hasattr(widget, 'mark_event_button'): widget.mark_event_button.setEnabled(True)
        self.log_status(f"Starting {mode} measurement for sample: {sample_name}..."); self.statusBar().showMessage(f"Measurement running ({mode})...")
        self.measurement_worker = MeasurementWorker(mode=mode, sample_name=sample_name, username=self.current_user, settings=current_settings)
        self.measurement_worker.data_point.connect(self.update_data)
        self.measurement_worker.status_update.connect(self.log_status_from_worker)
        self.measurement_worker.measurement_complete.connect(self.on_measurement_complete)
        self.measurement_worker.error_occurred.connect(self.on_error)
        self.measurement_worker.compliance_hit.connect(self.on_compliance_hit)
        self.measurement_worker.finished.connect(self.on_worker_finished)
        self.measurement_worker.start()
        update_interval = current_settings['display']['plot_update_interval']
        if current_settings['display']['enable_plot']:
            self.plot_timer.start(update_interval)
        else:
            self.log_status("Plotting disabled in settings.")

    def stop_current_measurement(self):
        if self.measurement_worker and self.measurement_running:
            self.log_status(f"Attempting to stop {self.active_mode} measurement...")
            self.statusBar().showMessage(f"Stopping {self.active_mode} measurement...")
            widget = self.get_widget_for_mode(self.active_mode)
            if widget:
                widget.stop_button.setEnabled(False)
                widget.status_label.setText("Status: Stopping...")
                widget.status_label.setStyleSheet("font-weight: bold; color: orange;")
                if hasattr(widget, 'mark_event_button'):
                    widget.mark_event_button.setEnabled(False)
                if hasattr(widget, 'pause_button'):
                    widget.pause_button.setEnabled(False)
            self.shortcut_mark.setEnabled(False)
            self.plot_timer.stop()
            self.measurement_worker.stop_measurement()
        else:
            self.log_status("No measurement currently running.")

    def pause_resume_measurement(self, pause: bool):
        if not self.measurement_running or not self.measurement_worker:
            return
        widget = self.get_widget_for_mode(self.active_mode)
        if not widget:
            return
        if pause:
            self.measurement_worker.pause_measurement()
            widget.pause_button.setText("Resume"); widget.pause_button.setIcon(QIcon.fromTheme("media-playback-start"))
            widget.status_label.setText("Status: Paused"); widget.status_label.setStyleSheet("font-weight: bold; color: blue;")
        else:
            self.measurement_worker.resume_measurement()
            widget.pause_button.setText("Pause"); widget.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))
            widget.status_label.setText("Status: Running"); widget.status_label.setStyleSheet("font-weight: bold; color: green;")

    def mark_event_shortcut(self):
        if self.measurement_running and self.measurement_worker:
            self.measurement_worker.mark_event("MARK")
            self.log_status("⭐ Event marked.", color="purple")
            widget = self.get_widget_for_mode(self.active_mode)
            if widget and hasattr(widget, 'mark_event_button'):
                original_style = widget.mark_event_button.styleSheet()
                widget.mark_event_button.setStyleSheet("background-color: yellow;")
                QTimer.singleShot(500, lambda: widget.mark_event_button.setStyleSheet(original_style))

    def update_data(self, timestamp: float, value: Dict[str, float], compliance_status: str, event: str):
        if not self.measurement_running or self.active_mode is None:
            return
        buffer = self.data_buffers[self.active_mode]
        if 'resistance' in value and ('voltage' not in value and 'current' not in value):
            buffer.add_resistance(timestamp, value.get('resistance', float('nan')), compliance_status)
        else:
            buffer.add_voltage_current(timestamp, value.get('voltage', float('nan')), value.get('current', float('nan')), compliance_status)

        # Append a row to 4PP table and update stats live
        if self.active_mode == 'four_point':
            w = self.tab_four_point
            v = value.get('voltage', float('nan'))
            i = value.get('current', float('nan'))
            ratio = (v / i) if (isinstance(i, (int, float)) and i != 0 and not np.isnan(i)) else float('nan')
            s = w.fpp_spacing_cm.value()
            t_um = w.fpp_thickness_um.value(); t_cm = t_um * 1e-4
            alpha = w.fpp_alpha.value(); k = w.fpp_k_factor.value() or 4.532
            model = w.fpp_model.currentText()
            if model == 'thin_film' and alpha and alpha != 1.0:
                rs = (k * alpha * ratio) if np.isfinite(ratio) else float('nan')
            else:
                rs = (k * ratio) if np.isfinite(ratio) else float('nan')
            if model == 'semi_infinite':
                rho = 2*np.pi*s*ratio if np.isfinite(ratio) else float('nan')
            elif model in ('thin_film','finite_thin'):
                k_eff = k * (alpha if (model == 'thin_film' and alpha and alpha != 1.0) else 1.0)
                rho = (k_eff * t_cm * ratio) if np.isfinite(ratio) else float('nan')
            else:
                rho = (alpha * 2*np.pi*s*ratio) if np.isfinite(ratio) else float('nan')
            sigma = (1.0 / rho) if (np.isfinite(rho) and rho != 0) else float('nan')
            # Elapsed time relative to first timestamp in buffer
            ts, _, _ = buffer.get_data_for_plot('voltage')
            elapsed = (timestamp - ts[0]) if ts else 0.0
            row = (elapsed, v, i, ratio, rs, rho, sigma, compliance_status, event)
            w._fpp_rows.append(row)
            self._append_four_point_row(row)
            self._update_four_point_stats()

    def update_active_plot(self):
        if not self.measurement_running or self.active_mode is None or not self.user_settings:
            return
        mode = self.active_mode; widget = self.get_widget_for_mode(mode); buffer = self.data_buffers[mode]
        if not widget or not buffer:
            return
        if mode == 'four_point' and hasattr(widget, 'fpp_show_plot') and not widget.fpp_show_plot.isChecked():
            return
        if self.user_settings['display']['enable_plot']:
            if mode == 'resistance':
                var = 'resistance'
                timestamps, values, compliance_list = buffer.get_data_for_plot(var)
                stats = buffer.get_statistics(var)
            elif mode == 'source_v':
                var = widget.v_plot_var.currentText() if hasattr(widget, 'v_plot_var') else 'current'
                timestamps, values, compliance_list = buffer.get_data_for_plot(var)
                stats = buffer.get_statistics(var)
            elif mode == 'source_i':
                var = widget.i_plot_var.currentText() if hasattr(widget, 'i_plot_var') else 'voltage'
                timestamps, values, compliance_list = buffer.get_data_for_plot(var)
                stats = buffer.get_statistics(var)
            else:  # four_point
                # derive variables from V and I
                t, vvals, cl = buffer.get_data_for_plot('voltage')
                _, ivals, _ = buffer.get_data_for_plot('current')
                var = widget.fpp_plot_var.currentText() if hasattr(widget, 'fpp_plot_var') else 'sheet_Rs'
                ratio = []
                for v, i in zip(vvals, ivals):
                    if isinstance(i, (int, float)) and i != 0 and not np.isnan(i):
                        ratio.append(v / i)
                    else:
                        ratio.append(float('nan'))
                s = self.tab_four_point.fpp_spacing_cm.value()
                t_um = self.tab_four_point.fpp_thickness_um.value()
                t_thick = t_um * 1e-4  # convert µm to cm
                k_factor = self.tab_four_point.fpp_k_factor.value() or 4.532
                alpha = self.tab_four_point.fpp_alpha.value()
                model = self.tab_four_point.fpp_model.currentText()
                if var == 'V/I':
                    values = ratio
                elif var == 'sheet_Rs':
                    # Allow custom correction (alpha) and K factor in thin_film mode
                    k_eff = k_factor * (alpha if (model == 'thin_film' and alpha and alpha != 1.0) else 1.0)
                    values = [k_eff * r if np.isfinite(r) else float('nan') for r in ratio]
                elif var == 'rho':
                    if model == 'semi_infinite':
                        values = [2*np.pi*s*r if np.isfinite(r) else float('nan') for r in ratio]
                    elif model in ('thin_film','finite_thin'):
                        # Apply alpha for thin_film when provided and use K factor
                        k = k_factor * (alpha if (model == 'thin_film' and alpha and alpha != 1.0) else 1.0)
                        values = [k * t_thick * r if np.isfinite(r) else float('nan') for r in ratio]
                    else:
                        values = [alpha * 2*np.pi*s*r if np.isfinite(r) else float('nan') for r in ratio]
                elif var == 'voltage':
                    values = vvals
                else:
                    values = ivals
                timestamps = t; compliance_list = cl
                stats = {
                    'min': np.nanmin(values) if values else float('inf'),
                    'max': np.nanmax(values) if values else float('-inf'),
                    'avg': np.nanmean(values) if values else 0.0,
                }
            widget.canvas.update_plot(timestamps, values, compliance_list, stats, self.current_user, self.sample_input.text())

    def _append_four_point_row(self, row):
        w = self.tab_four_point
        table = w.fpp_table
        table.insertRow(table.rowCount())
        for col, val in enumerate(row):
            if isinstance(val, float):
                text = f"{val:.6g}"
            else:
                text = str(val)
            table.setItem(table.rowCount()-1, col, QTableWidgetItem(text))
        table.scrollToBottom()

    def _update_four_point_stats(self):
        w = self.tab_four_point
        rows = w._fpp_rows
        n = len(rows)
        w.fpp_n_label.setText(str(n))
        import math
        def stats(idx):
            arr = [r[idx] for r in rows]
            arr = [a for a in arr if isinstance(a, (int, float)) and not math.isnan(a)]
            if not arr:
                return None
            import numpy as np
            mean = float(np.mean(arr)); std = float(np.std(arr, ddof=1)) if len(arr)>1 else 0.0
            rsd = (std/mean*100.0) if mean != 0 else 0.0
            return mean, std, rsd
        rs_s = stats(4); rho_s = stats(5); sig_s = stats(6)
        def fmt(s):
            return f"{s[0]:.6g} ± {s[1]:.6g}  ({s[2]:.2f}%)" if s else "--"
        w.fpp_rs_label.setText(fmt(rs_s))
        w.fpp_rho_label.setText(fmt(rho_s))
        w.fpp_sigma_label.setText(fmt(sig_s))

    def update_four_point_model_info(self, w=None, *args):
        # Robustly resolve the 4PP widget whether called with a widget, a value from a signal, or no args.
        if w is None or not hasattr(w, 'fpp_spacing_cm'):
            w = getattr(self, 'tab_four_point', None)
        if w is None or not hasattr(w, 'fpp_spacing_cm'):
            return
        s = w.fpp_spacing_cm.value(); t_um = w.fpp_thickness_um.value(); t_cm = t_um*1e-4
        k = w.fpp_k_factor.value() or 4.532; alpha = w.fpp_alpha.value(); model = w.fpp_model.currentText()
        txt = ""
        if model == 'semi_infinite':
            txt = f"ρ = 2π·s·(V/I) = {2*3.1416*s:.4g}·(V/I) Ω·cm"
        elif model in ('thin_film','finite_thin'):
            # Show both Rs and rho forms
            txt = f"Rs = {k:.4g}·(V/I) Ω/□\nρ = {k:.4g}·t·(V/I) = {k*t_cm:.4g}·(V/I) Ω·cm"
            if model == 'thin_film' and alpha and alpha != 1.0:
                txt += f"\n(α applied: Rs = {k*alpha:.4g}·(V/I), ρ = {k*alpha:.4g}·t·(V/I))"
        else:
            txt = f"ρ = α·2π·s·(V/I) = α·{2*3.1416*s:.4g}·(V/I) Ω·cm"
        w.fpp_model_info.setText(txt)

    def on_measurement_complete(self, mode: str):
        self.log_status(f"Worker reported measurement complete for mode: {mode}", color="darkGreen")
        self.statusBar().showMessage(f"Measurement ({mode}) completed | Ready", 5000)

    def on_error(self, error_message: str):
        self.log_status(f"ERROR: {error_message}", color="red")
        self.statusBar().showMessage(f"Measurement Error ({self.active_mode})", 5000)
        self.plot_timer.stop()
        # If instrument not detected, prompt for quick selection
        if ("not found" in error_message.lower()) or ("no visa instruments" in error_message.lower()):
            self.prompt_gpib_selection(self.user_settings['measurement']['gpib_address'] if self.user_settings else "")
        else:
            QMessageBox.critical(self, "Measurement Error", error_message)

    def on_compliance_hit(self, compliance_type: str):
        mode = self.active_mode; widget = self.get_widget_for_mode(mode)
        if widget:
            widget.status_label.setText(f"Status: {compliance_type.upper()} COMPLIANCE")
            widget.status_label.setStyleSheet("font-weight: bold; color: red;")
        self.log_status(f"⚠️ {compliance_type} Compliance Hit during {mode} measurement!", color="orange")
        QMessageBox.warning(self, f"{compliance_type} Compliance Warning", f"The {compliance_type.lower()} compliance limit was reached during the {mode} measurement.")

    def on_worker_finished(self):
        self.log_status(f"Measurement worker thread ({self.active_mode}) finished.", color="grey")
        self.reset_ui_after_measurement()

    def reset_ui_after_measurement(self):
        if not self.active_mode:
            return
        finished_mode = self.active_mode
        self.measurement_running = False
        self.active_mode = None
        self.measurement_worker = None
        self.plot_timer.stop()
        self.sample_input.setEnabled(True); self.change_user_button.setEnabled(True)
        widget = self.get_widget_for_mode(finished_mode)
        if widget:
            widget.status_label.setText("Status: Idle"); widget.status_label.setStyleSheet("font-weight: bold; color: black;")
            widget.start_button.setEnabled(True); widget.stop_button.setEnabled(False)
            if hasattr(widget, 'pause_button'):
                widget.pause_button.setEnabled(False); widget.pause_button.setChecked(False)
            if hasattr(widget, 'mark_event_button'):
                widget.mark_event_button.setEnabled(False)
        self.set_all_controls_enabled(True)
        self.shortcut_mark.setEnabled(False)
        self.statusBar().showMessage("Ready", 0); self.log_status("Measurement stopped. UI controls re-enabled.")

    def set_controls_for_mode(self, mode: str, running: bool):
        widget = self.get_widget_for_mode(mode)
        if widget:
            widget.start_button.setEnabled(not running); widget.stop_button.setEnabled(running)
            if hasattr(widget, 'pause_button'):
                widget.pause_button.setEnabled(running)
            for i in range(widget.param_layout.rowCount()):
                field = widget.param_layout.itemAt(i, QFormLayout.FieldRole)
                if field and field.widget(): field.widget().setEnabled(not running)
                label = widget.param_layout.itemAt(i, QFormLayout.LabelRole)
                if label and label.widget(): label.widget().setEnabled(not running)
            if hasattr(widget, 'mark_event_button'):
                widget.mark_event_button.setEnabled(running)

    def set_all_controls_enabled(self, enabled: bool, except_mode: Optional[str] = None):
        for mode in ['resistance', 'source_v', 'source_i', 'four_point']:
            if mode == except_mode: continue
            widget = self.get_widget_for_mode(mode)
            if widget:
                widget.start_button.setEnabled(enabled); widget.stop_button.setEnabled(False)
                if hasattr(widget, 'pause_button'):
                    widget.pause_button.setEnabled(False); widget.pause_button.setChecked(False)
                if hasattr(widget, 'mark_event_button'):
                    widget.mark_event_button.setEnabled(False)
                for i in range(widget.param_layout.rowCount()):
                    field = widget.param_layout.itemAt(i, QFormLayout.FieldRole)
                    if field and field.widget(): field.widget().setEnabled(enabled)
                    label = widget.param_layout.itemAt(i, QFormLayout.LabelRole)
                    if label and label.widget(): label.widget().setEnabled(enabled)

    def handle_tab_change(self, index):
        if self.measurement_running:
            current_widget = self.main_tabs.widget(index)
            if not hasattr(current_widget, 'mode') or current_widget.mode != self.active_mode:
                QMessageBox.warning(self, "Measurement Active", f"Cannot switch tabs while a measurement ({self.active_mode}) is running. Please stop the current measurement first.")
                for i in range(self.main_tabs.count()):
                    widget = self.main_tabs.widget(i)
                    if hasattr(widget, 'mode') and widget.mode == self.active_mode:
                        self.main_tabs.blockSignals(True); self.main_tabs.setCurrentIndex(i); self.main_tabs.blockSignals(False); break

    def export_fpp_summary(self):
        # Export summary for 4-point probe using current buffer and settings
        buffer = self.data_buffers.get('four_point')
        if not buffer:
            QMessageBox.information(self, "Export Summary", "4-point probe buffer not available.")
            return
        t, vvals, _ = buffer.get_data_for_plot('voltage')
        _, ivals, _ = buffer.get_data_for_plot('current')
        if not t:
            QMessageBox.information(self, "Export Summary", "No data available for 4-point probe.")
            return
        ratio = []
        for v, i in zip(vvals, ivals):
            if isinstance(i, (int, float)) and i != 0 and not np.isnan(i):
                ratio.append(v / i)
            else:
                ratio.append(float('nan'))
        s = self.tab_four_point.fpp_spacing_cm.value()
        t_um = self.tab_four_point.fpp_thickness_um.value()
        t_thick = t_um * 1e-4
        alpha = self.tab_four_point.fpp_alpha.value()
        model = self.tab_four_point.fpp_model.currentText()
        k_factor = self.tab_four_point.fpp_k_factor.value() or 4.532
        if model == 'thin_film' and alpha and alpha != 1.0:
            Rs = np.array([k_factor * alpha * r if np.isfinite(r) else np.nan for r in ratio])
        else:
            Rs = np.array([k_factor * r if np.isfinite(r) else np.nan for r in ratio])
        if model == 'semi_infinite':
            rho = np.array([2*np.pi*s*r if np.isfinite(r) else np.nan for r in ratio])
        elif model in ('thin_film','finite_thin'):
            k = k_factor * (alpha if (model == 'thin_film' and alpha and alpha != 1.0) else 1.0)
            rho = np.array([k * t_thick * r if np.isfinite(r) else np.nan for r in ratio])
        else:
            rho = np.array([alpha * 2*np.pi*s*r if np.isfinite(r) else np.nan for r in ratio])
        sigma = np.where(np.isfinite(rho) & (rho != 0), 1.0 / rho, np.nan)
        def stat(a):
            return float(np.nanmean(a)), float(np.nanstd(a))
        Rs_mean, Rs_std = stat(Rs)
        rho_mean, rho_std = stat(rho)
        sigma_mean, sigma_std = stat(sigma)
        filename, _ = QFileDialog.getSaveFileName(self, "Save Summary", "fpp_summary.csv", "CSV Files (*.csv)")
        if not filename:
            return
        import csv
        try:
            with open(filename, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(["4-Point Probe Summary"])
                w.writerow(["Sample", self.sample_input.text()])
                w.writerow(["User", self.current_user or "-"])
                w.writerow(["Model", model])
                w.writerow(["Spacing s (cm)", s])
                w.writerow(["Thickness t (cm)", t_thick])
                w.writerow(["Alpha", alpha])
                w.writerow([])
                w.writerow(["Metric", "Mean", "StdDev"])
                w.writerow(["Sheet Resistance (Ω/□)", f"{Rs_mean:.6g}", f"{Rs_std:.6g}"])
                w.writerow(["Resistivity (Ω·cm)", f"{rho_mean:.6g}", f"{rho_std:.6g}"])
                w.writerow(["Conductivity (S/cm)", f"{sigma_mean:.6g}", f"{sigma_std:.6g}"])
            self.log_status(f"Summary saved: {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Save Summary", f"Failed to save summary: {e}")

    def log_status(self, message: str, color: str = "black"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        colored_message = f'<font color="{color}">[{timestamp}] {message}</font>'
        self.status_display.append(colored_message)
        self.status_display.verticalScrollBar().setValue(self.status_display.verticalScrollBar().maximum())

    def log_status_from_worker(self, message: str):
        color = "black"
        if "error" in message.lower(): color="red"
        elif "warn" in message.lower() or "compliance" in message.lower(): color="orange"
        self.log_status(message, color=color)
        self.statusBar().showMessage(message, 3000)

    def save_active_plot(self):
        current_tab_widget = self.main_tabs.currentWidget()
        if not hasattr(current_tab_widget, 'canvas'):
            QMessageBox.warning(self, "Save Error", "Could not find plot canvas on the current tab.")
            return
        mode = getattr(current_tab_widget, 'mode', 'unknown')
        sample_name = self.sample_input.text().strip().replace(' ','_') or "plot"
        timestamp = int(time.time())
        suggested = f"{timestamp}_{sample_name}_{mode}.png"
        default_dir = self.user_settings['file']['data_directory'] if self.user_settings else "."
        filename, _ = QFileDialog.getSaveFileName(self, "Save Plot", f"{default_dir}/{suggested}", "PNG Files (*.png);;PDF Files (*.pdf);;JPEG Files (*.jpg);;All Files (*)")
        if filename:
            try:
                current_tab_widget.canvas.fig.savefig(filename, dpi=300)
                self.log_status(f"Plot saved to: {filename}", color="blue")
            except Exception as e:
                QMessageBox.critical(self, "Save Error", f"Failed to save plot: {str(e)}"); self.log_status(f"Error saving plot: {str(e)}", color="red")

    def prompt_gpib_selection(self, current_addr: str):
        try:
            import pyvisa
            rm = pyvisa.ResourceManager()
            resources = rm.list_resources()
        except Exception as e:
            QMessageBox.information(self, "GPIB Detection", f"Failed to list VISA resources: {e}")
            return
        if not resources:
            QMessageBox.information(self, "GPIB Detection", "No VISA instruments detected.")
            return
        # Simple selection dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Select GPIB Instrument")
        v = QVBoxLayout(dialog)
        v.addWidget(QLabel("Select instrument address:"))
        combo = QComboBox(dialog); combo.addItems(resources)
        if current_addr in resources:
            combo.setCurrentText(current_addr)
        v.addWidget(combo)
        use_btn = QPushButton("Use Address")
        use_btn.clicked.connect(dialog.accept)
        v.addWidget(use_btn)
        if dialog.exec_():
            addr = combo.currentText()
            # Save to global settings
            cfg = dict(self.config_manager.config)
            cfg['measurement']['gpib_address'] = addr
            self.config_manager.update_global_settings({'measurement': {'gpib_address': addr}})
            self.log_status(f"GPIB address set to: {addr}")
            QMessageBox.information(self, "GPIB Updated", f"GPIB address updated to {addr}. Start the measurement again.")

    def clear_all_plots(self):
        self.tab_resistance.canvas.clear_plot(); self.tab_voltage_source.canvas.clear_plot(); self.tab_current_source.canvas.clear_plot(); self.log_status("All plots cleared.")

    def show_about(self):
        about_text = f"""
        <h2>ResistaMet GUI (Tabbed)</h2>
        <p>Version: {__version__}</p>
        <p>Original Author: Brenden Ferland</p>
        <hr>
        <p>A graphical interface for controlling Keithley SourceMeasure Units, providing modes for:</p>
        <ul>
            <li>Resistance Measurement (Source Current, Measure Resistance)</li>
            <li>Voltage Source (Source Voltage, Measure Current)</li>
            <li>Current Source (Source Current, Measure Voltage)</li>
        </ul>
        <p>Supports real-time plotting, data logging, user profiles, and compliance monitoring.</p>
        """
        QMessageBox.about(self, f"About ResistaMet GUI v{__version__}", about_text)

    def closeEvent(self, event):
        if self.measurement_running:
            reply = QMessageBox.question(self, "Exit Confirmation", f"A measurement ({self.active_mode}) is currently running.\nStopping may result in incomplete data.\n\nAre you sure you want to exit?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.log_status("Exit requested during measurement. Stopping worker...", color="orange")
                if self.measurement_worker:
                    self.measurement_worker.stop_measurement()
                    if not self.measurement_worker.wait(2000):
                        self.log_status("Worker did not stop gracefully. Forcing exit.", color="red")
                event.accept()
            else:
                event.ignore()
        else:
            self.log_status("Exiting application."); event.accept()
