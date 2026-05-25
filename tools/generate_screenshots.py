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
from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

from resistamet_gui.ui.main_window import ResistanceMeterApp  # noqa: E402
from resistamet_gui.ui.widgets import (  # noqa: E402
    format_readout_html,
    precision_for_nplc,
)
from resistamet_gui.accuracy import (  # noqa: E402
    current_source_uncertainty,
    current_uncertainty,
    resistance_uncertainty,
    voltage_source_uncertainty,
    voltage_uncertainty,
)


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

    # Mirror production resistance live-readout (main_window.py:2354):
    # same uncertainty source (resistance_uncertainty with enhanced flag
    # from the offset-comp checkbox) and same format_readout_html chain
    # so the screenshot is a faithful render of what the user sees.
    i_test = float(win.tab_resistance.res_test_current.value())
    r_last = float(r[-1])
    enhanced = bool(win.tab_resistance.res_offset_comp.isChecked())
    sigma_r = resistance_uncertainty(
        voltage=i_test * r_last,
        current=i_test,
        nplc=1.0,
        enhanced=enhanced,
    )
    fp = precision_for_nplc(1.0)
    parts = [format_readout_html(
        'R', r_last, sigma_r, 'Ω', fallback_precision=fp)]
    if i_test != 0:
        p = abs(i_test * i_test * r_last)
        parts.append(format_readout_html(
            'P', p, float('nan'), 'W', fallback_precision=fp))
    win.tab_resistance.live_readout.setText(
        win._READOUT_DIVIDER.join(parts))
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
    # Mirror production source_v live-readout (main_window.py:2374): V is
    # sourced (voltage_source_uncertainty), I is measured (current_uncertainty),
    # R is propagated (resistance_uncertainty), P has no propagated σ.
    last_i = float(i[-1])
    sigma_v = voltage_source_uncertainty(v)
    sigma_i = current_uncertainty(last_i, nplc=1.0)
    sigma_r = resistance_uncertainty(v, last_i, nplc=1.0)
    fp = precision_for_nplc(1.0)
    parts = [
        format_readout_html('V', v, sigma_v, 'V', fallback_precision=fp),
        format_readout_html('I', last_i, sigma_i, 'A', fallback_precision=fp),
        format_readout_html(
            'R', v / last_i, sigma_r, 'Ω', fallback_precision=fp),
        format_readout_html(
            'P', abs(v * last_i), float('nan'), 'W', fallback_precision=fp),
    ]
    win.tab_voltage_source.live_readout.setText(
        win._READOUT_DIVIDER.join(parts))
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
    # Mirror production source_i live-readout (main_window.py:2374): I is
    # sourced (current_source_uncertainty), V is measured (voltage_uncertainty),
    # R is propagated, P has no propagated σ.
    last_v = float(v[-1])
    sigma_v = voltage_uncertainty(last_v, nplc=1.0)
    sigma_i = current_source_uncertainty(i)
    sigma_r = resistance_uncertainty(last_v, i, nplc=1.0)
    fp = precision_for_nplc(1.0)
    parts = [
        format_readout_html('V', last_v, sigma_v, 'V', fallback_precision=fp),
        format_readout_html('I', i, sigma_i, 'A', fallback_precision=fp),
        format_readout_html(
            'R', last_v / i, sigma_r, 'Ω', fallback_precision=fp),
        format_readout_html(
            'P', abs(last_v * i), float('nan'), 'W', fallback_precision=fp),
    ]
    win.tab_current_source.live_readout.setText(
        win._READOUT_DIVIDER.join(parts))
    win.tab_current_source.status_label.setText("Status: Running")


