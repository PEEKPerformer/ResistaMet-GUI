import os
from PyQt5.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QFormLayout, QHBoxLayout,
    QLineEdit, QPushButton, QDoubleSpinBox, QSpinBox, QComboBox, QLabel,
    QFileDialog, QMessageBox, QCheckBox
)
from PyQt5.QtCore import Qt

import pyvisa

from ..config import ConfigManager
from .widgets import EngineeringSpinBox, NoScrollSpinBox


class SettingsDialog(QDialog):
    def __init__(self, config_manager: ConfigManager, username=None, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.username = username

        if username:
            global_settings = config_manager.config
            user_settings_raw = config_manager.get_user_settings(username)
            self.settings = {
                'measurement': {**global_settings['measurement'], **user_settings_raw.get('measurement', {})},
                'display': {**global_settings['display'], **user_settings_raw.get('display', {})},
                'file': {**global_settings['file'], **user_settings_raw.get('file', {})},
            }
            self.setWindowTitle(f"Settings for {username}")
        else:
            self.settings = {
                'measurement': dict(config_manager.config['measurement']),
                'display': dict(config_manager.config['display']),
                'file': dict(config_manager.config['file'])
            }
            self.setWindowTitle("Global Settings")

        self.init_ui()
        self.load_settings()

    def init_ui(self):
        self.setMinimumWidth(600)
        self.tabs = QTabWidget()
        self.measurement_tab = self.create_measurement_tab()
        self.display_tab = self.create_display_tab()
        self.file_tab = self.create_file_tab()
        self.tabs.addTab(self.measurement_tab, "Measurement")
        self.tabs.addTab(self.display_tab, "Display")
        self.tabs.addTab(self.file_tab, "File")
        self.save_button = QPushButton("Save")
        self.cancel_button = QPushButton("Cancel")
        self.save_button.clicked.connect(self.save_settings)
        self.cancel_button.clicked.connect(self.reject)
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.tabs)
        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)

    def create_measurement_tab(self):
        tab = QWidget()
        main_layout = QVBoxLayout()

        # General instrument settings
        general_layout = QFormLayout()
        self.gpib_address = QLineEdit()
        self.gpib_address.setToolTip("VISA resource string for the instrument.\nExample: GPIB0::24::INSTR\nUse 'Detect Devices' to scan for available instruments.")
        self.detect_gpib_button = QPushButton("Detect Devices")
        self.detect_gpib_button.setToolTip("Scan for VISA instruments on all available buses.")
        self.detect_gpib_button.clicked.connect(self.detect_gpib_devices)
        gpib_layout = QHBoxLayout()
        gpib_layout.addWidget(self.gpib_address)
        gpib_layout.addWidget(self.detect_gpib_button)
        general_layout.addRow("GPIB Address:", gpib_layout)

        self.sampling_rate = QDoubleSpinBox(decimals=1, minimum=0.1, maximum=100.0, singleStep=1.0, suffix=" Hz")
        self.sampling_rate.setToolTip("Default readings per second. Can be overridden per-tab.\nActual rate may be limited by NPLC and instrument speed.")
        general_layout.addRow("Sampling Rate:", self.sampling_rate)
        self.nplc = QDoubleSpinBox(decimals=2, minimum=0.01, maximum=10.0, singleStep=0.1)
        self.nplc.setToolTip("Default Number of Power Line Cycles. Can be overridden per-tab.\n0.01 = fast/noisy, 1 = balanced, 10 = slow/precise.")
        general_layout.addRow("NPLC:", self.nplc)
        self.settling_time = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=10.0, singleStep=0.1, suffix=" s")
        self.settling_time.setToolTip("Delay after turning on output before taking the first reading.\nAllows the DUT and instrument to stabilize.")
        general_layout.addRow("Settling Time:", self.settling_time)
        main_layout.addLayout(general_layout)

        # Resistance mode defaults
        res_layout = QFormLayout()
        self.res_test_current = EngineeringSpinBox(unit='A', minimum=1e-7, maximum=3.0, default=1e-3)
        self.res_test_current.setToolTip("DC current sourced through the DUT to measure resistance.\nAccepts: 1mA, 100µA, 0.001, etc.")
        res_layout.addRow("Test Current:", self.res_test_current)
        self.res_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=200.0, singleStep=0.1, suffix=" V")
        self.res_voltage_compliance.setToolTip("Maximum voltage allowed across the DUT.")
        res_layout.addRow("Voltage Compliance:", self.res_voltage_compliance)
        self.res_measurement_type = QComboBox()
        self.res_measurement_type.addItems(["2-wire", "4-wire"])
        self.res_measurement_type.setToolTip("2-wire: includes lead resistance. 4-wire: eliminates it.")
        res_layout.addRow("Measurement Type:", self.res_measurement_type)
        self.res_auto_range = None  # controlled per-mode in the main UI
        main_layout.addLayout(res_layout)

        # Voltage source mode defaults
        vsrc_layout = QFormLayout()
        self.vsource_voltage = EngineeringSpinBox(unit='V', minimum=-200.0, maximum=200.0, default=1.0, allow_negative=True)
        self.vsource_voltage.setToolTip("DC voltage to apply to the DUT.\nAccepts: 100mV, 1V, -0.5V, etc.")
        vsrc_layout.addRow("Source Voltage:", self.vsource_voltage)
        self.vsource_current_compliance = EngineeringSpinBox(unit='A', minimum=1e-7, maximum=3.0, default=0.1)
        self.vsource_current_compliance.setToolTip("Maximum current allowed through the DUT.\nAccepts: 100mA, 1mA, 0.1A, etc.")
        vsrc_layout.addRow("Current Compliance:", self.vsource_current_compliance)
        self.vsource_duration_hours = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        self.vsource_duration_hours.setToolTip("Measurement duration. 0 = run until stopped.")
        vsrc_layout.addRow("Duration (hours):", self.vsource_duration_hours)
        main_layout.addLayout(vsrc_layout)

        # Current source mode defaults
        isrc_layout = QFormLayout()
        self.isource_current = EngineeringSpinBox(unit='A', minimum=-3.0, maximum=3.0, default=1e-3, allow_negative=True)
        self.isource_current.setToolTip("DC current to source through the DUT.\nAccepts: 1mA, -100µA, 0.001, etc.")
        isrc_layout.addRow("Source Current:", self.isource_current)
        self.isource_voltage_compliance = QDoubleSpinBox(decimals=2, minimum=0.1, maximum=200.0, singleStep=0.1, suffix=" V")
        self.isource_voltage_compliance.setToolTip("Maximum voltage allowed across the DUT.")
        isrc_layout.addRow("Voltage Compliance:", self.isource_voltage_compliance)
        self.isource_duration_hours = QDoubleSpinBox(decimals=2, minimum=0.0, maximum=168.0, singleStep=0.5, suffix=" h")
        self.isource_duration_hours.setToolTip("Measurement duration. 0 = run until stopped.")
        isrc_layout.addRow("Duration (hours):", self.isource_duration_hours)
        self.stop_on_compliance = QCheckBox("Stop on compliance")
        self.stop_on_compliance.setToolTip("Automatically stop the measurement when compliance is reached.\nUseful for protecting sensitive samples.")
        main_layout.addLayout(isrc_layout)
        main_layout.addWidget(self.stop_on_compliance)

        # Advanced instrument settings
        adv_layout = QFormLayout()
        self.auto_zero = QComboBox()
        self.auto_zero.addItems(["on", "once", "off"])
        self.auto_zero.setToolTip(
            "Controls auto-zero calibration:\n"
            "• ON: Most accurate — takes 3 measurements per reading (signal + reference + zero)\n"
            "• ONCE: Fast mode — calibrates once at start, then disables. ~3x faster, slight drift over time\n"
            "• OFF: Fastest — no zero calibration. Will drift. Use only for short, high-speed measurements")
        adv_layout.addRow("Auto Zero:", self.auto_zero)

        self.filter_enabled = QCheckBox("Enable Hardware Filter")
        self.filter_enabled.setToolTip(
            "Enable the Keithley's built-in averaging filter.\n"
            "Averages multiple readings internally at hardware speed\n"
            "and returns one clean result. Reduces noise without\n"
            "slowing down GPIB communication.")
        adv_layout.addRow(self.filter_enabled)
        self.filter_type = QComboBox()
        self.filter_type.addItems(["repeat", "moving"])
        self.filter_type.setToolTip(
            "• Repeat: Takes N readings, averages, returns one result. Best for stable signals.\n"
            "• Moving: Running average of last N readings. Better for trending/changing signals.")
        adv_layout.addRow("Filter Type:", self.filter_type)
        self.filter_count = QSpinBox(minimum=1, maximum=100, singleStep=5)
        self.filter_count.setToolTip("Number of readings to average (1-100).\nHigher = cleaner but slower. 10 is a good starting point.")
        adv_layout.addRow("Filter Count:", self.filter_count)

        self.res_offset_comp = QCheckBox("Offset Compensated Ohms (Resistance mode)")
        self.res_offset_comp.setToolTip(
            "Cancels thermoelectric EMF by automatically measuring with\n"
            "current ON and OFF, then subtracting. Halves measurement\n"
            "speed but improves accuracy for low-resistance DUTs.\n"
            "Only applies to Resistance mode.")
        adv_layout.addRow(self.res_offset_comp)

        main_layout.addLayout(adv_layout)

        main_layout.addStretch()
        tab.setLayout(main_layout)
        return tab

    def create_display_tab(self):
        tab = QWidget()
        layout = QFormLayout()
        self.enable_plot = QComboBox()
        self.enable_plot.addItems(["True", "False"])
        self.enable_plot.setToolTip("Enable or disable the real-time plot during measurement.\nDisabling can improve performance for high-speed sampling.")
        layout.addRow("Enable Real-time Plots:", self.enable_plot)

        self.plot_update_interval = QSpinBox(minimum=50, maximum=5000, singleStep=50, suffix=" ms")
        self.plot_update_interval.setToolTip("How often the plot redraws (milliseconds).\nLower = smoother but uses more CPU. Default: 200 ms.")
        layout.addRow("Plot Update Interval:", self.plot_update_interval)

        self.plot_color_r = QComboBox(); self.plot_color_r.addItems(["red","blue","green","black","purple","orange","cyan","magenta"])
        self.plot_color_r.setToolTip("Line color for the Resistance mode plot.")
        layout.addRow("Resistance Plot Color:", self.plot_color_r)
        self.plot_color_v = QComboBox(); self.plot_color_v.addItems(["red","blue","green","black","purple","orange","cyan","magenta"])
        self.plot_color_v.setToolTip("Line color for the Voltage Source mode plot.")
        layout.addRow("V Source Plot Color:", self.plot_color_v)
        self.plot_color_i = QComboBox(); self.plot_color_i.addItems(["red","blue","green","black","purple","orange","cyan","magenta"])
        self.plot_color_i.setToolTip("Line color for the Current Source mode plot.")
        layout.addRow("I Source Plot Color:", self.plot_color_i)

        self.plot_width = QDoubleSpinBox(decimals=1, minimum=4, maximum=20, singleStep=0.5)
        self.plot_width.setToolTip("Plot width in inches (affects saved plot resolution).")
        self.plot_height = QDoubleSpinBox(decimals=1, minimum=3, maximum=15, singleStep=0.5)
        self.plot_height.setToolTip("Plot height in inches (affects saved plot resolution).")
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Width:")); size_layout.addWidget(self.plot_width)
        size_layout.addWidget(QLabel("Height:")); size_layout.addWidget(self.plot_height)
        layout.addRow("Plot Figure Size (inches):", size_layout)

        self.buffer_size = QSpinBox(minimum=0, maximum=1000000, singleStep=100)
        self.buffer_size.setSpecialValueText("Unlimited")
        self.buffer_size.setToolTip("Max data points to keep in memory for plotting.\n0 = unlimited (may use a lot of RAM for long runs).\n1000 is a good default for most measurements.")
        layout.addRow("Data Buffer Size (points):", self.buffer_size)
        tab.setLayout(layout)
        return tab

    def create_file_tab(self):
        tab = QWidget()
        layout = QFormLayout()
        self.auto_save_interval = QSpinBox(minimum=1, maximum=3600, singleStep=10, suffix=" s")
        self.auto_save_interval.setToolTip("How often to flush data to disk during measurement.\nLower values reduce data loss risk on crash but may\naffect performance. Default: 60 seconds.")
        layout.addRow("Auto-save Interval:", self.auto_save_interval)
        self.data_directory = QLineEdit()
        self.data_directory.setToolTip("Root directory where measurement data files are saved.\nA subdirectory is created for each user.")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self.browse_directory)
        hl = QHBoxLayout(); hl.addWidget(self.data_directory); hl.addWidget(browse)
        layout.addRow("Data Directory:", hl)
        tab.setLayout(layout)
        return tab

    def load_settings(self):
        m_cfg = self.settings['measurement']
        self.gpib_address.setText(m_cfg['gpib_address'])
        self.sampling_rate.setValue(m_cfg['sampling_rate'])
        self.nplc.setValue(m_cfg['nplc'])
        self.settling_time.setValue(m_cfg['settling_time'])
        self.res_test_current.setValue(m_cfg['res_test_current'])
        self.res_voltage_compliance.setValue(m_cfg['res_voltage_compliance'])
        self.res_measurement_type.setCurrentText(m_cfg['res_measurement_type'])
        self.vsource_voltage.setValue(m_cfg['vsource_voltage'])
        self.vsource_current_compliance.setValue(m_cfg['vsource_current_compliance'])
        self.vsource_duration_hours.setValue(m_cfg.get('vsource_duration_hours', 0.0))
        self.isource_current.setValue(m_cfg['isource_current'])
        self.isource_voltage_compliance.setValue(m_cfg['isource_voltage_compliance'])
        self.isource_duration_hours.setValue(m_cfg.get('isource_duration_hours', 0.0))
        self.stop_on_compliance.setChecked(bool(m_cfg.get('stop_on_compliance', False)))
        self.auto_zero.setCurrentText(str(m_cfg.get('auto_zero', 'on')))
        self.filter_enabled.setChecked(bool(m_cfg.get('filter_enabled', False)))
        self.filter_type.setCurrentText(str(m_cfg.get('filter_type', 'repeat')))
        self.filter_count.setValue(int(m_cfg.get('filter_count', 10)))
        self.res_offset_comp.setChecked(bool(m_cfg.get('res_offset_comp', False)))
        d_cfg = self.settings['display']
        self.enable_plot.setCurrentText("True" if d_cfg['enable_plot'] else "False")
        self.plot_update_interval.setValue(d_cfg['plot_update_interval'])
        self.plot_color_r.setCurrentText(d_cfg['plot_color_r'])
        self.plot_color_v.setCurrentText(d_cfg['plot_color_v'])
        self.plot_color_i.setCurrentText(d_cfg['plot_color_i'])
        self.plot_width.setValue(d_cfg['plot_figsize'][0])
        self.plot_height.setValue(d_cfg['plot_figsize'][1])
        self.buffer_size.setValue(0 if not d_cfg['buffer_size'] else d_cfg['buffer_size'])
        f_cfg = self.settings['file']
        self.auto_save_interval.setValue(f_cfg['auto_save_interval'])
        self.data_directory.setText(f_cfg['data_directory'])

    def save_settings(self):
        m_cfg = self.settings['measurement']
        m_cfg['gpib_address'] = self.gpib_address.text()
        m_cfg['sampling_rate'] = self.sampling_rate.value()
        m_cfg['nplc'] = self.nplc.value()
        m_cfg['settling_time'] = self.settling_time.value()
        m_cfg['res_test_current'] = self.res_test_current.value()
        m_cfg['res_voltage_compliance'] = self.res_voltage_compliance.value()
        m_cfg['res_measurement_type'] = self.res_measurement_type.currentText()
        m_cfg['vsource_voltage'] = self.vsource_voltage.value()
        m_cfg['vsource_current_compliance'] = self.vsource_current_compliance.value()
        m_cfg['vsource_duration_hours'] = self.vsource_duration_hours.value()
        m_cfg['isource_current'] = self.isource_current.value()
        m_cfg['isource_voltage_compliance'] = self.isource_voltage_compliance.value()
        m_cfg['isource_duration_hours'] = self.isource_duration_hours.value()
        m_cfg['stop_on_compliance'] = self.stop_on_compliance.isChecked()
        m_cfg['auto_zero'] = self.auto_zero.currentText()
        m_cfg['filter_enabled'] = self.filter_enabled.isChecked()
        m_cfg['filter_type'] = self.filter_type.currentText()
        m_cfg['filter_count'] = self.filter_count.value()
        m_cfg['res_offset_comp'] = self.res_offset_comp.isChecked()
        d_cfg = self.settings['display']
        d_cfg['enable_plot'] = (self.enable_plot.currentText() == "True")
        d_cfg['plot_update_interval'] = self.plot_update_interval.value()
        d_cfg['plot_color_r'] = self.plot_color_r.currentText()
        d_cfg['plot_color_v'] = self.plot_color_v.currentText()
        d_cfg['plot_color_i'] = self.plot_color_i.currentText()
        d_cfg['plot_figsize'] = [self.plot_width.value(), self.plot_height.value()]
        self.settings['display']['buffer_size'] = self.buffer_size.value() if self.buffer_size.value() > 0 else None
        f_cfg = self.settings['file']
        f_cfg['auto_save_interval'] = self.auto_save_interval.value()
        f_cfg['data_directory'] = self.data_directory.text()
        if self.username:
            self.config_manager.update_user_settings(self.username, self.settings)
        else:
            self.config_manager.update_global_settings(self.settings)
        QMessageBox.information(self, "Settings Saved", "Settings have been updated successfully.")
        self.accept()

    def browse_directory(self):
        current_dir = self.data_directory.text()
        if not os.path.isdir(current_dir):
            current_dir = os.path.expanduser("~")
        directory = QFileDialog.getExistingDirectory(self, "Select Data Directory", current_dir)
        if directory:
            self.data_directory.setText(directory)

    def detect_gpib_devices(self):
        try:
            self.setEnabled(False)
            rm = pyvisa.ResourceManager()
            resources = rm.list_resources()
        finally:
            self.setEnabled(True)
        if not resources:
            QMessageBox.information(self, "GPIB Detection", "No VISA instruments detected.")
            return
        # simple selection prompt
        from PyQt5.QtWidgets import QDialog, QVBoxLayout
        dialog = QDialog(self)
        dialog.setWindowTitle("Select GPIB Device")
        layout = QVBoxLayout(dialog)
        combo = QComboBox(dialog)
        combo.addItems(resources)
        if self.gpib_address.text() in resources:
            combo.setCurrentText(self.gpib_address.text())
        layout.addWidget(QLabel("Select the instrument address:"))
        layout.addWidget(combo)
        btn = QPushButton("Select", dialog)
        btn.clicked.connect(dialog.accept)
        layout.addWidget(btn)
        if dialog.exec_():
            self.gpib_address.setText(combo.currentText())


