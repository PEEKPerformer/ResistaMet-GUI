"""Generate publication-quality screenshots of each ResistaMet GUI tab.

Bypasses the measurement worker entirely by populating canvases and live
readouts directly. Runs headless via Qt's offscreen platform — no Keithley,
no display, fully reproducible.

Usage:
    python tools/generate_screenshots.py [--out docs/screenshots]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

# Offscreen must be set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from resistamet_gui.ui.main_window import ResistanceMeterApp  # noqa: E402


DEMO_USER = "demo"
DEMO_SAMPLE = "DEMO-RES-100R"


def _patched_select_user(self):
    """Replacement for ResistanceMeterApp.select_user that skips the dialog."""
    self.current_user = DEMO_USER
    self.user_label.setText(f"User: <b>{DEMO_USER}</b>")
    self.user_settings = self.config_manager.get_user_settings(DEMO_USER)
    self.update_ui_from_settings()
    for buf in self.data_buffers.values():
        buf.clear()
    self.clear_all_plots()
    self.set_all_controls_enabled(True)


def make_app() -> tuple[QApplication, ResistanceMeterApp]:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    ResistanceMeterApp.select_user = _patched_select_user

    win = ResistanceMeterApp()
    win.sample_input.setText(DEMO_SAMPLE)
    win.resize(1920, 1080)
    win.main_splitter.setSizes([900, 150])

    # Replace the noisy startup log with a clean, demo-friendly message.
    win.status_display.clear()
    win.log_status("Connected: Keithley MODEL 2420  (sourced via fake driver for demo)")
    win.log_status("User settings loaded.")

    return app, win


# ---------- per-tab fillers ----------

def fill_resistance(win):
    rng = np.random.default_rng(42)
    n = 300
    t = np.linspace(0, 60, n)
    base = 102.5  # Ω — looks like a typical thin-film cut
    drift = 0.0008 * t
    noise = rng.normal(0, 0.06, n)
    r = base + drift + noise

    buf = win.data_buffers["resistance"]
    for i in range(n):
        buf.add_resistance(float(t[i]), float(r[i]), "OK")

    timestamps, values, comp = buf.get_data_for_plot("resistance")
    stats = buf.get_statistics("resistance")
    win.tab_resistance.canvas.update_plot(
        timestamps, values, comp, stats,
        win.current_user, win.sample_input.text(),
    )

    i_test = float(win.tab_resistance.res_test_current.value())
    p = abs(i_test * i_test * r[-1])
    win.tab_resistance.live_readout.setText(
        f"{r[-1]:.3f} Ω   P: {p*1e6:.2f} µW"
    )
    win.tab_resistance.status_label.setText("Status: Running")


def fill_voltage_source(win):
    rng = np.random.default_rng(7)
    n = 300
    t = np.linspace(0, 60, n)
    v = 1.0
    i = (v / 102.5) + rng.normal(0, 1.5e-5, n)

    buf = win.data_buffers["source_v"]
    for k in range(n):
        buf.add_voltage_current(float(t[k]), v, float(i[k]), "OK")

    timestamps, values, comp = buf.get_data_for_plot("current")
    stats = buf.get_statistics("current")
    win.tab_voltage_source.canvas.update_plot(
        timestamps, values, comp, stats,
        win.current_user, win.sample_input.text(),
    )
    last_i = float(i[-1])
    win.tab_voltage_source.live_readout.setText(
        f"V: 1.000 V   I: {last_i*1000:.3f} mA   "
        f"R: {v/last_i:.2f} Ω   P: {abs(v*last_i)*1000:.2f} mW"
    )
    win.tab_voltage_source.status_label.setText("Status: Running")


def fill_current_source(win):
    rng = np.random.default_rng(11)
    n = 300
    t = np.linspace(0, 60, n)
    i = 1e-3
    v = (i * 102.5) + rng.normal(0, 1.5e-5, n)

    buf = win.data_buffers["source_i"]
    for k in range(n):
        buf.add_voltage_current(float(t[k]), float(v[k]), i, "OK")

    timestamps, values, comp = buf.get_data_for_plot("voltage")
    stats = buf.get_statistics("voltage")
    win.tab_current_source.canvas.update_plot(
        timestamps, values, comp, stats,
        win.current_user, win.sample_input.text(),
    )
    last_v = float(v[-1])
    win.tab_current_source.live_readout.setText(
        f"V: {last_v*1000:.3f} mV   I: 1.000 mA   "
        f"R: {last_v/i:.2f} Ω   P: {abs(last_v*i)*1e6:.2f} µW"
    )
    win.tab_current_source.status_label.setText("Status: Running")


def fill_four_point(win):
    rng = np.random.default_rng(3)
    n_spots, n_per_spot = 8, 30
    rs_mean = 47.3  # Ω/□
    rs_per_spot = rs_mean + rng.normal(0, 0.6, n_spots)
    rs_values = []
    for s in range(n_spots):
        rs_values.extend(rs_per_spot[s] + rng.normal(0, 0.15, n_per_spot))

    w = win.tab_four_point
    # Use a realistic source current — default in the UI is now 100 µA.
    w.fpp_current.setValue(1e-4)
    w.fpp_histogram.update_histogram(rs_values, "Rs (Ω/□)")

    mean = float(np.mean(rs_values))
    std = float(np.std(rs_values, ddof=1))
    rsd = std / mean * 100
    w.fpp_n_label.setText(str(len(rs_values)))
    w.fpp_rs_label.setText(f"{mean:.3f} ± {std:.3f} ({rsd:.2f}%)")
    # Synthetic ρ and σ assuming t = 0.5 µm thin film, K = 4.532
    t_cm = 0.5e-4
    k = 4.532
    rho_mean = mean * t_cm / k * k  # equals mean*t_cm
    rho_std = std * t_cm
    sigma_mean = 1.0 / rho_mean if rho_mean > 0 else 0
    sigma_std = sigma_mean * (rho_std / rho_mean) if rho_mean > 0 else 0
    w.fpp_rho_label.setText(
        f"{rho_mean:.3e} ± {rho_std:.2e} ({rsd:.2f}%)"
    )
    w.fpp_sigma_label.setText(
        f"{sigma_mean:.3e} ± {sigma_std:.2e} ({rsd:.2f}%)"
    )
    w.live_readout.setText(
        f"Rs: {mean:.2f} Ω/□   ρ: {rho_mean:.2e} Ω·cm   "
        f"σ: {sigma_mean:.0f} S/cm   V/I: {mean/k:.3f} Ω"
    )
    w.status_label.setText("Status: Running")


def fill_sweep(win):
    # Linear DUT: 100 Ω resistor, sweep ±1 V in 50 mV steps.
    v = np.linspace(-1.0, 1.0, 41)
    i = v / 100.0
    w = win.tab_sweep
    w.iv_canvas.clear_plot()
    w.iv_canvas.plot_sweep(v.tolist(), i.tolist(), label="Forward", color="blue")
    w.live_readout.setText("41 points acquired")
    w.status_label.setText("Status: Idle")
    if hasattr(w, "sweep_points_label"):
        w.sweep_points_label.setText("Points: 41")


# ---------- driver ----------

def grab(win, path: Path):
    pix = win.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(path), "PNG")
    print(f"  -> {path.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="docs/screenshots",
        help="Output directory relative to repo root (default: docs/screenshots)",
    )
    args = parser.parse_args()
    out_dir = ROOT / args.out

    # Run inside a temp dir so we don't mutate the user's config.json.
    with tempfile.TemporaryDirectory(prefix="resistamet-shots-") as tmp:
        os.chdir(tmp)
        app, win = make_app()
        win.show()
        app.processEvents()

        tabs = [
            ("01_resistance", "Resistance Measurement", fill_resistance),
            ("02_voltage_source", "Voltage Source", fill_voltage_source),
            ("03_current_source", "Current Source", fill_current_source),
            ("04_four_point_probe", "4-Point Probe", fill_four_point),
            ("05_iv_sweep", "I-V Sweep", fill_sweep),
        ]

        print(f"Generating screenshots in {out_dir.relative_to(ROOT)}/")
        for name, label, fill in tabs:
            idx = next(
                i for i in range(win.main_tabs.count())
                if win.main_tabs.tabText(i) == label
            )
            win.main_tabs.setCurrentIndex(idx)
            app.processEvents()
            fill(win)
            app.processEvents()
            grab(win, out_dir / f"{name}.png")

        print("Done.")


if __name__ == "__main__":
    main()