def fill_four_point(win):
    """Synthesize a realistic 4PP workflow snapshot:
      - 8 spots × 30 readings (Wong/F84-aligned thin-film numbers)
      - 7 of them already saved → populate fpp_spots_table
      - 8th in progress → populate fpp_table (per-reading) + "Current
        Spot Stats" panel from that spot only
      - Histogram aggregates across all readings (matches what the user
        sees when reviewing a finished multi-spot survey)
    """
    rng = np.random.default_rng(3)
    n_spots_total, n_per_spot = 8, 30
    n_saved = n_spots_total - 1
    rs_mean_overall = 47.3  # Ω/□
    rs_per_spot = rs_mean_overall + rng.normal(0, 0.6, n_spots_total)
    spot_readings = [
        rs_per_spot[s] + rng.normal(0, 0.15, n_per_spot)
        for s in range(n_spots_total)
    ]

    w = win.tab_four_point
    # Use a realistic source current — default in the UI is now 100 µA.
    w.fpp_current.setValue(1e-4)
    src_i = float(w.fpp_current.value())
    t_cm = 0.5e-4  # 0.5 µm thin film
    k = 4.532  # Smits geometric factor

    # Histogram aggregates across all readings.
    all_rs = np.concatenate(spot_readings).tolist()
    w.fpp_histogram.update_histogram(all_rs, "Rs (Ω/□)")

    # Saved-spots summary table — first 7 spots already locked in.
    # Mirrors the row format produced by _save_fpp_spot at
    # main_window.py:3159.
    for s_idx in range(n_saved):
        readings = spot_readings[s_idx]
        m = float(np.mean(readings))
        s_std = float(np.std(readings, ddof=1))
        rsd = (s_std / m * 100) if m != 0 else 0.0
        row_idx = w.fpp_spots_table.rowCount()
        w.fpp_spots_table.insertRow(row_idx)
        for col, val in enumerate([
            f"Spot {s_idx + 1}", str(len(readings)),
            f"{m:.5g}", f"{s_std:.3g}", f"{rsd:.2f}",
        ]):
            w.fpp_spots_table.setItem(row_idx, col, QTableWidgetItem(val))

    # The 8th (in-progress) spot drives the Current Spot Stats panel
    # and the per-reading fpp_table. This is the live state the user
    # sees just before clicking "Save Spot".
    current_readings = spot_readings[-1]
    mean = float(np.mean(current_readings))
    std = float(np.std(current_readings, ddof=1))
    rsd = (std / mean * 100) if mean != 0 else 0.0
    rho_mean = mean * t_cm
    rho_std = std * t_cm
    sigma_mean = (1.0 / rho_mean) if rho_mean > 0 else 0.0
    sigma_std = sigma_mean * (rho_std / rho_mean) if rho_mean > 0 else 0.0

    w.fpp_n_label.setText(str(len(current_readings)))
    w.fpp_rs_label.setText(f"{mean:.3f} ± {std:.3f} ({rsd:.2f}%)")
    w.fpp_rho_label.setText(f"{rho_mean:.3e} ± {rho_std:.2e} ({rsd:.2f}%)")
    w.fpp_sigma_label.setText(
        f"{sigma_mean:.3e} ± {sigma_std:.2e} ({rsd:.2f}%)"
    )

    # Per-reading table for the in-progress spot at 10 Hz cadence.
    # Mirrors the row format produced by _append_four_point_row at
    # main_window.py:2485 (9 cols: Time, V, I, V/I, Rs, ρ, σ, Comp, Event).
    dt = 0.1
    for k_idx, rs in enumerate(current_readings):
        v_i = rs / k                       # V/I = Rs / k by F84 inversion
        v_meas = src_i * v_i               # measured voltage at the probes
        rho = rs * t_cm
        sigma = (1.0 / rho) if rho > 0 else 0.0
        row = [k_idx * dt, v_meas, src_i, v_i, rs, rho, sigma, 'OK', '']
        row_idx = w.fpp_table.rowCount()
        w.fpp_table.insertRow(row_idx)
        for col, val in enumerate(row):
            text = f"{val:.6g}" if isinstance(val, float) else str(val)
            w.fpp_table.setItem(row_idx, col, QTableWidgetItem(text))
    w.fpp_table.scrollToBottom()
    # Mirror production 4PP per-reading live-readout (main_window.py:2460).
    # The real GUI shows V/I/R/P for the *last reading* — the per-spot
    # derived Rs/ρ/σ already live in the "Current Spot Stats" panel on
    # the right side of this same screenshot. Synthesize a single reading
    # from the spot mean: source current I, measured V = I·Rs/k.
    src_i = float(w.fpp_current.value())
    v_per_reading = src_i * mean / k  # invert Rs = k · V/I
    sigma_v = voltage_uncertainty(v_per_reading, nplc=1.0)
    sigma_i = current_uncertainty(src_i, nplc=1.0)
    parts = [
        format_readout_html('V', v_per_reading, sigma_v, 'V'),
        format_readout_html('I', src_i, sigma_i, 'A'),
        format_readout_html(
            'R', v_per_reading / src_i, float('nan'), 'Ω'),
        format_readout_html(
            'P', abs(v_per_reading * src_i), float('nan'), 'W'),
    ]
    w.live_readout.setText(win._READOUT_DIVIDER.join(parts))
    w.live_readout.setStyleSheet(
        "color: #222; background: #f0f0f0; border: 1px solid #ccc; "
        "border-radius: 4px; padding: 4px;"
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

def _pretty(path: Path) -> str:
    """Render path relative to repo root when possible; fall back to absolute."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def grab(win, path: Path):
    pix = win.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(path), "PNG")
    print(f"  -> {_pretty(path)}")


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

        print(f"Generating screenshots in {_pretty(out_dir)}/")
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