class UserSelectionDialog(QDialog):
    def __init__(self, config_manager: ConfigManager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.selected_user = None
        self.setWindowTitle("User Selection")
        self.init_ui()

    def init_ui(self):
        from PyQt5.QtWidgets import QVBoxLayout, QGroupBox, QHBoxLayout
        layout = QVBoxLayout()
        users = self.config_manager.get_users()
        last_user = self.config_manager.get_last_user()
        self.user_combo = QComboBox()
        if users:
            self.user_combo.addItems(users)
            if last_user and last_user in users:
                self.user_combo.setCurrentText(last_user)
        else:
            layout.addWidget(QLabel("No users found. Please create one."))

        new_user_group = QGroupBox("Create New User")
        new_user_layout = QHBoxLayout()
        self.new_user_input = QLineEdit()
        self.new_user_input.setPlaceholderText("Enter new username")
        self.create_user_button = QPushButton("Create && Select")
        self.create_user_button.clicked.connect(self.create_new_user)
        new_user_layout.addWidget(QLabel("Name:"))
        new_user_layout.addWidget(self.new_user_input)
        new_user_layout.addWidget(self.create_user_button)
        new_user_group.setLayout(new_user_layout)

        self.select_button = QPushButton("Select Existing User")
        self.select_button.clicked.connect(self.select_user)
        self.select_button.setEnabled(len(users) > 0)
        self.settings_button = QPushButton("Global Settings...")
        self.settings_button.clicked.connect(self.open_global_settings)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.settings_button)
        button_layout.addStretch()
        button_layout.addWidget(self.select_button)
        button_layout.addWidget(self.cancel_button)
        if users:
            layout.addWidget(QLabel("Select Existing User:"))
            layout.addWidget(self.user_combo)
        layout.addWidget(new_user_group)
        layout.addLayout(button_layout)
        self.setLayout(layout)
        self.new_user_input.setFocus()

    def create_new_user(self):
        username = self.new_user_input.text().strip()
        if not username:
            QMessageBox.warning(self, "Invalid Username", "Please enter a valid username.")
            return
        if username in self.config_manager.get_users():
            QMessageBox.warning(self, "User Exists", f"User '{username}' already exists. Please choose a different name or select the existing user.")
            return
        self.config_manager.add_user(username)
        self.selected_user = username
        self.config_manager.set_last_user(self.selected_user)
        QMessageBox.information(self, "User Created", f"User '{username}' created and selected.")
        self.accept()

    def select_user(self):
        if self.user_combo.count() == 0:
            QMessageBox.warning(self, "No Users", "No existing users to select.")
            return
        self.selected_user = self.user_combo.currentText()
        self.config_manager.set_last_user(self.selected_user)
        self.accept()

    def open_global_settings(self):
        dialog = SettingsDialog(self.config_manager, parent=self)
        dialog.exec_()
