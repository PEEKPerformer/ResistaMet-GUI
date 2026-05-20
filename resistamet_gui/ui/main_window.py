import logging
import time
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAction, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QShortcut,
    QTextEdit, QTabWidget, QVBoxLayout, QWidget, QFileDialog, QSplitter, QTableWidget, QTableWidgetItem,
    QDialog, QSpinBox, QSizePolicy, QInputDialog
)
from PyQt5.QtGui import QIcon, QFont, QBrush, QColor
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar

from ..buffers import EnhancedDataBuffer
from ..config import ConfigManager
from ..constants import __version__
from ..workers import MeasurementWorker, VdpMeasurementWorker
from .canvas import MplCanvas, HistogramCanvas, IVCanvas
from .widgets import EngineeringSpinBox, NoScrollSpinBox, NoScrollIntSpinBox, VdpSampleDiagram, VdpProtocolFilmstrip, VdpPerGeometryBarChart, format_engineering
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
            'sweep': EnhancedDataBuffer(),
        }
        self.measurement_worker = None
        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.update_active_plot)
        self.current_user = None
        self.user_settings = None
        self.measurement_running = False
        self.active_mode = None
        self.setWindowTitle(f"ResistaMet GUI v{__version__}")
        # 720×560 fits comfortably on 1366×768 laptops after taskbar/title-bar
        # chrome. The horizontal-splitter layout makes this floor reachable;
        # the previous 900×700 was tied to the old vertical-splitter layout.
        self.setMinimumSize(720, 560)
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
        self.tab_sweep = self.create_sweep_tab()
        self.tab_vdp = self.create_vdp_tab()
        self.main_tabs.addTab(self.tab_resistance, "Resistance Measurement")
        self.main_tabs.addTab(self.tab_voltage_source, "Voltage Source")
        self.main_tabs.addTab(self.tab_current_source, "Current Source")
        self.main_tabs.addTab(self.tab_four_point, "4-Point Probe")
        self.main_tabs.addTab(self.tab_sweep, "I-V Sweep")
        self.main_tabs.addTab(self.tab_vdp, "Van der Pauw")

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
        self.shortcut_mark = QShortcut(Qt.Key_M, self); self.shortcut_mark.activated.connect(self.mark_event_shortcut)
        self.shortcut_mark.setEnabled(False)

        # Cmd/Ctrl + 1..6 jumps to a tab. Match macOS / browser convention.
        for idx, key in enumerate(
            (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5, Qt.Key_6)
        ):
            sc = QShortcut(Qt.ControlModifier | key, self)
            sc.activated.connect(lambda i=idx: self.main_tabs.setCurrentIndex(i))

    @staticmethod
    def _wrap_in_scroll(widget: QWidget) -> QScrollArea:
        """Wrap a widget in a frameless QScrollArea so the form scrolls when
        the available space is shorter than the widget's preferred height."""
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        return scroll

    @staticmethod
    def _form_pair(label1: str, w1: QWidget, label2: str, w2: QWidget):
        """Pack two label-field pairs onto one form row.

        Returns ``(label1_text, container)`` ready for ``form.addRow(*...)``.
        The container holds ``[w1, label2, w2]`` so a single QFormLayout row
        carries two fields. setEnabled() on the container cascades — keeps
        param_layout.itemAt(i, FieldRole) compatible with the existing
        enable/disable loop.
        """
        container = QWidget()
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(w1, 1)
        h.addWidget(QLabel(label2 + ":"), 0)
        h.addWidget(w2, 1)
        return label1 + ":", container

    @staticmethod
    def _build_live_readout(font_pt: int = 28) -> QLabel:
        """Big centered measurement readout used at the top of each tab."""
        live = QLabel("--")
        live.setAlignment(Qt.AlignCenter)
        f = QFont(); f.setPointSize(font_pt); f.setBold(True)
        live.setFont(f)
        live.setStyleSheet(
            "color: #222; background: #f0f0f0; border: 1px solid #ccc; "
            "border-radius: 4px; padding: 4px;"
        )
        live.setMinimumHeight(50)
        live.setToolTip(
            "Live measurement reading — updates in real time during measurement"
        )
        return live

    @staticmethod
    def _build_control_row(with_pause: bool = True):
        """Bottom Control row: Start / Stop / [Pause] / status_label.

        Returns ``(group, start, stop, pause_or_None, status_label)``.
        """
        group = QGroupBox("Control")
        layout = QHBoxLayout(group)
        start = QPushButton(QIcon.fromTheme("media-playback-start"), "Start")
        stop = QPushButton(QIcon.fromTheme("media-playback-stop"), "Stop")
        stop.setEnabled(False)
        pause = None
        if with_pause:
            pause = QPushButton(QIcon.fromTheme("media-playback-pause"), "Pause")
            pause.setEnabled(False)
            pause.setCheckable(True)
        status_label = QLabel("Status: Idle")
        status_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(start)
        layout.addWidget(stop)
        if pause is not None:
            layout.addWidget(pause)
        layout.addStretch()
        layout.addWidget(status_label)
        return group, start, stop, pause, status_label

    def create_tab_widget(self, mode: str) -> QWidget:
        """Time-series tab layout: Parameters left, plot right; live readout
        and Control row stacked beneath. Matches the I-V Sweep / 4PP pattern
        so all five tabs share the same shape."""
        tab_widget = QWidget()
        tab_widget.mode = mode
        tab_layout = QVBoxLayout(tab_widget)

        # Parameters (left column). No QScrollArea wrap — its small default
        # sizeHint causes the splitter to collapse the params pane to a few
        # px, forcing horizontal scrollbars. Letting the QGroupBox report its
        # real sizeHint is enough; the form is only ~9 rows.
        param_group = QGroupBox("Parameters")
        param_layout = QFormLayout()
        param_group.setLayout(param_layout)

        # Plot (right column)
        plot_group = QGroupBox("Real-time Data")
        plot_layout = QVBoxLayout(plot_group)
        canvas = MplCanvas(self, width=8, height=5, dpi=90)
        toolbar = NavigationToolbar(canvas, self)
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(canvas)

        # Live readout + Control row (full-width, beneath the splitter)
        live_readout = self._build_live_readout()
        control_group, start_button, stop_button, pause_button, status_label = (
            self._build_control_row(with_pause=True)
        )

        # Horizontal splitter — params left, plot right
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(param_group)
        splitter.addWidget(plot_group)
        splitter.setStretchFactor(0, 0)  # params keeps its sizeHint width
        splitter.setStretchFactor(1, 1)  # plot eats all extra space
        splitter.setChildrenCollapsible(False)

        tab_layout.addWidget(splitter, 1)
        tab_layout.addWidget(live_readout, 0)
        tab_layout.addWidget(control_group, 0)

        # Stash references used by other methods
        tab_widget.param_layout = param_layout
        tab_widget.canvas = canvas
        tab_widget.start_button = start_button
        tab_widget.stop_button = stop_button
        tab_widget.pause_button = pause_button
        tab_widget.status_label = status_label
        tab_widget.live_readout = live_readout
        tab_widget.param_group = param_group
        tab_widget.plot_group = plot_group
        tab_widget.control_group = control_group
        tab_widget.splitter = splitter
        return tab_widget

    def create_resistance_tab(self):
        widget = self.create_tab_widget('resistance'); layout = widget.param_layout
        widget.res_test_current = EngineeringSpinBox(unit='A', minimum=1e-7, maximum=3.0, default=1e-3)
        widget.res_test_current.setToolTip("DC current sourced through the DUT to measure resistance.\nHigher currents give better signal-to-noise but may heat the sample.\nTypical: 1 mA for metals, 1 µA for semiconductors.\nAccepts: 1mA, 100µA, 0.001, etc.")
        widget.res_voltage_compliance = NoScrollSpinBox(decimals=2, minimum=0.1, maximum=200.0, singleStep=0.1, suffix=" V")
        widget.res_voltage_compliance.setToolTip("Maximum voltage the instrument will apply across the DUT.\nIf the DUT resistance is too high, voltage will be clamped here\nand a compliance warning will appear. Protects sensitive samples.")
        layout.addRow(*self._form_pair(
            "Test Current", widget.res_test_current,
            "V Compliance", widget.res_voltage_compliance,
        ))

        widget.res_measurement_type = QComboBox(); widget.res_measurement_type.addItems(["2-wire", "4-wire"])
        widget.res_measurement_type.setToolTip("2-wire: Simple connection, includes lead resistance.\n4-wire (Kelvin): Separate sense leads eliminate lead resistance.\nUse 4-wire for low-resistance DUTs (<10 Ω) or precision work.")
        widget.sampling_rate = NoScrollSpinBox(decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz")
        widget.sampling_rate.setToolTip("How many readings per second to take.\nLimited by NPLC — high NPLC with high rate will bottleneck\nat the instrument's actual measurement speed.")
        layout.addRow(*self._form_pair(
            "Measurement Type", widget.res_measurement_type,
            "Sampling Rate", widget.sampling_rate,
        ))

        widget.res_auto_range = QCheckBox("Auto Range Resistance")
        widget.res_auto_range.setToolTip("When checked, the instrument automatically selects the best\nmeasurement range for the DUT resistance. Disable for faster\nmeasurements at a fixed range.")
        widget.res_offset_comp = QCheckBox("Offset Compensated Ohms")
        widget.res_offset_comp.setToolTip("Cancels thermoelectric EMF by automatically measuring with\ncurrent ON and OFF, then subtracting. Halves measurement\nspeed but improves accuracy for low-resistance DUTs.")
        cb_row = QWidget(); cb_h = QHBoxLayout(cb_row)
        cb_h.setContentsMargins(0, 0, 0, 0)
        cb_h.addWidget(widget.res_auto_range)
        cb_h.addWidget(widget.res_offset_comp)
        cb_h.addStretch()
        layout.addRow("", cb_row)

        # Cable null row
        null_layout = QHBoxLayout()
        widget.null_cables_btn = QPushButton("Null Cables")
        widget.null_cables_btn.setToolTip("Short the probes together, then click to measure and subtract\ncable resistance from all future readings (2-wire mode).")
        widget.null_cables_btn.clicked.connect(self._null_cables)
        widget.clear_null_btn = QPushButton("Clear Null")
        widget.clear_null_btn.setToolTip("Remove cable null reference.")
        widget.clear_null_btn.clicked.connect(self._clear_cable_null)
        widget.null_label = QLabel("Cable null: OFF")
        widget.null_label.setStyleSheet("color: grey;")
        null_layout.addWidget(widget.null_cables_btn)
        null_layout.addWidget(widget.clear_null_btn)
        null_layout.addWidget(widget.null_label)
        null_layout.addStretch()
        layout.addRow("Cable Null:", null_layout)

        # Action buttons (Mark Event + Test Connection on one row)
        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setToolTip("Insert a named event marker into the data stream (keyboard shortcut: M).\nUseful for annotating probe moves, temperature changes, etc.")
        widget.mark_event_button.setEnabled(False)
        test_conn_button = QPushButton("Test Connection")
        test_conn_button.setToolTip("Check if the instrument is reachable at the configured GPIB address\nbefore starting a measurement.")
        test_conn_button.clicked.connect(self.test_instrument_connection)
        action_row = QWidget(); action_h = QHBoxLayout(action_row)
        action_h.setContentsMargins(0, 0, 0, 0)
        action_h.addWidget(widget.mark_event_button)
        action_h.addWidget(test_conn_button)
        layout.addRow("", action_row)
        widget.start_button.clicked.connect(lambda: self.start_measurement('resistance'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        return widget

    def create_voltage_source_tab(self):
        widget = self.create_tab_widget('source_v'); layout = widget.param_layout
        widget.vsource_voltage = EngineeringSpinBox(unit='V', minimum=-200.0, maximum=200.0, default=1.0, allow_negative=True)
        widget.vsource_voltage.setToolTip("DC voltage applied to the DUT.\nNegative values reverse polarity. The instrument will source\nthis exact voltage and measure the resulting current.\nAccepts: 100mV, 1V, -0.5V, etc.")
        widget.vsource_current_compliance = EngineeringSpinBox(unit='A', minimum=1e-7, maximum=3.0, default=0.1)
        widget.vsource_current_compliance.setToolTip("Maximum current allowed to flow through the DUT.\nIf the DUT draws more than this, the instrument limits current\nand reports compliance. Protects the DUT from overcurrent.\nAccepts: 100mA, 1mA, 0.1A, etc.")
        layout.addRow(*self._form_pair(
            "Source Voltage", widget.vsource_voltage,
            "I Compliance", widget.vsource_current_compliance,
        ))

        widget.vsource_duration = NoScrollSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        widget.vsource_duration.setToolTip("How long to run the measurement.\nSet a specific duration, or check 'Run until stopped'.")
        widget.sampling_rate = NoScrollSpinBox(decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz")
        widget.sampling_rate.setToolTip("How many readings per second to take.")
        layout.addRow(*self._form_pair(
            "Duration", widget.vsource_duration,
            "Sampling Rate", widget.sampling_rate,
        ))

        widget.vsource_current_range_auto = QCheckBox("Auto Range Current")
        widget.vsource_current_range_auto.setToolTip("Automatically select the best current measurement range.\nDisable for faster measurements when you know the expected range.")
        widget.vsource_run_continuous = QCheckBox("Run until stopped")
        widget.vsource_run_continuous.setToolTip("When checked, measurement runs indefinitely until you press Stop.")
        widget.vsource_run_continuous.setChecked(True)
        widget.vsource_duration.setEnabled(False)
        widget.vsource_run_continuous.toggled.connect(lambda c: widget.vsource_duration.setEnabled(not c))
        cb_row = QWidget(); cb_h = QHBoxLayout(cb_row)
        cb_h.setContentsMargins(0, 0, 0, 0)
        cb_h.addWidget(widget.vsource_current_range_auto)
        cb_h.addWidget(widget.vsource_run_continuous)
        cb_h.addStretch()
        layout.addRow("", cb_row)

        widget.v_plot_var = QComboBox(); widget.v_plot_var.addItems(["current", "voltage", "resistance"])
        widget.v_plot_var.setToolTip("Which measurement variable to plot in real time.")
        layout.addRow("Plot Variable:", widget.v_plot_var)

        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setToolTip("Insert a named event marker into the data stream (keyboard shortcut: M).")
        widget.mark_event_button.setEnabled(False)
        test_conn_button = QPushButton("Test Connection")
        test_conn_button.setToolTip("Check if the instrument is reachable before starting.")
        test_conn_button.clicked.connect(self.test_instrument_connection)
        action_row = QWidget(); action_h = QHBoxLayout(action_row)
        action_h.setContentsMargins(0, 0, 0, 0)
        action_h.addWidget(widget.mark_event_button)
        action_h.addWidget(test_conn_button)
        layout.addRow("", action_row)
        widget.start_button.clicked.connect(lambda: self.start_measurement('source_v'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.v_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('source_v'))
        return widget

    def create_current_source_tab(self):
        widget = self.create_tab_widget('source_i'); layout = widget.param_layout
        widget.isource_current = EngineeringSpinBox(unit='A', minimum=-3.0, maximum=3.0, default=1e-3, allow_negative=True)
        widget.isource_current.setToolTip("DC current sourced through the DUT.\nNegative values reverse polarity. The instrument measures\nthe resulting voltage across the DUT.\nAccepts: 1mA, -100µA, 0.001, etc.")
        widget.isource_voltage_compliance = NoScrollSpinBox(decimals=2, minimum=0.1, maximum=200.0, singleStep=0.1, suffix=" V")
        widget.isource_voltage_compliance.setToolTip("Maximum voltage the instrument will apply.\nIf the DUT resistance causes voltage to exceed this,\nthe instrument clamps and reports compliance.")
        layout.addRow(*self._form_pair(
            "Source Current", widget.isource_current,
            "V Compliance", widget.isource_voltage_compliance,
        ))

        widget.isource_duration = NoScrollSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        widget.isource_duration.setToolTip("How long to run the measurement.\nSet a specific duration, or check 'Run until stopped'.")
        widget.sampling_rate = NoScrollSpinBox(decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz")
        widget.sampling_rate.setToolTip("How many readings per second to take.")
        layout.addRow(*self._form_pair(
            "Duration", widget.isource_duration,
            "Sampling Rate", widget.sampling_rate,
        ))

        widget.isource_voltage_range_auto = QCheckBox("Auto Range Voltage")
        widget.isource_voltage_range_auto.setToolTip("Automatically select the best voltage measurement range.\nDisable for faster measurements at a fixed range.")
        widget.isource_run_continuous = QCheckBox("Run until stopped")
        widget.isource_run_continuous.setToolTip("When checked, measurement runs indefinitely until you press Stop.")
        widget.isource_run_continuous.setChecked(True)
        widget.isource_duration.setEnabled(False)
        widget.isource_run_continuous.toggled.connect(lambda c: widget.isource_duration.setEnabled(not c))
        cb_row = QWidget(); cb_h = QHBoxLayout(cb_row)
        cb_h.setContentsMargins(0, 0, 0, 0)
        cb_h.addWidget(widget.isource_voltage_range_auto)
        cb_h.addWidget(widget.isource_run_continuous)
        cb_h.addStretch()
        layout.addRow("", cb_row)

        widget.i_plot_var = QComboBox(); widget.i_plot_var.addItems(["voltage", "current", "resistance"])
        widget.i_plot_var.setToolTip("Which measurement variable to plot in real time.")
        layout.addRow("Plot Variable:", widget.i_plot_var)

        widget.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        widget.mark_event_button.setToolTip("Insert a named event marker into the data stream (keyboard shortcut: M).")
        widget.mark_event_button.setEnabled(False)
        test_conn_button = QPushButton("Test Connection")
        test_conn_button.setToolTip("Check if the instrument is reachable before starting.")
        test_conn_button.clicked.connect(self.test_instrument_connection)
        action_row = QWidget(); action_h = QHBoxLayout(action_row)
        action_h.setContentsMargins(0, 0, 0, 0)
        action_h.addWidget(widget.mark_event_button)
        action_h.addWidget(test_conn_button)
        layout.addRow("", action_row)
        widget.start_button.clicked.connect(lambda: self.start_measurement('source_i'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)
        widget.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        widget.mark_event_button.clicked.connect(self.mark_event_shortcut)
        widget.i_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('source_i'))
        return widget

    def create_four_point_tab(self):
        """Create 4-Point Probe tab with robust horizontal layout: Left=Parameters, Right=Summary+Table"""
        
        # Main container - creates custom layout structure for 4PP only
        main_container = QWidget()
        main_container.mode = 'four_point'
        main_layout = QVBoxLayout(main_container)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)
        
        # TOP: Horizontal splitter - the key to using screen width effectively
        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setObjectName("fpp_top_splitter")
        top_splitter.setChildrenCollapsible(False)  # Prevent zero-size collapse on Windows
        
        # LEFT: Parameters panel (preserve existing QGroupBox structure - never touch its layout!)
        param_group = QGroupBox("Parameters")
        param_layout = QFormLayout(param_group)
        
        # RIGHT: Summary + Table panel  
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 6, 6, 6)
        right_layout.setSpacing(6)
        
        # Width minimums in "char widths" so the layout scales with DPI/font
        # instead of staying frozen at 96-DPI pixel counts.
        ch = self.fontMetrics().averageCharWidth()
        param_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        param_group.setMinimumWidth(40 * ch)

        right_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_panel.setMinimumWidth(48 * ch)
        right_panel.setMinimumHeight(20 * self.fontMetrics().height())

        top_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # right_panel's setMinimumHeight(300) keeps the splitter from collapsing;
        # an explicit splitter minimum here was forcing total height past the tab's
        # available space, causing QVBoxLayout below to overlap live_readout and
        # control_group on top of the splitter. Let the children drive the minimum.

        # Add panels to splitter — params keeps its sizeHint width, right
        # panel takes everything else.
        top_splitter.addWidget(param_group)
        top_splitter.addWidget(right_panel)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        
        # Live readout + Control row (shared chrome)
        live_readout = self._build_live_readout()
        control_group, start_button, stop_button, pause_button, status_label = (
            self._build_control_row(with_pause=True)
        )

        # Assemble main layout
        main_layout.addWidget(top_splitter, 1)
        main_layout.addWidget(live_readout, 0)
        main_layout.addWidget(control_group, 0)

        # Store references used by other methods
        main_container.param_layout = param_layout
        main_container.start_button = start_button
        main_container.stop_button = stop_button
        main_container.pause_button = pause_button
        main_container.status_label = status_label
        main_container.live_readout = live_readout
        main_container.param_group = param_group
        main_container.control_group = control_group
        main_container.splitter = top_splitter
        main_container.top_splitter = top_splitter  # legacy alias
        main_container.right_panel = right_panel
        
        # CREATE ALL 4PP-SPECIFIC WIDGETS (exactly as before)
        layout = param_layout  # Use the parameter layout for form fields
        
        # Instrument parameters
        main_container.fpp_current = EngineeringSpinBox(unit='A', minimum=-3.0, maximum=3.0, default=1e-3, allow_negative=True)
        main_container.fpp_current.setToolTip("DC current sourced through the outer two probe tips.\nThe instrument measures voltage across the inner two tips.\nTypical: 1 mA for metals, 100 µA for thin films.\nAccepts: 1mA, 100µA, 0.001, etc.")
        main_container.fpp_voltage_compliance = NoScrollSpinBox(decimals=2, minimum=0.1, maximum=200.0, singleStep=0.1, suffix=" V")
        main_container.fpp_voltage_compliance.setToolTip("Maximum voltage the instrument will apply.\nProtects samples from overvoltage damage.")
        layout.addRow(*self._form_pair(
            "Source Current", main_container.fpp_current,
            "V Compliance", main_container.fpp_voltage_compliance,
        ))

        main_container.fpp_voltage_range_auto = QCheckBox("Auto Range Voltage Measurement")
        main_container.fpp_voltage_range_auto.setToolTip("Automatically select the best voltage measurement range.")
        main_container.fpp_spacing_cm = QDoubleSpinBox(decimals=5, minimum=0.001, maximum=5.0, singleStep=0.001, suffix=" cm")
        main_container.fpp_spacing_cm.setToolTip("Distance between adjacent probe tips (s).\nFor the SP4-40085TBQ probe head: s = 0.1016 cm (40 mil).\nUsed in resistivity calculations for the semi-infinite model.")
        main_container.fpp_thickness_um = QDoubleSpinBox(decimals=3, minimum=0.0, maximum=5000.0, singleStep=0.1, suffix=" µm")
        main_container.fpp_thickness_um.setToolTip("Film thickness for resistivity calculation.\nSet to 0 if unknown — sheet resistance will still be valid.\nρ = K · t · (V/I) for thin film models.")
        layout.addRow(*self._form_pair(
            "Probe Spacing s", main_container.fpp_spacing_cm,
            "Thickness t", main_container.fpp_thickness_um,
        ))

        # Sample geometry and lateral size — drive the F84 / Smits geometry
        # correction factor. Leaving Diameter D = 0 treats the sample as
        # effectively infinite (the legacy default).
        main_container.fpp_diameter_cm = QDoubleSpinBox(decimals=4, minimum=0.0, maximum=100.0, singleStep=0.1, suffix=" cm")
        main_container.fpp_diameter_cm.setToolTip(
            "Sample lateral size D (diameter for circles, width for rectangles).\n"
            "Set to 0 to treat as infinite (no finite-size correction).\n"
            "Used with Geometry to look up F2 from ASTM F84 Table 3 (circles)\n"
            "or the Smits 1958 table (squares / rectangles)."
        )
        main_container.fpp_geometry = QComboBox()
        main_container.fpp_geometry.addItems(["circle", "square", "rectangle_2", "rectangle_3", "rectangle_4"])
        main_container.fpp_geometry.setToolTip(
            "Sample shape for the geometric correction factor.\n"
            "• circle: wafers / discs — F84 Table 3\n"
            "• square: square cuts\n"
            "• rectangle_2/3/4: rectangles with L/W = 2, 3, 4\n"
            "For rectangles, enter the WIDTH as Diameter D."
        )
        layout.addRow(*self._form_pair(
            "Diameter D (0=∞)", main_container.fpp_diameter_cm,
            "Geometry", main_container.fpp_geometry,
        ))

        main_container.fpp_alpha = QDoubleSpinBox(decimals=4, minimum=0.0, maximum=10.0, singleStep=0.01)
        main_container.fpp_alpha.setToolTip("Finite sample size correction factor.\nAccounts for edge effects when the sample is not much\nlarger than the probe spacing. Default: 1.0 (no correction).")
        main_container.fpp_k_factor = QDoubleSpinBox(decimals=4, minimum=0.1, maximum=50.0, singleStep=0.001)
        main_container.fpp_k_factor.setToolTip("Geometric correction factor (K).\n4.532 is the standard value for a linear 4-point probe\non an infinite thin film (F.M. Smits, 1958).")
        main_container.fpp_model = QComboBox()
        main_container.fpp_model.addItems(["thin_film", "semi_infinite", "finite_thin", "finite_alpha"])
        main_container.fpp_model.setToolTip("Calculation model:\n• thin_film: Rs = K·α·(V/I), ρ = K·α·t·(V/I)\n• semi_infinite: ρ = 2π·s·(V/I) — for bulk samples\n• finite_thin: like thin_film but without α correction")
        main_container.fpp_samples = QSpinBox(); main_container.fpp_samples.setRange(0, 1000000); main_container.fpp_samples.setSingleStep(10)
        main_container.fpp_samples.setToolTip("Number of readings to take before stopping.\n0 = continuous (run until you press Stop).")
        layout.addRow(*self._form_pair(
            "Model", main_container.fpp_model,
            "Samples (0=cont.)", main_container.fpp_samples,
        ))

        main_container.sampling_rate = NoScrollSpinBox(decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz")
        main_container.sampling_rate.setToolTip("How many readings per second to take.")
        main_container.fpp_plot_var = QComboBox()
        main_container.fpp_plot_var.addItems(["voltage", "current", "V/I", "sheet_Rs", "rho"])
        main_container.fpp_plot_var.setToolTip("Which derived quantity to plot.\nThe histogram in the right panel updates live; this only affects post-run analysis.")
        layout.addRow(*self._form_pair(
            "Sampling Rate", main_container.sampling_rate,
            "Plot Variable", main_container.fpp_plot_var,
        ))

        # Model info
        main_container.fpp_model_info = QLabel("")
        main_container.fpp_model_info.setWordWrap(True)
        layout.addRow("Model Info:", main_container.fpp_model_info)
        
        # Advanced collapsible using a checkable groupbox (proven robust approach)
        adv_group = QGroupBox("Advanced")
        adv_group.setCheckable(True)
        adv_group.setChecked(False)
        adv_form = QFormLayout(adv_group)
        adv_form.addRow("Auto Range Voltage:", main_container.fpp_voltage_range_auto)
        adv_form.addRow("Correction Factor α:", main_container.fpp_alpha)
        adv_form.addRow("K Factor:", main_container.fpp_k_factor)

        # Temperature correction (F84 §13.6–13.8, Table 5 / Appendix X2).
        # Only applied if dopant_type is 'n' or 'p'. NaN temperature also
        # short-circuits the correction. Silicon-specific; keep off for
        # non-Si materials.
        main_container.fpp_temperature_c = QDoubleSpinBox(decimals=2, minimum=-50.0, maximum=200.0, singleStep=0.5, suffix=" °C")
        main_container.fpp_temperature_c.setSpecialValueText("not measured")
        main_container.fpp_temperature_c.setValue(-50.0)  # special-value sentinel
        main_container.fpp_temperature_c.setToolTip(
            "Measurement temperature (°C). Leave at 'not measured' to skip\n"
            "the temperature correction. When set with a dopant, the reported\n"
            "ρ is corrected to 23 °C per F84 §13.6–13.8."
        )
        main_container.fpp_dopant_type = QComboBox()
        main_container.fpp_dopant_type.addItems(["none", "n", "p"])
        main_container.fpp_dopant_type.setToolTip(
            "Silicon dopant type for the temperature coefficient C_T\n"
            "lookup (F84 Table 5). Use 'none' for non-Si materials —\n"
            "no temperature correction will be applied."
        )
        adv_form.addRow("Temperature T:", main_container.fpp_temperature_c)
        adv_form.addRow("Dopant Type:", main_container.fpp_dopant_type)
        main_container.nplc = NoScrollSpinBox(decimals=2, minimum=0.01, maximum=10.0, singleStep=0.1)
        main_container.nplc.setToolTip("Number of Power Line Cycles for integration.\nHigher = slower but less noise. 1 PLC = 16.7 ms at 60 Hz.\n0.01: fastest, noisy | 1: balanced | 10: highest precision")
        adv_form.addRow("NPLC:", main_container.nplc)
        # Delta mode (current reversal)
        main_container.fpp_delta_mode = QCheckBox("Current Reversal (Delta Mode)")
        main_container.fpp_delta_mode.setToolTip(
            "Alternates +I and -I for each reading to cancel thermoelectric\n"
            "EMF from dissimilar probe-sample contacts.\n"
            "V_delta = (V+ - V-) / 2 eliminates DC offset voltages.\n"
            "Each reading takes 2x longer (two instrument reads per point).")
        adv_form.addRow(main_container.fpp_delta_mode)
        main_container.fpp_delta_settling = QDoubleSpinBox(decimals=3, minimum=0.01, maximum=5.0, singleStep=0.05, suffix=" s")
        main_container.fpp_delta_settling.setValue(0.1)
        main_container.fpp_delta_settling.setToolTip("Settling time between polarity flips.\nAllows the instrument and DUT to stabilize after reversing current.")
        main_container.fpp_delta_settling.setEnabled(False)
        main_container.fpp_delta_mode.toggled.connect(main_container.fpp_delta_settling.setEnabled)
        adv_form.addRow("Delta Settling:", main_container.fpp_delta_settling)

        # Probe safety: pre-flight + runtime power envelope.
        main_container.fpp_power_warn_w = QDoubleSpinBox(
            decimals=4, minimum=0.0001, maximum=10.0, singleStep=0.001, suffix=" W"
        )
        main_container.fpp_power_warn_w.setValue(0.01)
        main_container.fpp_power_warn_w.setToolTip(
            "Power envelope at which the run flashes a warning (no stop).\n"
            "Default 10 mW — typical conservative threshold for thin-film\n"
            "samples and tungsten-carbide 4PP probes (Signatone SP4 family).")
        adv_form.addRow("Power Warn:", main_container.fpp_power_warn_w)
        main_container.fpp_power_stop_w = QDoubleSpinBox(
            decimals=4, minimum=0.0001, maximum=22.0, singleStep=0.01, suffix=" W"
        )
        main_container.fpp_power_stop_w.setValue(0.1)
        main_container.fpp_power_stop_w.setToolTip(
            "Hard stop: aborts the run if measured V×I exceeds this.\n"
            "Default 100 mW. Pre-flight check at Start refuses to begin\n"
            "if the worst-case I_source × V_compliance exceeds this value.\n"
            "Raise only after reviewing your specific probe's spec sheet.")
        adv_form.addRow("Power Stop:", main_container.fpp_power_stop_w)
        main_container.fpp_stop_on_overpower = QCheckBox("Stop on Overpower")
        main_container.fpp_stop_on_overpower.setChecked(True)
        main_container.fpp_stop_on_overpower.setToolTip(
            "When checked, the run aborts immediately if measured power\n"
            "exceeds Power Stop. Uncheck to log a warning but keep running\n"
            "(advanced — not recommended for delicate samples).")
        adv_form.addRow(main_container.fpp_stop_on_overpower)

        layout.addRow("", adv_group)
        
        # Action buttons: Mark Event | Export Summary | Test Connection on one row
        main_container.mark_event_button = QPushButton(QIcon.fromTheme("emblem-important"), "Mark Event (M)")
        main_container.mark_event_button.setToolTip("Insert a named event marker into the data stream (keyboard shortcut: M).")
        main_container.mark_event_button.setEnabled(False)
        main_container.report_button = QPushButton(QIcon.fromTheme("document-save"), "Export Summary…")
        main_container.report_button.setToolTip("Export a CSV summary of all 4PP measurements\nincluding mean, standard deviation, and RSD.")
        main_container.report_button.clicked.connect(self.export_fpp_summary)
        test_conn_button = QPushButton("Test Connection")
        test_conn_button.setToolTip("Check if the instrument is reachable before starting.")
        test_conn_button.clicked.connect(self.test_instrument_connection)
        action_row = QWidget(); action_h = QHBoxLayout(action_row)
        action_h.setContentsMargins(0, 0, 0, 0)
        action_h.addWidget(main_container.mark_event_button)
        action_h.addWidget(main_container.report_button)
        action_h.addWidget(test_conn_button)
        layout.addRow("", action_row)
        
        # CREATE RIGHT PANEL CONTENTS: Spot management → Summary → Histogram → Spots table → Readings table

        # Spot management bar
        spot_bar = QHBoxLayout()
        main_container.fpp_spot_name = QLineEdit("Spot 1")
        main_container.fpp_spot_name.setMaximumWidth(120)
        main_container.fpp_spot_name.setToolTip("Name for the current measurement spot.")
        save_spot_btn = QPushButton("Save Spot")
        save_spot_btn.setToolTip("Archive current readings as a named spot,\nthen clear for the next probe position.")
        save_spot_btn.clicked.connect(self._save_fpp_spot)
        clear_spots_btn = QPushButton("Clear All")
        clear_spots_btn.setToolTip("Clear all saved spots and current readings.")
        clear_spots_btn.clicked.connect(self._clear_all_fpp_spots)
        spot_bar.addWidget(QLabel("Spot:"))
        spot_bar.addWidget(main_container.fpp_spot_name)
        spot_bar.addWidget(save_spot_btn)
        spot_bar.addWidget(clear_spots_btn)
        spot_bar.addStretch()
        right_layout.addLayout(spot_bar)

        # Current reading summary stats
        main_container.fpp_summary = QGroupBox("Current Spot Stats")
        sum_layout = QFormLayout(main_container.fpp_summary)
        main_container.fpp_n_label = QLabel("0")
        main_container.fpp_rs_label = QLabel("--")
        main_container.fpp_rho_label = QLabel("--")
        main_container.fpp_sigma_label = QLabel("--")
        sum_layout.addRow("N:", main_container.fpp_n_label)
        sum_layout.addRow("Rs mean±std (Ω/□; RSD%):", main_container.fpp_rs_label)
        sum_layout.addRow("ρ mean±std (Ω·cm; RSD%):", main_container.fpp_rho_label)
        sum_layout.addRow("σ mean±std (S/cm; RSD%):", main_container.fpp_sigma_label)

        # Histogram canvas
        main_container.fpp_histogram = HistogramCanvas(main_container, width=5, height=2.5, dpi=90)
        main_container.fpp_histogram.setMinimumHeight(150)
        main_container.fpp_histogram.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Spots summary table (compact, shows saved spots)
        main_container.fpp_spots_table = QTableWidget(0, 5)
        main_container.fpp_spots_table.setHorizontalHeaderLabels(['Spot', 'N', 'Rs (Ω/□)', 'Std', 'RSD%'])
        main_container.fpp_spots_table.setMaximumHeight(120)
        main_container.fpp_spots_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # Measurements table (current spot readings)
        main_container.fpp_table = QTableWidget(0, 9)
        main_container.fpp_table.setHorizontalHeaderLabels([
            'Time (s)', 'V (V)', 'I (A)', 'V/I (Ω)', 'Rs (Ω/□)', 'ρ (Ω·cm)', 'σ (S/cm)', 'Comp', 'Event'
        ])
        main_container.fpp_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Add to right panel with stretch factors
        right_layout.addWidget(main_container.fpp_summary)
        right_layout.addWidget(main_container.fpp_histogram, 2)
        right_layout.addWidget(main_container.fpp_spots_table, 0)
        right_layout.addWidget(main_container.fpp_table, 3)
        
        # Make parameter inputs compact (preserve existing styling)
        for sb in [main_container.fpp_current, main_container.fpp_voltage_compliance, main_container.fpp_spacing_cm, main_container.fpp_thickness_um, main_container.fpp_alpha, main_container.fpp_k_factor, main_container.fpp_samples]:
            sb.setMaximumWidth(140)
        main_container.fpp_plot_var.setMaximumWidth(140)
        main_container.fpp_model.setMaximumWidth(160)
        
        # Connect all events (exactly as before)
        main_container.start_button.clicked.connect(lambda: self.start_measurement('four_point'))
        main_container.stop_button.clicked.connect(self.stop_current_measurement)
        main_container.pause_button.toggled.connect(lambda checked: self.pause_resume_measurement(checked))
        main_container.mark_event_button.clicked.connect(self.mark_event_shortcut)
        main_container.fpp_plot_var.currentTextChanged.connect(lambda _: self.update_canvas_labels_for_mode('four_point'))
        main_container.fpp_model.currentTextChanged.connect(lambda *_: self.update_four_point_model_info())
        main_container.fpp_alpha.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        main_container.fpp_k_factor.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        main_container.fpp_spacing_cm.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        main_container.fpp_thickness_um.valueChanged.connect(lambda *_: self.update_four_point_model_info())
        
        # Internal storage for quick stats
        main_container._fpp_rows = []  # list of tuples (time, v, i, ratio, rs, rho, sigma, comp, event)
        main_container._fpp_spots = []  # list of dicts: {name, n, rows, rs_mean, rs_std, rho_mean, rho_std, sigma_mean, sigma_std}
        main_container._fpp_spot_counter = 1

        # Stretch factor [0, 1] + setChildrenCollapsible(False) is enough now:
        # params holds its sizeHint width, right_panel claims the remainder.

        # Initialize model info text using this widget (before self.tab_four_point is assigned)
        self.update_four_point_model_info(main_container)

        return main_container

    def create_sweep_tab(self):
        """Create I-V Sweep tab with sweep parameters and I-V plot."""
        widget = QWidget()
        widget.mode = 'sweep'
        tab_layout = QVBoxLayout(widget)

        # Parameters
        param_group = QGroupBox("Sweep Parameters")
        param_layout = QFormLayout(param_group)
        widget.param_layout = param_layout
        widget.param_group = param_group

        widget.sweep_source = QComboBox()
        widget.sweep_source.addItems(["voltage", "current"])
        widget.sweep_source.setToolTip("Source function for the sweep.\nVoltage: sweep V, measure I\nCurrent: sweep I, measure V")
        widget.sweep_source.currentTextChanged.connect(self._update_sweep_labels)
        widget.sweep_direction = QComboBox()
        widget.sweep_direction.addItems(["up", "down", "up_down"])
        widget.sweep_direction.setToolTip("Sweep direction:\n• Up: start → stop\n• Down: stop → start\n• Up-Down: forward + reverse (shows hysteresis)")
        param_layout.addRow(*self._form_pair(
            "Source", widget.sweep_source,
            "Direction", widget.sweep_direction,
        ))

        widget.sweep_start = EngineeringSpinBox(unit='V', minimum=-200.0, maximum=200.0, default=0.0, allow_negative=True)
        widget.sweep_start.setToolTip("Sweep start value")
        widget.sweep_stop = EngineeringSpinBox(unit='V', minimum=-200.0, maximum=200.0, default=1.0, allow_negative=True)
        widget.sweep_stop.setToolTip("Sweep stop value")
        param_layout.addRow(*self._form_pair(
            "Start", widget.sweep_start,
            "Stop", widget.sweep_stop,
        ))

        widget.sweep_step = EngineeringSpinBox(unit='V', minimum=1e-6, maximum=200.0, default=0.05)
        widget.sweep_step.setToolTip("Step size (always positive — direction is determined by start/stop)")
        widget.sweep_compliance = EngineeringSpinBox(unit='A', minimum=1e-7, maximum=3.0, default=0.1)
        widget.sweep_compliance.setToolTip("Compliance limit for the measured function.\nAccepts: 100mA, 1mA, 0.1A, etc.")
        param_layout.addRow(*self._form_pair(
            "Step", widget.sweep_step,
            "Compliance", widget.sweep_compliance,
        ))

        widget.sweep_delay = NoScrollSpinBox(decimals=3, minimum=0.0, maximum=10.0, singleStep=0.01, suffix=" s")
        widget.sweep_delay.setValue(0.01)
        widget.sweep_delay.setToolTip("Source delay per step — time for DUT to settle after each step.\n0.01s is typical. Increase for capacitive DUTs.")
        widget.sweep_nplc = NoScrollSpinBox(decimals=2, minimum=0.01, maximum=10.0, singleStep=0.1)
        widget.sweep_nplc.setValue(1.0)
        widget.sweep_nplc.setToolTip("NPLC for each measurement point in the sweep.")
        param_layout.addRow(*self._form_pair(
            "Step Delay", widget.sweep_delay,
            "NPLC", widget.sweep_nplc,
        ))

        # Points preview + Test Connection on one row
        widget.sweep_points_label = QLabel("Points: 21")
        widget.sweep_start.valueChanged.connect(lambda: self._update_sweep_points())
        widget.sweep_stop.valueChanged.connect(lambda: self._update_sweep_points())
        widget.sweep_step.valueChanged.connect(lambda: self._update_sweep_points())
        test_conn_button = QPushButton("Test Connection")
        test_conn_button.setToolTip("Check instrument connection before sweeping.")
        test_conn_button.clicked.connect(self.test_instrument_connection)
        bottom_row = QWidget(); bottom_h = QHBoxLayout(bottom_row)
        bottom_h.setContentsMargins(0, 0, 0, 0)
        bottom_h.addWidget(widget.sweep_points_label)
        bottom_h.addStretch()
        bottom_h.addWidget(test_conn_button)
        param_layout.addRow("", bottom_row)

        # I-V Plot
        plot_group = QGroupBox("I-V Characteristic")
        plot_layout = QVBoxLayout(plot_group)
        widget.iv_canvas = IVCanvas(self, width=8, height=5, dpi=90)
        widget.canvas = widget.iv_canvas  # alias for compatibility
        toolbar = NavigationToolbar(widget.iv_canvas, self)
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(widget.iv_canvas)

        # Live readout + Control row (shared chrome). Sweep uses a smaller
        # readout font because the message ("41 points acquired") is long, and
        # the run is atomic so no Pause button.
        widget.live_readout = self._build_live_readout(font_pt=20)
        control_group, widget.start_button, widget.stop_button, _, widget.status_label = (
            self._build_control_row(with_pause=False)
        )
        widget.start_button.setText("Run Sweep")
        widget.stop_button.setText("Abort")
        widget.control_group = control_group
        widget.pause_button = None
        widget.mark_event_button = None

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(param_group)
        splitter.addWidget(plot_group)
        splitter.setStretchFactor(0, 0)  # params keeps its sizeHint width
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        widget.splitter = splitter
        widget.plot_group = plot_group

        tab_layout.addWidget(splitter, 1)
        tab_layout.addWidget(widget.live_readout, 0)
        tab_layout.addWidget(control_group, 0)

        # Connect
        widget.start_button.clicked.connect(lambda: self.start_measurement('sweep'))
        widget.stop_button.clicked.connect(self.stop_current_measurement)

        return widget

    def _update_sweep_labels(self):
        """Update sweep input labels when source function changes."""
        w = self.tab_sweep
        src = w.sweep_source.currentText()
        if src == 'voltage':
            for box in (w.sweep_start, w.sweep_stop, w.sweep_step):
                box._unit = 'V'
                box._display_value()
            w.sweep_compliance._unit = 'A'
            w.sweep_compliance._display_value()
        else:
            for box in (w.sweep_start, w.sweep_stop, w.sweep_step):
                box._unit = 'A'
                box._display_value()
            w.sweep_compliance._unit = 'V'
            w.sweep_compliance._display_value()

    def _update_sweep_points(self):
        """Update sweep points preview label."""
        w = self.tab_sweep
        start = w.sweep_start.value()
        stop = w.sweep_stop.value()
        step = w.sweep_step.value()
        if step > 0:
            points = int(round(abs(stop - start) / step)) + 1
        else:
            points = 1
        direction = w.sweep_direction.currentText()
        if direction == 'up_down':
            w.sweep_points_label.setText(f"Points: {points} × 2 = {points * 2} (forward + reverse)")
        else:
            w.sweep_points_label.setText(f"Points: {points}")

    # =========================================================================
    # Van der Pauw tab (ASTM F76 Method A)
    # =========================================================================
    def create_vdp_tab(self):
        """6th tab: van der Pauw resistivity per ASTM F76 Method A.

        Workflow:
        1. User wires 4 leads to one of F76's 4 cabling configurations.
        2. UI walks them through the 4 geometries one at a time.
        3. For each: user presses "Measure This Configuration"; the worker
           takes +I and -I readings (current reversal embedded per F76).
        4. After 4 geometries (8 voltage readings) the worker computes
           rho, R_s, Q, f and the homogeneity gate.
        """
        from ..calculations_vdp import f76_geometries

        widget = QWidget()
        widget.mode = 'vdp'
        tab_layout = QVBoxLayout(widget)

        # ------- LEFT: parameters -------
        param_group = QGroupBox("Sample & Source")
        param_form = QFormLayout(param_group)

        widget.vdp_current = EngineeringSpinBox(unit='A', minimum=1e-9, maximum=1.0, default=1.0e-3)
        widget.vdp_current.setToolTip(
            "Source current magnitude I (constant; sign reversed automatically).\n"
            "F76 sec. 7.3.1: keep I small enough to avoid resistive heating; the\n"
            "associated electric field should be well under 1 V/cm."
        )
        widget.vdp_voltage_compliance = NoScrollSpinBox(
            decimals=2, minimum=0.001, maximum=200.0, singleStep=0.1, suffix=" V"
        )
        widget.vdp_voltage_compliance.setValue(5.0)
        widget.vdp_voltage_compliance.setToolTip(
            "Maximum voltage the source will drive. Caps the V at the Force\n"
            "terminals if the sample is unexpectedly high-impedance."
        )
        param_form.addRow(*self._form_pair(
            "Source Current I", widget.vdp_current,
            "V Compliance", widget.vdp_voltage_compliance,
        ))

        widget.vdp_thickness_cm = NoScrollSpinBox(
            decimals=6, minimum=1e-7, maximum=10.0, singleStep=1e-4, suffix=" cm"
        )
        widget.vdp_thickness_cm.setValue(1.0e-4)
        widget.vdp_thickness_cm.setToolTip(
            "Sample thickness t in cm (F76 sec. 9.3 wants t/L_p <= 1/15).\n"
            "rho = (pi/(4*ln2)) * f * t * (V/I) per F76 eq. (1).\n"
            "Sheet resistance Rs = rho / t is reported separately."
        )
        widget.vdp_settling_s = NoScrollSpinBox(
            decimals=3, minimum=0.0, maximum=10.0, singleStep=0.05, suffix=" s"
        )
        widget.vdp_settling_s.setValue(0.2)
        widget.vdp_settling_s.setToolTip(
            "Delay after each polarity flip before reading. Lets the source\n"
            "and DUT settle; longer is safer for capacitive samples."
        )
        param_form.addRow(*self._form_pair(
            "Thickness t", widget.vdp_thickness_cm,
            "Settling", widget.vdp_settling_s,
        ))

        widget.vdp_voltage_range_auto = QCheckBox("Auto Range V")
        widget.vdp_voltage_range_auto.setChecked(True)
        widget.vdp_voltage_range_auto.setToolTip(
            "When checked, the Keithley picks the voltage range automatically.\n"
            "Uncheck for slightly faster, fixed-range measurements."
        )
        widget.vdp_readings_per_polarity = NoScrollIntSpinBox(
            minimum=1, maximum=100, singleStep=1, value=1
        )
        widget.vdp_readings_per_polarity.setToolTip(
            "Software averaging at each polarity. >1 trades time for noise.\n"
            "Independent of the hardware Filter on the Settings dialog."
        )
        param_form.addRow(*self._form_pair(
            "Auto Range", widget.vdp_voltage_range_auto,
            "Avg / polarity", widget.vdp_readings_per_polarity,
        ))

        widget.nplc = NoScrollSpinBox(
            decimals=2, minimum=0.01, maximum=10.0, singleStep=0.1, suffix=" PLC"
        )
        widget.nplc.setValue(1.0)
        widget.nplc.setToolTip(
            "Integration time per reading, in power-line cycles.\n"
            "Higher NPLC = lower noise, slower reading. Standard scientific\n"
            "choice is 1 NPLC."
        )
        widget.sampling_rate = NoScrollSpinBox(  # accepted by gather_settings, unused live
            decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz"
        )
        widget.sampling_rate.setValue(10.0)
        widget.sampling_rate.setEnabled(False)
        widget.sampling_rate.setToolTip(
            "Not used in vdP (the workflow is step-by-step, not streaming).\n"
            "Kept for settings compatibility with the other tabs."
        )
        param_form.addRow(*self._form_pair(
            "NPLC", widget.nplc,
            "Sampling Rate", widget.sampling_rate,
        ))

        # ------- RIGHT: stepper + readings table + result panel -------
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Instruction panel: diagram (left) + text label (right) side-by-side
        instr_group = QGroupBox("Current Configuration")
        instr_layout = QVBoxLayout(instr_group)
        instr_top = QHBoxLayout()
        widget.vdp_diagram = VdpSampleDiagram()
        instr_top.addWidget(widget.vdp_diagram, 0)
        widget.vdp_step_label = QLabel(
            "Idle. Wire 4 contacts (numbered 1-4 counter-clockwise around the "
            "sample periphery) and press Start."
        )
        widget.vdp_step_label.setWordWrap(True)
        widget.vdp_step_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        f = QFont(); f.setPointSize(11); f.setBold(True)
        widget.vdp_step_label.setFont(f)
        widget.vdp_step_label.setStyleSheet(
            "color: #222; background: #f6f6f6; border: 1px solid #ccc; "
            "border-radius: 4px; padding: 8px;"
        )
        widget.vdp_step_label.setMinimumHeight(80)
        instr_top.addWidget(widget.vdp_step_label, 1)
        instr_layout.addLayout(instr_top)

        # Protocol filmstrip: all 4 geometries at a glance, current highlighted.
        widget.vdp_filmstrip = VdpProtocolFilmstrip()
        instr_layout.addWidget(widget.vdp_filmstrip)

        widget.vdp_proceed_button = QPushButton("Measure This Configuration")
        widget.vdp_proceed_button.setEnabled(False)
        widget.vdp_proceed_button.setToolTip(
            "Click after you've reconnected the 4 leads as shown above.\n"
            "The worker will then source +I, read V, source -I, read V."
        )
        widget.vdp_proceed_button.clicked.connect(self._vdp_proceed_clicked)
        instr_layout.addWidget(widget.vdp_proceed_button)
        right_layout.addWidget(instr_group)

        # Readings table (8 F76 labels)
        table_group = QGroupBox("F76 Voltage Readings")
        table_layout = QVBoxLayout(table_group)
        widget.vdp_readings_table = QTableWidget(8, 3)
        widget.vdp_readings_table.setHorizontalHeaderLabels([
            "F76 Label", "Geometry / Polarity", "V (V)"
        ])
        widget.vdp_readings_table.verticalHeader().setVisible(False)
        widget.vdp_readings_table.setEditTriggers(QTableWidget.NoEditTriggers)
        widget.vdp_readings_table.setSelectionMode(QTableWidget.NoSelection)
        for row, geom in enumerate(f76_geometries()):
            widget.vdp_readings_table.setItem(2 * row, 0, QTableWidgetItem(geom.label_pos))
            widget.vdp_readings_table.setItem(2 * row, 1, QTableWidgetItem(f"{geom.name}, +I (group {geom.group})"))
            widget.vdp_readings_table.setItem(2 * row, 2, QTableWidgetItem("--"))
            widget.vdp_readings_table.setItem(2 * row + 1, 0, QTableWidgetItem(geom.label_neg))
            widget.vdp_readings_table.setItem(2 * row + 1, 1, QTableWidgetItem(f"{geom.name}, -I (group {geom.group})"))
            widget.vdp_readings_table.setItem(2 * row + 1, 2, QTableWidgetItem("--"))
        widget.vdp_readings_table.resizeColumnsToContents()
        widget.vdp_readings_table.horizontalHeader().setStretchLastSection(True)
        table_layout.addWidget(widget.vdp_readings_table)
        right_layout.addWidget(table_group, 1)

        # Result panel: hidden visually until a measurement completes; then
        # the instruction panel collapses and this expands to fill the
        # space with the headline numbers + per-geometry bar chart.
        result_group = QGroupBox("Result (F76 Method A)")
        widget.vdp_result_group = result_group
        result_layout = QVBoxLayout(result_group)

        # Headline numbers (large) -- Rs and rho side-by-side.
        headline = QHBoxLayout()
        widget.vdp_rs_label = QLabel("Rs: —")
        widget.vdp_rho_label = QLabel("ρ: —")
        big = QFont(); big.setPointSize(16); big.setBold(True)
        widget.vdp_rs_label.setFont(big)
        widget.vdp_rho_label.setFont(big)
        widget.vdp_rs_label.setAlignment(Qt.AlignCenter)
        widget.vdp_rho_label.setAlignment(Qt.AlignCenter)
        widget.vdp_rs_label.setTextFormat(Qt.RichText)
        widget.vdp_rho_label.setTextFormat(Qt.RichText)
        headline.addWidget(widget.vdp_rs_label, 1)
        headline.addWidget(widget.vdp_rho_label, 1)
        result_layout.addLayout(headline)

        # Per-geometry bar chart -- visual homogeneity check.
        widget.vdp_bar_chart = VdpPerGeometryBarChart()
        result_layout.addWidget(widget.vdp_bar_chart)

        # Secondary stats (Q, f, asymmetry %).
        widget.vdp_stats_label = QLabel("")
        widget.vdp_stats_label.setAlignment(Qt.AlignCenter)
        widget.vdp_stats_label.setTextFormat(Qt.RichText)
        sf = QFont(); sf.setPointSize(10)
        widget.vdp_stats_label.setFont(sf)
        result_layout.addWidget(widget.vdp_stats_label)

        # Homogeneity verdict banner -- big, color-coded.
        widget.vdp_homogeneity_banner = QLabel("")
        widget.vdp_homogeneity_banner.setAlignment(Qt.AlignCenter)
        bf = QFont(); bf.setPointSize(12); bf.setBold(True)
        widget.vdp_homogeneity_banner.setFont(bf)
        widget.vdp_homogeneity_banner.setMinimumHeight(36)
        result_layout.addWidget(widget.vdp_homogeneity_banner)
        right_layout.addWidget(result_group)
        widget.vdp_instr_group = instr_group
        # Only one of (instructions, result) is ever on screen at a time.
        # Initial idle state shows the instructions; the result panel
        # appears only after a measurement completes.
        result_group.setVisible(False)

        # Control row (Start / Stop / Status)
        control_group, start_button, stop_button, _, status_label = (
            self._build_control_row(with_pause=False)
        )

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(param_group)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)

        tab_layout.addWidget(splitter, 1)
        tab_layout.addWidget(control_group, 0)

        # Wire start/stop
        start_button.clicked.connect(self.start_vdp_measurement)
        stop_button.clicked.connect(self.stop_vdp_measurement)

        widget.param_layout = param_form
        widget.param_group = param_group
        widget.start_button = start_button
        widget.stop_button = stop_button
        widget.pause_button = None
        widget.status_label = status_label
        widget.control_group = control_group
        widget.splitter = splitter
        # vdP does not have a continuous plot canvas; expose a placeholder
        # so generic callers that probe `.canvas` don't AttributeError.
        widget.canvas = None
        return widget

    def start_vdp_measurement(self):
        """Begin a van der Pauw run."""
        if self.measurement_running:
            QMessageBox.warning(
                self, "Measurement Active",
                f"A measurement ({self.active_mode}) is already running. "
                "Stop it first."
            )
            return
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select or create a user first.")
            return
        sample_name = self._require_sample_name()
        if not sample_name:
            return

        try:
            current_settings = self.gather_settings_for_mode('vdp')
        except ValueError as e:
            QMessageBox.critical(self, "Settings Error", f"Failed to gather settings: {e}")
            return

        self.active_mode = 'vdp'
        self.measurement_running = True
        self.set_controls_for_mode('vdp', running=True)
        self.set_all_controls_enabled(False, except_mode='vdp')
        self.sample_input.setEnabled(False)
        self.change_user_button.setEnabled(False)

        widget = self.tab_vdp
        widget.status_label.setText("Status: Connecting...")
        widget.status_label.setStyleSheet("font-weight: bold; color: green;")

        # Reset readings table to blanks
        for r in range(widget.vdp_readings_table.rowCount()):
            widget.vdp_readings_table.item(r, 2).setText("--")
            widget.vdp_readings_table.item(r, 2).setBackground(QBrush(QColor("white")))
        # Reset result panel; instruction panel is brought back into view.
        widget.vdp_rs_label.setText("Rs: —")
        widget.vdp_rho_label.setText("ρ: —")
        widget.vdp_stats_label.setText("")
        widget.vdp_bar_chart.clear()
        widget.vdp_homogeneity_banner.setText("")
        widget.vdp_homogeneity_banner.setStyleSheet("")
        widget.vdp_step_label.setText("Connecting to instrument...")
        widget.vdp_diagram.set_configuration(None)
        widget.vdp_filmstrip.reset()
        # Swap panels: instructions in, result out. Keeps the right pane
        # at a single content height so neither panel gets squeezed.
        widget.vdp_instr_group.setVisible(True)
        widget.vdp_result_group.setVisible(False)

        self.log_status(f"Starting van der Pauw measurement for sample: {sample_name}...")
        self.statusBar().showMessage("Measurement running (vdp)...")

        self.measurement_worker = VdpMeasurementWorker(
            sample_name=sample_name, username=self.current_user,
            settings=current_settings,
        )
        self.measurement_worker.geometry_ready.connect(self._vdp_on_geometry_ready)
        self.measurement_worker.geometry_complete.connect(self._vdp_on_geometry_complete)
        self.measurement_worker.vdp_complete.connect(self._vdp_on_complete)
        self.measurement_worker.status_update.connect(self.log_status_from_worker)
        self.measurement_worker.error_occurred.connect(self.on_error)
        self.measurement_worker.compliance_hit.connect(self.on_compliance_hit)
        self.measurement_worker.finished.connect(self._vdp_on_worker_finished)
        self.measurement_worker.start()

    def stop_vdp_measurement(self):
        if self.measurement_worker and self.measurement_running:
            self.log_status("Stopping van der Pauw measurement...")
            self.tab_vdp.stop_button.setEnabled(False)
            self.tab_vdp.vdp_proceed_button.setEnabled(False)
            self.tab_vdp.status_label.setText("Status: Stopping...")
            self.tab_vdp.status_label.setStyleSheet("font-weight: bold; color: orange;")
            self.measurement_worker.stop_measurement()

    def _vdp_proceed_clicked(self):
        if self.measurement_worker and isinstance(self.measurement_worker, VdpMeasurementWorker):
            self.tab_vdp.vdp_proceed_button.setEnabled(False)
            self.tab_vdp.vdp_step_label.setText(
                self.tab_vdp.vdp_step_label.text() + "\n\nMeasuring (+I then -I)..."
            )
            self.measurement_worker.proceed()

    def _vdp_on_geometry_ready(self, idx: int, geom: Dict):
        widget = self.tab_vdp
        widget.vdp_step_label.setText(
            f"<b>{geom['name']}</b>  (group {geom['group']})<br>"
            f"<br>"
            f"<b>Force HI</b>  &rarr;  Contact <b>{geom['source_high']}</b>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;"
            f"<b>Force LO</b>  &rarr;  Contact <b>{geom['source_low']}</b><br>"
            f"<b>Sense HI</b>  &rarr;  Contact <b>{geom['sense_high']}</b>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;"
            f"<b>Sense LO</b>  &rarr;  Contact <b>{geom['sense_low']}</b><br>"
            f"<br>"
            f"Reconnect leads, then press <b>Measure This Configuration</b>.<br>"
            f"Will produce: {geom['label_pos']} (at +I) and {geom['label_neg']} (at &minus;I)."
        )
        widget.vdp_step_label.setTextFormat(Qt.RichText)
        widget.vdp_diagram.set_configuration(geom)
        widget.vdp_filmstrip.set_current(idx)
        widget.vdp_proceed_button.setEnabled(True)
        widget.vdp_proceed_button.setText(f"Measure {geom['name']}")

    def _vdp_on_geometry_complete(self, idx: int, readings: Dict):
        widget = self.tab_vdp
        # Update the two rows for this geometry (rows 2*idx and 2*idx+1).
        widget.vdp_readings_table.item(2 * idx, 2).setText(f"{readings['v_pos']:.6e}")
        widget.vdp_readings_table.item(2 * idx, 2).setBackground(QBrush(QColor("#e8f5e9")))
        widget.vdp_readings_table.item(2 * idx + 1, 2).setText(f"{readings['v_neg']:.6e}")
        widget.vdp_readings_table.item(2 * idx + 1, 2).setBackground(QBrush(QColor("#e8f5e9")))
        widget.vdp_filmstrip.mark_completed(idx)

    def _vdp_on_complete(self, result: Dict):
        widget = self.tab_vdp

        # Measurement is done -- swap panels: instructions out, result in.
        # Only one of the two is ever visible so neither gets squeezed
        # below its minimum size.
        widget.vdp_instr_group.setVisible(False)
        widget.vdp_result_group.setVisible(True)

        # Headline numbers in engineering notation.
        rs = float(result['sheet_resistance'])
        rho = float(result['rho_avg'])
        widget.vdp_rs_label.setText(
            f"R<sub>s</sub> = {format_engineering(rs, 'Ω/sq', precision=4)}"
        )
        widget.vdp_rho_label.setText(
            f"ρ = {format_engineering(rho, 'Ω·cm', precision=4)}"
        )

        # Per-geometry resistance bars: each is the current-reversal-derived
        # R for one F76 geometry. On a uniform sample they should agree.
        voltages = result['voltages']
        current = float(result['current_a'])
        pairs = [
            ("V_21,34", "V_12,34"),
            ("V_32,41", "V_23,41"),
            ("V_43,12", "V_34,12"),
            ("V_14,23", "V_41,23"),
        ]
        r_values = [
            (voltages[p] - voltages[n]) / (2.0 * current) for p, n in pairs
        ]
        widget.vdp_bar_chart.set_data(r_values, ["G1", "G2", "G3", "G4"])

        widget.vdp_stats_label.setText(
            f"Q<sub>A</sub> = {result['q_a']:.4f}"
            f" &nbsp;&nbsp; Q<sub>B</sub> = {result['q_b']:.4f}"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"f<sub>A</sub> = {result['f_a']:.4f}"
            f" &nbsp;&nbsp; f<sub>B</sub> = {result['f_b']:.4f}"
            f"<br>asymmetry = {result['asymmetry_pct']:.3f} %"
        )

        if result['homogeneous']:
            widget.vdp_homogeneity_banner.setText(
                f"HOMOGENEOUS  (asymmetry {result['asymmetry_pct']:.2f}% ≤ 10% F76 gate)"
            )
            widget.vdp_homogeneity_banner.setStyleSheet(
                "color: white; background: #2e7d32; border-radius: 4px; padding: 6px;"
            )
        else:
            widget.vdp_homogeneity_banner.setText(
                f"NON-HOMOGENEOUS  (asymmetry {result['asymmetry_pct']:.2f}% > 10% F76 gate)"
            )
            widget.vdp_homogeneity_banner.setStyleSheet(
                "color: white; background: #c62828; border-radius: 4px; padding: 6px;"
            )

    def _vdp_on_worker_finished(self):
        """QThread finished signal: reset UI back to idle regardless of cause."""
        self.measurement_running = False
        self.active_mode = None
        self.set_controls_for_mode('vdp', running=False)
        self.set_all_controls_enabled(True)
        self.sample_input.setEnabled(True)
        self.change_user_button.setEnabled(True)
        self.tab_vdp.vdp_proceed_button.setEnabled(False)
        self.tab_vdp.status_label.setText("Status: Idle")
        self.tab_vdp.status_label.setStyleSheet("font-weight: bold;")
        self.statusBar().showMessage("Ready")
        self.measurement_worker = None

    def create_menus(self):
        menu_bar = self.menuBar()
        # File
        file_menu = menu_bar.addMenu("&File")
        save_plot_action = QAction(QIcon.fromTheme("document-save"), "Save Plot...", self)
        save_plot_action.triggered.connect(self.save_active_plot)
        exit_action = QAction(QIcon.fromTheme("application-exit"), "Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(save_plot_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)
        # Note: "Open Result (CSV)..." lives on the Results Viewer tab itself
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
        self.action_show_status = QAction("Show Status Log", self, checkable=True)
        self.action_show_status.setChecked(True)
        self.action_show_status.toggled.connect(lambda v: self.toggle_status_visibility(v))
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
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open Result CSV",
            self.user_settings['file']['data_directory'] if self.user_settings else ".",
            "Measurement Files (*.csv *.csv.gz);;CSV Files (*.csv);;Gzipped CSV (*.csv.gz);;All Files (*)",
        )
        if not filename:
            return
        try:
            import csv
            import gzip
            columns = {}
            order = []
            opener = gzip.open if filename.endswith('.gz') else open
            with opener(filename, 'rt', encoding='utf-8', newline='') as f:
                reader = csv.reader(f)
                headers = None
                for row in reader:
                    if not row:
                        continue
                    # Skip #-prefixed metadata header/footer and the legacy
                    # "Test Parameters" preamble that older CSVs used.
                    if row[0].startswith('###') or row[0].startswith('#') or row[0] in ('Test Parameters',):
                        continue
                    if headers is None:
                        headers = row
                        for h in headers:
                            columns[h] = []
                            order.append(h)
                        continue
                    if headers and len(row) == len(headers):
                        for i, h in enumerate(headers):
                            val = row[i]
                            try:
                                valf = float(val)
                            except Exception:
                                valf = float('nan')
                            columns[h].append(valf)
            # Locate the time column. The v2.0 schema uses 'elapsed_s'; older
            # CSVs used friendly names like 'Elapsed Time (s)'.
            tkey = None
            for candidate in ('elapsed_s', 'Elapsed Time (s)', 'Elapsed Time'):
                if candidate in columns:
                    tkey = candidate
                    break
            if tkey is None:
                for k in columns.keys():
                    if 'elapsed' in k.lower() or 'Elapsed Time' in k:
                        tkey = k
                        break
            if not tkey:
                QMessageBox.warning(self, "Open Result", "Could not find an elapsed-time column in CSV.")
                return
            self.results_data = {"time": columns[tkey], "columns": columns, "order": order}
            # Pull metadata header (run parameters) for logging context.
            try:
                from resistamet_gui.data_export import parse_metadata
                meta = parse_metadata(filename)
                if meta.get('mode'):
                    self.log_status(
                        f"Run metadata: mode={meta.get('mode')} "
                        f"sample={meta.get('sample', '?')} "
                        f"started={meta.get('started_at', '?')}"
                    )
            except Exception as e:
                logger.debug(f"parse_metadata failed for {filename}: {e}")
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
            # Load NPLC and sampling rate if present (applies to all modes)
            if 'nplc' in prof and hasattr(w, 'nplc'):
                w.nplc.setValue(float(prof['nplc']))
            if 'sampling_rate' in prof and hasattr(w, 'sampling_rate'):
                w.sampling_rate.setValue(float(prof['sampling_rate']))
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
        self.tab_resistance.res_offset_comp.setChecked(m_cfg.get('res_offset_comp', False))
        self.tab_resistance.sampling_rate.setValue(m_cfg['sampling_rate'])
        self.tab_resistance.canvas.set_plot_properties('Elapsed Time (s)', 'Resistance (Ohms)', 'Resistance Measurement', d_cfg['plot_color_r'])
        self.tab_voltage_source.vsource_voltage.setValue(m_cfg['vsource_voltage'])
        self.tab_voltage_source.vsource_current_compliance.setValue(m_cfg['vsource_current_compliance'])
        self.tab_voltage_source.vsource_current_range_auto.setChecked(m_cfg['vsource_current_range_auto'])
        dur_v = m_cfg.get('vsource_duration_hours', 0.0)
        self.tab_voltage_source.vsource_duration.setValue(dur_v)
        self.tab_voltage_source.vsource_run_continuous.setChecked(dur_v == 0.0)
        self.tab_voltage_source.sampling_rate.setValue(m_cfg['sampling_rate'])
        self.tab_voltage_source.v_plot_var.setCurrentText('current')
        self.tab_voltage_source.canvas.set_plot_properties('Elapsed Time (s)', 'Measured Current (A)', 'Voltage Source Output', d_cfg['plot_color_v'])
        self.tab_current_source.isource_current.setValue(m_cfg['isource_current'])
        self.tab_current_source.isource_voltage_compliance.setValue(m_cfg['isource_voltage_compliance'])
        self.tab_current_source.isource_voltage_range_auto.setChecked(m_cfg['isource_voltage_range_auto'])
        dur_i = m_cfg.get('isource_duration_hours', 0.0)
        self.tab_current_source.isource_duration.setValue(dur_i)
        self.tab_current_source.isource_run_continuous.setChecked(dur_i == 0.0)
        self.tab_current_source.sampling_rate.setValue(m_cfg['sampling_rate'])
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
        self.tab_four_point.fpp_samples.setValue(int(m_cfg.get('fpp_samples', 0)))
        # F84 correction-factor inputs (added 2026-05). Defaults preserve
        # legacy behavior: D=0 (infinite), geometry=circle, no T correction.
        self.tab_four_point.fpp_diameter_cm.setValue(float(m_cfg.get('fpp_diameter_cm', 0.0)))
        self.tab_four_point.fpp_geometry.setCurrentText(m_cfg.get('fpp_geometry', 'circle'))
        t_c = m_cfg.get('fpp_temperature_c', float('nan'))
        if t_c is None or not (isinstance(t_c, (int, float)) and t_c == t_c):  # NaN check
            self.tab_four_point.fpp_temperature_c.setValue(-50.0)  # "not measured"
        else:
            self.tab_four_point.fpp_temperature_c.setValue(float(t_c))
        self.tab_four_point.fpp_dopant_type.setCurrentText(m_cfg.get('fpp_dopant_type', 'none'))
        if hasattr(self.tab_four_point, 'fpp_power_warn_w'):
            self.tab_four_point.fpp_power_warn_w.setValue(float(m_cfg.get('fpp_power_warn_w', 1.0e-2)))
        if hasattr(self.tab_four_point, 'fpp_power_stop_w'):
            self.tab_four_point.fpp_power_stop_w.setValue(float(m_cfg.get('fpp_power_stop_w', 1.0e-1)))
        if hasattr(self.tab_four_point, 'fpp_stop_on_overpower'):
            self.tab_four_point.fpp_stop_on_overpower.setChecked(bool(m_cfg.get('fpp_stop_on_overpower', True)))
        self.tab_four_point.nplc.setValue(m_cfg['nplc'])
        self.tab_four_point.sampling_rate.setValue(m_cfg['sampling_rate'])
        self.tab_four_point.fpp_plot_var.setCurrentText('sheet_Rs')
        # 4PP doesn't carry a time-series MplCanvas — its histogram lives in
        # the right panel and updates from update_active_plot directly.
        # Van der Pauw
        if hasattr(self, 'tab_vdp'):
            self.tab_vdp.vdp_current.setValue(float(m_cfg.get('vdp_current', 1.0e-3)))
            self.tab_vdp.vdp_voltage_compliance.setValue(float(m_cfg.get('vdp_voltage_compliance', 5.0)))
            self.tab_vdp.vdp_voltage_range_auto.setChecked(bool(m_cfg.get('vdp_voltage_range_auto', True)))
            self.tab_vdp.vdp_thickness_cm.setValue(float(m_cfg.get('vdp_thickness_cm', 1.0e-4)))
            self.tab_vdp.vdp_settling_s.setValue(float(m_cfg.get('vdp_settling_s', 0.2)))
            self.tab_vdp.vdp_readings_per_polarity.setValue(int(m_cfg.get('vdp_readings_per_polarity', 1)))
            self.tab_vdp.nplc.setValue(float(m_cfg.get('nplc', 1.0)))
            self.tab_vdp.sampling_rate.setValue(float(m_cfg.get('sampling_rate', 10.0)))
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
        if mode == 'sweep': return self.tab_sweep
        if mode == 'vdp': return self.tab_vdp
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
                m_cfg['res_offset_comp'] = widget.res_offset_comp.isChecked()
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
                m_cfg['fpp_samples'] = int(widget.fpp_samples.value())
                # F84 correction-factor inputs. The temperature SpinBox uses
                # its specialValueText ('not measured') at the minimum value
                # (-50 C) as a sentinel; we map that to NaN downstream.
                if hasattr(widget, 'fpp_diameter_cm'):
                    m_cfg['fpp_diameter_cm'] = float(widget.fpp_diameter_cm.value())
                if hasattr(widget, 'fpp_geometry'):
                    m_cfg['fpp_geometry'] = widget.fpp_geometry.currentText()
                if hasattr(widget, 'fpp_temperature_c'):
                    t_val = widget.fpp_temperature_c.value()
                    m_cfg['fpp_temperature_c'] = float('nan') if t_val <= -49.999 else float(t_val)
                if hasattr(widget, 'fpp_dopant_type'):
                    m_cfg['fpp_dopant_type'] = widget.fpp_dopant_type.currentText()
                if hasattr(widget, 'fpp_delta_mode'):
                    m_cfg['fpp_delta_mode'] = widget.fpp_delta_mode.isChecked()
                if hasattr(widget, 'fpp_delta_settling'):
                    m_cfg['fpp_delta_settling'] = widget.fpp_delta_settling.value()
                if hasattr(widget, 'fpp_power_warn_w'):
                    m_cfg['fpp_power_warn_w'] = widget.fpp_power_warn_w.value()
                if hasattr(widget, 'fpp_power_stop_w'):
                    m_cfg['fpp_power_stop_w'] = widget.fpp_power_stop_w.value()
                if hasattr(widget, 'fpp_stop_on_overpower'):
                    m_cfg['fpp_stop_on_overpower'] = widget.fpp_stop_on_overpower.isChecked()
            elif mode == 'sweep':
                m_cfg['sweep_source'] = widget.sweep_source.currentText()
                m_cfg['sweep_start'] = widget.sweep_start.value()
                m_cfg['sweep_stop'] = widget.sweep_stop.value()
                m_cfg['sweep_step'] = widget.sweep_step.value()
                m_cfg['sweep_compliance'] = widget.sweep_compliance.value()
                m_cfg['sweep_delay'] = widget.sweep_delay.value()
                m_cfg['sweep_direction'] = widget.sweep_direction.currentText()
            elif mode == 'vdp':
                m_cfg['vdp_current'] = widget.vdp_current.value()
                m_cfg['vdp_voltage_compliance'] = widget.vdp_voltage_compliance.value()
                m_cfg['vdp_voltage_range_auto'] = widget.vdp_voltage_range_auto.isChecked()
                m_cfg['vdp_thickness_cm'] = widget.vdp_thickness_cm.value()
                m_cfg['vdp_settling_s'] = widget.vdp_settling_s.value()
                m_cfg['vdp_readings_per_polarity'] = int(widget.vdp_readings_per_polarity.value())
        except AttributeError as e:
            raise ValueError(f"UI Widgets not found for mode {mode}: {e}")
        # Read NPLC and sampling rate from the tab (overrides settings dialog)
        if hasattr(widget, 'nplc'):
            m_cfg['nplc'] = widget.nplc.value()
        elif hasattr(widget, 'sweep_nplc'):
            m_cfg['nplc'] = widget.sweep_nplc.value()
        else:
            m_cfg['nplc'] = self.user_settings['measurement']['nplc']
        if hasattr(widget, 'sampling_rate'):
            m_cfg['sampling_rate'] = widget.sampling_rate.value()
        else:
            m_cfg['sampling_rate'] = self.user_settings['measurement']['sampling_rate']
        # Handle run-until-stopped checkboxes (duration=0 means infinite)
        if mode == 'source_v' and hasattr(widget, 'vsource_run_continuous') and widget.vsource_run_continuous.isChecked():
            m_cfg['vsource_duration_hours'] = 0.0
        if mode == 'source_i' and hasattr(widget, 'isource_run_continuous') and widget.isource_run_continuous.isChecked():
            m_cfg['isource_duration_hours'] = 0.0
        m_cfg['settling_time'] = self.user_settings['measurement']['settling_time']
        m_cfg['gpib_address'] = self.user_settings['measurement']['gpib_address']
        return effective_settings

    def _require_sample_name(self) -> Optional[str]:
        """Return the trimmed sample name, prompting inline if empty.

        Returns the name on success, or None if the user cancelled or
        provided no text. On success the sample_input field is populated
        so the value is visible from the top bar for the rest of the run.
        """
        sample_name = self.sample_input.text().strip()
        if sample_name:
            return sample_name
        self.sample_input.setFocus()
        name, ok = QInputDialog.getText(
            self,
            "Sample Name",
            "Enter a sample name for this measurement:",
            QLineEdit.Normal,
            "",
        )
        if not ok:
            return None
        name = name.strip()
        if not name:
            return None
        self.sample_input.setText(name)
        return name

    def start_measurement(self, mode: str):
        if self.measurement_running:
            QMessageBox.warning(self, "Measurement Active", f"A measurement ({self.active_mode}) is already running. Please stop it first.")
            return
        if not self.current_user:
            QMessageBox.warning(self, "No User Selected", "Please select or create a user first.")
            return
        sample_name = self._require_sample_name()
        if not sample_name:
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
        self.data_buffers[mode].clear()
        if mode == 'sweep':
            widget.iv_canvas.clear_plot()
            widget._sweep_trace_count = 0
        elif mode == 'four_point':
            # 4PP visualizes via histogram + tables, no time-series MplCanvas
            self._clear_four_point_data()
        else:
            widget.canvas.clear_plot()
        widget.status_label.setText("Status: Sweeping..." if mode == 'sweep' else "Status: Running")
        widget.status_label.setStyleSheet("font-weight: bold; color: green;")
        if getattr(widget, 'mark_event_button', None): widget.mark_event_button.setEnabled(True)
        self.log_status(f"Starting {mode} measurement for sample: {sample_name}..."); self.statusBar().showMessage(f"Measurement running ({mode})...")
        self.measurement_worker = MeasurementWorker(mode=mode, sample_name=sample_name, username=self.current_user, settings=current_settings)
        self.measurement_worker.data_point.connect(self.update_data)
        self.measurement_worker.status_update.connect(self.log_status_from_worker)
        self.measurement_worker.measurement_complete.connect(self.on_measurement_complete)
        self.measurement_worker.error_occurred.connect(self.on_error)
        self.measurement_worker.compliance_hit.connect(self.on_compliance_hit)
        self.measurement_worker.sweep_complete.connect(self.on_sweep_complete)
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
                if getattr(widget, 'mark_event_button', None):
                    widget.mark_event_button.setEnabled(False)
                if getattr(widget, 'pause_button', None):
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
            text, ok = QInputDialog.getText(self, "Mark Event",
                                            "Event label:", QLineEdit.Normal, "MARK")
            if ok and text.strip():
                label = text.strip()
                self.measurement_worker.mark_event(label)
                self.log_status(f"⭐ Event marked: {label}", color="purple")
                widget = self.get_widget_for_mode(self.active_mode)
                if widget and getattr(widget, 'mark_event_button', None):
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

        # Update live readout with engineering notation
        widget = self.get_widget_for_mode(self.active_mode)
        is_bd = (compliance_status != 'OK')
        if widget and hasattr(widget, 'live_readout'):
            if self.active_mode == 'resistance':
                r = value.get('resistance', float('nan'))
                if np.isfinite(r):
                    parts = [format_engineering(r, '\u03a9')]
                    # In resistance mode the worker doesn't emit V/I, but we know
                    # the configured test current \u2014 compute P = I^2 * R.
                    try:
                        i_test = float(widget.res_test_current.value())
                    except Exception:
                        i_test = float('nan')
                    if np.isfinite(i_test) and i_test != 0:
                        parts.append(f"P: {format_engineering(abs(i_test * i_test * r), 'W')}")
                    widget.live_readout.setText("   ".join(parts))
                else:
                    widget.live_readout.setText("-- \u03a9")
            elif self.active_mode in ('source_v', 'source_i'):
                v = value.get('voltage', float('nan'))
                i = value.get('current', float('nan'))
                parts = []
                if np.isfinite(v):
                    parts.append(f"V: {format_engineering(v, 'V')}")
                if np.isfinite(i):
                    parts.append(f"I: {format_engineering(i, 'A')}")
                if np.isfinite(v) and np.isfinite(i) and i != 0:
                    ohm = '\u03a9'
                    parts.append(f"R: {format_engineering(v/i, ohm)}")
                if np.isfinite(v) and np.isfinite(i):
                    parts.append(f"P: {format_engineering(abs(v * i), 'W')}")
                widget.live_readout.setText("   ".join(parts) if parts else "--")
            elif self.active_mode == 'four_point':
                self._update_fpp_live_readout(widget, value, is_bd)

        # Append a row to 4PP table and update stats live
        if self.active_mode == 'four_point':
            w = self.tab_four_point
            v = value.get('voltage', float('nan'))
            i = value.get('current', float('nan'))
            from ..calculations import (
                calculate_four_point_probe,
                calculate_four_point_probe_bound,
            )
            fpp_kwargs = dict(
                spacing_cm=w.fpp_spacing_cm.value(),
                thickness_um=w.fpp_thickness_um.value(),
                k_factor=w.fpp_k_factor.value() or 4.532,
                alpha=w.fpp_alpha.value(),
                model=w.fpp_model.currentText(),
            )
            if is_bd:
                result = calculate_four_point_probe_bound(
                    v_compliance=w.fpp_voltage_compliance.value(),
                    measured_current=i,
                    source_current=w.fpp_current.value(),
                    **fpp_kwargs,
                )
            else:
                result = calculate_four_point_probe(voltage=v, current=i, **fpp_kwargs)
            ts, _, _ = buffer.get_data_for_plot('voltage')
            elapsed = (timestamp - ts[0]) if ts else 0.0
            row = (
                elapsed, v, i,
                result.ratio, result.sheet_resistance,
                result.resistivity, result.conductivity,
                compliance_status, event,
            )
            w._fpp_rows.append(row)
            self._append_four_point_row(row)
            self._update_four_point_stats()

    def update_active_plot(self):
        if not self.measurement_running or self.active_mode is None or not self.user_settings:
            return
        if self.active_mode == 'sweep':
            return  # I-V sweep uses IVCanvas.plot_sweep(), not time-series update_plot()
        if self.active_mode == 'four_point':
            return  # 4PP visualizes via fpp_histogram (right panel); no time-series canvas
        mode = self.active_mode; widget = self.get_widget_for_mode(mode); buffer = self.data_buffers[mode]
        if not widget or not buffer:
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
            else:  # source_i
                var = widget.i_plot_var.currentText() if hasattr(widget, 'i_plot_var') else 'voltage'
                timestamps, values, compliance_list = buffer.get_data_for_plot(var)
                stats = buffer.get_statistics(var)
            widget.canvas.update_plot(timestamps, values, compliance_list, stats, self.current_user, self.sample_input.text())

    def _update_fpp_live_readout(self, widget, value, is_bd):
        """Live readout for 4PP. Bare numbers only when the point is valid;
        BD points show the upper bound on σ with explicit ≤ and amber styling.
        """
        v = value.get('voltage', float('nan'))
        i = value.get('current', float('nan'))
        if is_bd:
            from ..calculations import (
                calculate_four_point_probe_bound, estimate_current_floor,
            )
            src_i = widget.fpp_current.value()
            i_floor = estimate_current_floor(src_i)
            bound = calculate_four_point_probe_bound(
                v_compliance=widget.fpp_voltage_compliance.value(),
                measured_current=i,
                source_current=src_i,
                spacing_cm=widget.fpp_spacing_cm.value(),
                thickness_um=widget.fpp_thickness_um.value(),
                k_factor=widget.fpp_k_factor.value() or 4.532,
                alpha=widget.fpp_alpha.value(),
                model=widget.fpp_model.currentText(),
            )
            v_txt = f"V: {format_engineering(v, 'V')} (clamped)" if np.isfinite(v) else "V: --"
            i_txt = f"I: < {format_engineering(i_floor, 'A')}"
            sigma_txt = (
                f"σ ≤ {bound.conductivity:.3g} S/cm"
                if np.isfinite(bound.conductivity) else "σ: --"
            )
            widget.live_readout.setText(f"{v_txt}   {i_txt}   {sigma_txt}   [BD]")
            widget.live_readout.setStyleSheet(
                "color: #a85a00; background: #fff4e0; "
                "border: 1px solid #e8a85b; border-radius: 4px; padding: 4px;"
            )
            widget.live_readout.setToolTip(
                "Below detection: V at compliance, I below the meter's noise "
                "floor on this range. Lower source current to tighten the bound, "
                "or check probe contact."
            )
        else:
            parts = []
            if np.isfinite(v):
                parts.append(f"V: {format_engineering(v, 'V')}")
            if np.isfinite(i):
                parts.append(f"I: {format_engineering(i, 'A')}")
            if np.isfinite(v) and np.isfinite(i) and i != 0:
                parts.append(f"R: {format_engineering(v/i, 'Ω')}")
            if np.isfinite(v) and np.isfinite(i):
                parts.append(f"P: {format_engineering(abs(v * i), 'W')}")
            widget.live_readout.setText("   ".join(parts) if parts else "--")
            widget.live_readout.setStyleSheet(
                "color: #222; background: #f0f0f0; border: 1px solid #ccc; "
                "border-radius: 4px; padding: 4px;"
            )
            widget.live_readout.setToolTip(
                "Live measurement reading — updates in real time during measurement"
            )

    def _append_four_point_row(self, row):
        w = self.tab_four_point
        table = w.fpp_table
        table.insertRow(table.rowCount())
        is_bd = (len(row) >= 8 and row[7] != 'OK')
        bd_brush = QBrush(QColor("#fff4e0")) if is_bd else None
        bd_fg = QBrush(QColor("#a85a00")) if is_bd else None
        for col, val in enumerate(row):
            if isinstance(val, float):
                text = f"{val:.6g}"
                # Mark bound columns (V/I, Rs, ρ, σ) with ≤/≥ when BD
                if is_bd and col in (3, 4, 5):
                    text = f"≥ {text}"
                elif is_bd and col == 6:
                    text = f"≤ {text}"
            else:
                text = str(val)
            item = QTableWidgetItem(text)
            if bd_brush is not None:
                item.setBackground(bd_brush)
                item.setForeground(bd_fg)
            table.setItem(table.rowCount()-1, col, item)
        table.scrollToBottom()

    def _update_four_point_stats(self):
        w = self.tab_four_point
        rows = w._fpp_rows
        n = len(rows)
        valid_rows = [r for r in rows if len(r) < 8 or r[7] == 'OK']
        bd_count = n - len(valid_rows)
        if bd_count:
            w.fpp_n_label.setText(f"{len(valid_rows)} valid · {bd_count} BD")
            w.fpp_n_label.setStyleSheet("color: #a85a00;")
        else:
            w.fpp_n_label.setText(str(n))
            w.fpp_n_label.setStyleSheet("")
        import math
        def stats(idx):
            arr = [r[idx] for r in valid_rows]
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

        # Update histogram (throttle: every 5th point after 200), valid points only
        if hasattr(w, 'fpp_histogram') and (n <= 200 or n % 5 == 0):
            rs_values = [r[4] for r in valid_rows]
            if hasattr(w, '_fpp_spots') and len(w._fpp_spots) >= 2:
                names = [s['name'] for s in w._fpp_spots]
                means = [s['rs_mean'] for s in w._fpp_spots]
                stds = [s['rs_std'] for s in w._fpp_spots]
                w.fpp_histogram.update_bar_chart(names, means, stds)
            else:
                w.fpp_histogram.update_histogram(rs_values, 'Rs (Ω/□)')

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
            txt = f"ρ = 2π·s·(V/I) = {2*np.pi*s:.4g}·(V/I) Ω·cm"
        elif model in ('thin_film','finite_thin'):
            # Show both Rs and rho forms
            txt = f"Rs = {k:.4g}·(V/I) Ω/□\nρ = {k:.4g}·t·(V/I) = {k*t_cm:.4g}·(V/I) Ω·cm"
            if model == 'thin_film' and alpha and alpha != 1.0:
                txt += f"\n(α applied: Rs = {k*alpha:.4g}·(V/I), ρ = {k*alpha:.4g}·t·(V/I))"
        else:
            txt = f"ρ = α·2π·s·(V/I) = α·{2*np.pi*s:.4g}·(V/I) Ω·cm"
        w.fpp_model_info.setText(txt)

    def on_sweep_complete(self, voltages: list, currents: list, comp_list: list):
        """Handle I-V sweep data from worker."""
        w = self.tab_sweep
        n = len(voltages)
        # Determine sweep trace label
        if not hasattr(w, '_sweep_trace_count'):
            w._sweep_trace_count = 0
        w._sweep_trace_count += 1
        colors = ['blue', 'red', 'green', 'orange', 'purple']
        color = colors[(w._sweep_trace_count - 1) % len(colors)]
        label = 'Forward' if w._sweep_trace_count == 1 else f'Reverse' if w._sweep_trace_count == 2 else f'Trace {w._sweep_trace_count}'
        w.iv_canvas.plot_sweep(voltages, currents, label=label, color=color)

        # Update labels based on source function
        src = w.sweep_source.currentText()
        if src == 'voltage':
            w.iv_canvas.set_labels('Voltage (V)', 'Current (A)', 'I-V Characteristic')
        else:
            w.iv_canvas.set_labels('Current (A)', 'Voltage (V)', 'V-I Characteristic')

        # Update live readout with summary
        comp_count = sum(1 for c in comp_list if c != 'OK')
        w.live_readout.setText(f"{n} points | {comp_count} in compliance" if comp_count else f"{n} points acquired")
        self.log_status(f"Sweep trace plotted: {n} points", color="darkGreen")

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
            # 4PP drives its own live_readout styling (BD vs OK) from update_data;
            # for other modes, keep the existing brief red flash for visibility.
            if hasattr(widget, 'live_readout') and mode != 'four_point':
                widget.live_readout.setStyleSheet("color: red; background: #ffe0e0; border: 2px solid red; border-radius: 4px; padding: 4px;")
                QTimer.singleShot(2000, lambda: widget.live_readout.setStyleSheet(
                    "color: #222; background: #f0f0f0; border: 1px solid #ccc; border-radius: 4px; padding: 4px;"))
        self.log_status(f"⚠️ {compliance_type} Compliance Hit during {mode} measurement!", color="orange")
        # Non-blocking: show in status bar instead of modal popup
        self.statusBar().showMessage(f"⚠️ {compliance_type} COMPLIANCE — {mode} measurement", 10000)

    def on_worker_finished(self):
        self.log_status(f"Measurement worker thread ({self.active_mode}) finished.", color="grey")
        self.reset_ui_after_measurement()

    def reset_ui_after_measurement(self):
        if not self.active_mode:
            return
        finished_mode = self.active_mode
        # Capture filename before clearing the worker reference
        saved_file = None
        if self.measurement_worker and hasattr(self.measurement_worker, 'filename'):
            saved_file = self.measurement_worker.filename
        self.measurement_running = False
        self.active_mode = None
        self.measurement_worker = None
        self.plot_timer.stop()
        self.sample_input.setEnabled(True); self.change_user_button.setEnabled(True)
        widget = self.get_widget_for_mode(finished_mode)
        if widget:
            widget.status_label.setText("Status: Idle"); widget.status_label.setStyleSheet("font-weight: bold; color: black;")
            widget.start_button.setEnabled(True); widget.stop_button.setEnabled(False)
            if getattr(widget, 'pause_button', None):
                widget.pause_button.setEnabled(False); widget.pause_button.setChecked(False)
            if getattr(widget, 'mark_event_button', None):
                widget.mark_event_button.setEnabled(False)
        self.set_all_controls_enabled(True)
        self.shortcut_mark.setEnabled(False)
        # Show last saved file path in status bar
        if saved_file:
            self.statusBar().showMessage(f"Data saved: {saved_file}")
        else:
            self.statusBar().showMessage("Ready", 0)
        self.log_status("Measurement stopped. UI controls re-enabled.")

    def set_controls_for_mode(self, mode: str, running: bool):
        widget = self.get_widget_for_mode(mode)
        if widget:
            widget.start_button.setEnabled(not running); widget.stop_button.setEnabled(running)
            if getattr(widget, 'pause_button', None):
                widget.pause_button.setEnabled(running)
            for i in range(widget.param_layout.rowCount()):
                field = widget.param_layout.itemAt(i, QFormLayout.FieldRole)
                if field and field.widget(): field.widget().setEnabled(not running)
                label = widget.param_layout.itemAt(i, QFormLayout.LabelRole)
                if label and label.widget(): label.widget().setEnabled(not running)
            if getattr(widget, 'mark_event_button', None):
                widget.mark_event_button.setEnabled(running)
            # Re-enable plot variable selector during measurement
            for attr in ('v_plot_var', 'i_plot_var', 'fpp_plot_var'):
                if hasattr(widget, attr):
                    getattr(widget, attr).setEnabled(True)

    def set_all_controls_enabled(self, enabled: bool, except_mode: Optional[str] = None):
        for mode in ['resistance', 'source_v', 'source_i', 'four_point', 'sweep', 'vdp']:
            if mode == except_mode: continue
            widget = self.get_widget_for_mode(mode)
            if widget:
                widget.start_button.setEnabled(enabled); widget.stop_button.setEnabled(False)
                if getattr(widget, 'pause_button', None):
                    widget.pause_button.setEnabled(False); widget.pause_button.setChecked(False)
                if getattr(widget, 'mark_event_button', None):
                    widget.mark_event_button.setEnabled(False)
                for i in range(widget.param_layout.rowCount()):
                    field = widget.param_layout.itemAt(i, QFormLayout.FieldRole)
                    if field and field.widget(): field.widget().setEnabled(enabled)
                    label = widget.param_layout.itemAt(i, QFormLayout.LabelRole)
                    if label and label.widget(): label.widget().setEnabled(enabled)

    def handle_tab_change(self, index):
        if self.measurement_running:
            # Allow viewing other tabs (read-only) during measurement
            # The active mode's controls are already locked by set_all_controls_enabled
            current_widget = self.main_tabs.widget(index)
            if hasattr(current_widget, 'mode') and current_widget.mode != self.active_mode:
                self.statusBar().showMessage(f"Viewing {current_widget.mode} tab (read-only) — {self.active_mode} measurement running", 3000)

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
        # Calculate conductivity safely, avoiding divide by zero warnings
        with np.errstate(divide='ignore', invalid='ignore'):
            sigma = np.where(np.isfinite(rho) & (rho != 0), 1.0 / rho, np.nan)
        
        def safe_stat(a):
            """Calculate mean and std with proper handling of empty arrays and warnings"""
            valid_values = a[np.isfinite(a)]
            if len(valid_values) == 0:
                return float('nan'), float('nan')
            elif len(valid_values) == 1:
                return float(valid_values[0]), 0.0
            else:
                with np.errstate(invalid='ignore'):
                    mean_val = float(np.nanmean(a))
                    std_val = float(np.nanstd(a, ddof=1))
                    return mean_val, std_val
        
        Rs_mean, Rs_std = safe_stat(Rs)
        rho_mean, rho_std = safe_stat(rho)
        sigma_mean, sigma_std = safe_stat(sigma)
        filename, _ = QFileDialog.getSaveFileName(self, "Save Summary", "fpp_summary.csv", "CSV Files (*.csv)")
        if not filename:
            return
        import csv
        try:
            with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(["4-Point Probe Summary"])
                w.writerow(["Sample", self.sample_input.text()])
                w.writerow(["User", self.current_user or "-"])
                w.writerow(["Model", model])
                w.writerow(["Spacing s (cm)", s])
                w.writerow(["Thickness t (cm)", t_thick])
                w.writerow(["Alpha", alpha])
                w.writerow([])
                def safe_format(val):
                    """Format value safely, handling NaN and infinite values"""
                    if np.isnan(val) or np.isinf(val):
                        return "N/A"
                    return f"{val:.6g}"
                
                w.writerow(["Metric", "Mean", "StdDev"])
                w.writerow(["Sheet Resistance (Ω/□)", safe_format(Rs_mean), safe_format(Rs_std)])
                w.writerow(["Resistivity (Ω·cm)", safe_format(rho_mean), safe_format(rho_std)])
                w.writerow(["Conductivity (S/cm)", safe_format(sigma_mean), safe_format(sigma_std)])

                # Multi-spot section (if spots were saved)
                fpp_widget = self.tab_four_point
                spots = getattr(fpp_widget, '_fpp_spots', [])
                if spots:
                    w.writerow([])
                    w.writerow(["Per-Spot Results"])
                    w.writerow(["Spot", "N", "Rs Mean (Ω/□)", "Rs Std", "Rs RSD%",
                                "ρ Mean (Ω·cm)", "ρ Std", "σ Mean (S/cm)", "σ Std"])
                    for sp in spots:
                        rs_rsd = (sp['rs_std'] / sp['rs_mean'] * 100) if sp['rs_mean'] != 0 else 0
                        w.writerow([sp['name'], sp['n'],
                                    safe_format(sp['rs_mean']), safe_format(sp['rs_std']), f"{rs_rsd:.2f}",
                                    safe_format(sp['rho_mean']), safe_format(sp['rho_std']),
                                    safe_format(sp['sigma_mean']), safe_format(sp['sigma_std'])])
                    # Include unsaved current readings as "Current"
                    if fpp_widget._fpp_rows:
                        w.writerow(["(Current unsaved)", len(fpp_widget._fpp_rows),
                                    safe_format(Rs_mean), safe_format(Rs_std), "", "", "", "", ""])
                    # Inter-spot uniformity
                    if len(spots) >= 2:
                        spot_rs = [sp['rs_mean'] for sp in spots if sp['rs_mean'] != 0]
                        if spot_rs:
                            inter_mean = np.mean(spot_rs)
                            inter_std = np.std(spot_rs, ddof=1)
                            inter_rsd = (inter_std / inter_mean * 100) if inter_mean != 0 else 0
                            w.writerow([])
                            w.writerow(["Inter-spot Uniformity"])
                            w.writerow(["Rs Mean-of-Means (Ω/□)", safe_format(inter_mean)])
                            w.writerow(["Rs Std-of-Means (Ω/□)", safe_format(inter_std)])
                            w.writerow(["Inter-spot RSD%", f"{inter_rsd:.2f}"])

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
            self.config_manager.set_gpib_address(addr)
            # Refresh the in-memory cache so the next measurement reads the
            # new address. Without this, the address only takes effect after
            # bouncing through Settings → Save.
            if self.current_user:
                self.user_settings = self.config_manager.get_user_settings(self.current_user)
            self.log_status(f"GPIB address set to: {addr}")
            QMessageBox.information(self, "GPIB Updated", f"GPIB address updated to {addr}. Start the measurement again.")

    def clear_all_plots(self):
        self.tab_resistance.canvas.clear_plot()
        self.tab_voltage_source.canvas.clear_plot()
        self.tab_current_source.canvas.clear_plot()
        self.tab_sweep.iv_canvas.clear_plot()
        self.tab_sweep._sweep_trace_count = 0

        # 4PP visualizes via histogram + tables; reset those data structures
        self._clear_four_point_data()

        self.log_status("All plots and data cleared.")

    def _clear_four_point_data(self):
        """Clear 4-Point Probe current spot data (table, stats, rows)."""
        widget = self.tab_four_point
        if hasattr(widget, '_fpp_rows') and hasattr(widget, 'fpp_table'):
            widget._fpp_rows.clear()
            widget.fpp_table.setRowCount(0)
            if hasattr(widget, 'fpp_n_label'):
                widget.fpp_n_label.setText("0")
            if hasattr(widget, 'fpp_rs_label'):
                widget.fpp_rs_label.setText("--")
            if hasattr(widget, 'fpp_rho_label'):
                widget.fpp_rho_label.setText("--")
            if hasattr(widget, 'fpp_sigma_label'):
                widget.fpp_sigma_label.setText("--")
            if hasattr(widget, 'fpp_histogram'):
                widget.fpp_histogram.clear_histogram()

    def _save_fpp_spot(self):
        """Archive current readings as a named spot, reset for next position."""
        w = self.tab_four_point
        if not hasattr(w, '_fpp_rows') or not w._fpp_rows:
            self.log_status("No readings to save as a spot.", color="orange")
            return
        import math
        name = w.fpp_spot_name.text().strip() or f"Spot {w._fpp_spot_counter}"
        rows = list(w._fpp_rows)
        n = len(rows)

        def stat(idx):
            arr = [r[idx] for r in rows if isinstance(r[idx], (int, float)) and not math.isnan(r[idx])]
            if not arr:
                return 0.0, 0.0
            mean = float(np.mean(arr))
            std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
            return mean, std

        rs_mean, rs_std = stat(4)
        rho_mean, rho_std = stat(5)
        sigma_mean, sigma_std = stat(6)

        spot = {
            'name': name, 'n': n, 'rows': rows,
            'rs_mean': rs_mean, 'rs_std': rs_std,
            'rho_mean': rho_mean, 'rho_std': rho_std,
            'sigma_mean': sigma_mean, 'sigma_std': sigma_std,
        }
        w._fpp_spots.append(spot)

        # Update spots summary table
        table = w.fpp_spots_table
        row_idx = table.rowCount()
        table.insertRow(row_idx)
        rsd = (rs_std / rs_mean * 100) if rs_mean != 0 else 0
        for col, val in enumerate([name, str(n), f"{rs_mean:.5g}", f"{rs_std:.3g}", f"{rsd:.2f}"]):
            table.setItem(row_idx, col, QTableWidgetItem(val))

        self.log_status(f"Spot '{name}' saved: N={n}, Rs={rs_mean:.5g} ± {rs_std:.3g} Ω/□", color="darkGreen")

        # Increment auto-name and clear current readings
        w._fpp_spot_counter += 1
        w.fpp_spot_name.setText(f"Spot {w._fpp_spot_counter}")
        self._clear_four_point_data()

        # Update histogram to bar chart if ≥2 spots
        if len(w._fpp_spots) >= 2:
            names = [s['name'] for s in w._fpp_spots]
            means = [s['rs_mean'] for s in w._fpp_spots]
            stds = [s['rs_std'] for s in w._fpp_spots]
            w.fpp_histogram.update_bar_chart(names, means, stds)

    def _clear_all_fpp_spots(self):
        """Clear all saved spots and current readings."""
        w = self.tab_four_point
        if hasattr(w, '_fpp_spots'):
            w._fpp_spots.clear()
        if hasattr(w, '_fpp_spot_counter'):
            w._fpp_spot_counter = 1
        if hasattr(w, 'fpp_spot_name'):
            w.fpp_spot_name.setText("Spot 1")
        if hasattr(w, 'fpp_spots_table'):
            w.fpp_spots_table.setRowCount(0)
        self._clear_four_point_data()
        self.log_status("All spots and readings cleared.")

    def _null_cables(self):
        """Measure cable resistance and store as software null reference."""
        if self.measurement_running:
            QMessageBox.warning(self, "Busy", "Cannot null cables during a measurement.")
            return
        if not self.user_settings:
            QMessageBox.warning(self, "No Settings", "Please select a user first.")
            return
        reply = QMessageBox.question(
            self, "Null Cables",
            "Short the probe tips together, then click OK to measure cable resistance.\n\n"
            "This value will be subtracted from all future resistance readings.",
            QMessageBox.Ok | QMessageBox.Cancel)
        if reply != QMessageBox.Ok:
            return
        addr = self.user_settings['measurement']['gpib_address']
        try:
            from ..instrument import Keithley2400
            k = Keithley2400(addr).connect()
            k.write("*RST"); import time; time.sleep(0.5)
            k.write("*CLS")
            k.write(":SENS:FUNC:CONC OFF")
            k.write(":SENS:FUNC 'RES'")
            k.write(":SENS:RES:MODE MAN")
            k.write(":SOUR:FUNC CURR")
            test_current = self.tab_resistance.res_test_current.value()
            k.write(f":SOUR:CURR:RANG {abs(test_current)}")
            k.write(f":SOUR:CURR {test_current}")
            k.write(":SENS:VOLT:PROT 5")
            k.write(":SENS:RES:NPLC 10")  # high accuracy for null
            k.write(":FORM:ELEM RES")
            k.write(":OUTP ON"); time.sleep(0.5)
            ref = float(k.query(":READ?").strip().split(',')[0])
            k.write(":OUTP OFF")
            k.close()

            if not np.isfinite(ref) or ref < 0:
                QMessageBox.warning(self, "Null Failed", f"Invalid reading: {ref}. Ensure probes are shorted.")
                return

            # Store in settings (software subtraction — 2400 series lacks :SENS:RES:REL)
            self.user_settings['measurement']['res_cable_null'] = ref
            ohm = '\u03a9'
            self.tab_resistance.null_label.setText(f"Cable null: {format_engineering(ref, ohm)}")
            self.tab_resistance.null_label.setStyleSheet("color: green; font-weight: bold;")
            self.log_status(f"Cable null set: {format_engineering(ref, ohm)} (software subtraction)", color="darkGreen")
        except Exception as e:
            QMessageBox.critical(self, "Null Failed", f"Error during cable null: {e}")

    def _clear_cable_null(self):
        """Remove cable null reference."""
        if self.user_settings:
            self.user_settings['measurement']['res_cable_null'] = 0.0
        self.tab_resistance.null_label.setText("Cable null: OFF")
        self.tab_resistance.null_label.setStyleSheet("color: grey;")
        self.log_status("Cable null cleared.")

    def show_about(self):
        about_text = f"""
        <h2>ResistaMet GUI</h2>
        <p>Version: {__version__}</p>
        <p>Author: Brenden Ferland</p>
        <hr>
        <p>A graphical interface for controlling Keithley 2400/2450 SourceMeter units, providing modes for:</p>
        <ul>
            <li>Resistance Measurement (Source Current, Measure Resistance)</li>
            <li>Voltage Source (Source Voltage, Measure Current)</li>
            <li>Current Source (Source Current, Measure Voltage)</li>
            <li>Four-Point Probe (Sheet Resistance, Resistivity, Conductivity)</li>
        </ul>
        <p>Features: real-time plotting, dual-format data export (JSON + CSV),
        user profiles, compliance monitoring, and event markers.</p>
        """
        QMessageBox.about(self, f"About ResistaMet GUI v{__version__}", about_text)

    def test_instrument_connection(self):
        """Quick connection test — queries *IDN? at the configured GPIB address."""
        if not self.user_settings:
            QMessageBox.warning(self, "No Settings", "Please select a user first.")
            return
        addr = self.user_settings['measurement']['gpib_address']
        self.statusBar().showMessage(f"Testing connection to {addr}...")
        try:
            import pyvisa
            rm = pyvisa.ResourceManager()
            resources = rm.list_resources()
            if addr not in resources:
                rm.close()
                QMessageBox.warning(self, "Connection Failed",
                                    f"Instrument at '{addr}' not found.\n\nAvailable: {', '.join(resources) if resources else 'none'}")
                self.statusBar().showMessage("Connection failed", 5000)
                return
            dev = rm.open_resource(addr)
            dev.timeout = 5000
            try:
                dev.read_termination = '\n'
                dev.write_termination = '\n'
            except Exception:
                pass
            idn = dev.query("*IDN?").strip()
            dev.close()
            rm.close()
            QMessageBox.information(self, "Connection OK", f"Connected to:\n{idn}")
            self.log_status(f"Connection test OK: {idn}", color="darkGreen")
            self.statusBar().showMessage(f"Connected: {idn}", 5000)
        except Exception as e:
            QMessageBox.critical(self, "Connection Failed", f"Error connecting to {addr}:\n{str(e)}")
            self.statusBar().showMessage("Connection failed", 5000)

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
