"""End-to-end tests for MeasurementWorker against the FakeKeithley.

The worker is a QThread; we drive it with a Qt event loop running on the
``QT_QPA_PLATFORM=offscreen`` backend. Each test:
    1. Builds a settings dict for one measurement mode.
    2. Connects spies to the worker's signals.
    3. Starts the worker, processes Qt events until it finishes (or a
       data_point fires that we can use to stop it cleanly).
    4. Asserts on signals, file outputs, or fake state.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# Skip if PyQt5 missing
pytest.importorskip("PyQt5")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication

from resistamet_gui.workers import MeasurementWorker


# -------------------------------------------------------------- shared helpers

@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_sleep_inhibitor(monkeypatch):
    """Disable the real sleep inhibitor — we don't want pmset/inhibitd noise in tests."""
    from resistamet_gui import system_utils
    monkeypatch.setattr(system_utils.SleepInhibitor, "inhibit",
                         lambda self, reason="": True)
    monkeypatch.setattr(system_utils.SleepInhibitor, "uninhibit",
                         lambda self: True)


def _base_settings(tmp_path: Path) -> dict:
    return {
        "measurement": {
            "sampling_rate": 50.0,
            "nplc": 1.0,
            "settling_time": 0.0,
            "gpib_address": "GPIB0::24::INSTR",
            "stop_on_compliance": False,
            "auto_zero": "on",
            "filter_enabled": False,
            "filter_type": "repeat",
            "filter_count": 10,
        },
        "display": {
            "enable_plot": False,
            "plot_update_interval": 100,
            "buffer_size": 100,
        },
        "file": {
            "auto_save_interval": 60,
            "data_directory": str(tmp_path / "data"),
        },
    }


def _resistance_settings(tmp_path):
    s = _base_settings(tmp_path)
    s["measurement"].update({
        "res_test_current": 1e-3,
        "res_voltage_compliance": 5.0,
        "res_measurement_type": "4-wire",
        "res_auto_range": True,
        "res_offset_comp": False,
        "res_cable_null": 0.0,
    })
    return s


def _source_v_settings(tmp_path):
    s = _base_settings(tmp_path)
    s["measurement"].update({
        "vsource_voltage": 0.1,
        "vsource_current_compliance": 0.1,
        "vsource_current_range_auto": True,
        "vsource_duration_hours": 0.0,    # 0 = run until stopped
    })
    return s


def _source_i_settings(tmp_path):
    s = _base_settings(tmp_path)
    s["measurement"].update({
        "isource_current": 1e-3,
        "isource_voltage_compliance": 5.0,
        "isource_voltage_range_auto": True,
        "isource_duration_hours": 0.0,
    })
    return s


def _four_point_settings(tmp_path, samples=3, delta_mode=False):
    s = _base_settings(tmp_path)
    s["measurement"].update({
        "fpp_current": 1e-3,
        "fpp_voltage_compliance": 5.0,
        "fpp_voltage_range_auto": True,
        "fpp_spacing_cm": 0.1016,
        "fpp_thickness_um": 0.0,
        "fpp_alpha": 1.0,
        "fpp_k_factor": 4.532,
        "fpp_samples": samples,
        "fpp_model": "thin_film",
        "fpp_delta_mode": delta_mode,
        "fpp_delta_settling": 0.01,
    })
    return s


def _sweep_settings(tmp_path, direction="up"):
    s = _base_settings(tmp_path)
    s["measurement"].update({
        "sweep_source": "voltage",
        "sweep_start": 0.0,
        "sweep_stop": 0.5,
        "sweep_step": 0.125,
        "sweep_compliance": 0.1,
        "sweep_delay": 0.0,
        "sweep_direction": direction,
    })
    return s


class _Spies:
    """Collect every signal a worker emits."""

    def __init__(self, worker: MeasurementWorker):
        self.data_point = []
        self.status_update = []
        self.measurement_complete = []
        self.error_occurred = []
        self.compliance_hit = []
        self.sweep_complete = []
        worker.data_point.connect(
            lambda ts, d, c, e: self.data_point.append((ts, dict(d), c, e))
        )
        worker.status_update.connect(self.status_update.append)
        worker.measurement_complete.connect(self.measurement_complete.append)
        worker.error_occurred.connect(self.error_occurred.append)
        worker.compliance_hit.connect(self.compliance_hit.append)
        worker.sweep_complete.connect(
            lambda v, i, c: self.sweep_complete.append((list(v), list(i), list(c)))
        )


def _drive_worker(qapp, worker: MeasurementWorker, *,
                   stop_after_n_points: int | None = None,
                   timeout_s: float = 6.0) -> _Spies:
    """Start ``worker``, process Qt events, optionally stop after N data points.

    Returns the populated spy collector. Always drains the Qt event queue
    after ``worker.wait()`` so cross-thread signals emitted late in run()
    (``measurement_complete``, the final ``data_point``) are delivered.
    """
    spies = _Spies(worker)

    if stop_after_n_points is not None:
        def maybe_stop(*_args):
            if len(spies.data_point) >= stop_after_n_points:
                worker.stop_measurement()
        worker.data_point.connect(maybe_stop)

    worker.start()
    deadline = time.time() + timeout_s
    while worker.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    worker.wait(3000)
    # Final drain: deliver any signals queued just before run() exited
    for _ in range(5):
        qapp.processEvents()
        time.sleep(0.01)
    return spies


# ============================================================================
# Mode tests
# ============================================================================

class TestModelDetection:
    """The worker should announce the detected model (and its limits) at connect."""

    def test_detected_model_appears_in_status(self, qapp, fake_rm, tmp_path):
        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "model_test", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)

        assert spies.error_occurred == []
        # Default fake reports as a 2420 — status update should mention it
        assert any("Detected: Keithley 2420" in s for s in spies.status_update), (
            f"no model-detection line in status updates: {spies.status_update}"
        )
        # Should also include the source-V/I/W caps
        detected = [s for s in spies.status_update if "Detected:" in s]
        assert detected, "expected a Detected: line"
        assert "60V" in detected[0] and "3.05A" in detected[0], (
            f"expected 60V / 3.05A in detection line: {detected[0]}"
        )

    def test_unknown_model_warns_but_proceeds(self, qapp, fake_rm, tmp_path):
        # Reach into the FakeResourceManager and re-init opening with an
        # IDN that won't match any model in the spec table.
        from tests.fakes.fake_keithley import FakeKeithley
        original = fake_rm.open_resource

        def open_with_unknown(name, **kw):
            return FakeKeithley(idn="KEITHLEY INSTRUMENTS INC.,MODEL 9999,1,1")
        fake_rm.open_resource = open_with_unknown

        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "unknown_model", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)

        # Restore to keep the rest of the suite well-behaved
        fake_rm.open_resource = original

        assert any("model not in known table" in s.lower() for s in spies.status_update), (
            f"expected a 'not in known table' warning: {spies.status_update}"
        )


class TestResistanceMode:
    def test_resistance_runs_and_writes_data(self, qapp, fake_rm, tmp_path):
        settings = _resistance_settings(tmp_path)
        worker = MeasurementWorker("resistance", "sample1", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=2)

        assert spies.error_occurred == []
        assert len(spies.data_point) >= 2
        assert spies.measurement_complete == ["resistance"]

        # Each point carries a 'resistance' field
        ts, d, comp, evt = spies.data_point[0]
        assert "resistance" in d
        assert d["resistance"] == pytest.approx(100.0, rel=0.05)
        assert comp == "OK"

        # CSV+JSON files were written
        csv_files = list((tmp_path / "data").rglob("*.csv"))
        json_files = list((tmp_path / "data").rglob("*.json"))
        assert csv_files, "no CSV file produced"
        assert json_files, "no JSON file produced"

    def test_resistance_4wire_sets_rsen(self, qapp, fake_rm, tmp_path):
        settings = _resistance_settings(tmp_path)
        worker = MeasurementWorker("resistance", "sample1", "alice", settings)
        _drive_worker(qapp, worker, stop_after_n_points=1)
        # The worker opened one fake — inspect it
        assert len(fake_rm.opened) == 1
        # Worker has already closed the fake; check the command_log
        cmds = [c for op, c in fake_rm.opened[0].command_log if op == "write"]
        assert ":SYST:RSEN ON" in cmds

    def test_resistance_no_auto_ohms_error(self, qapp, fake_rm, tmp_path):
        """Regression: workers.py must sequence :SENS:RES:MODE MAN before sourcing."""
        settings = _resistance_settings(tmp_path)
        worker = MeasurementWorker("resistance", "sample1", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)
        # No 825 errors in the queue means the sequence was correct.
        # The worker should have produced data points without errors.
        assert spies.error_occurred == []
        assert any("Configuring instrument" in s for s in spies.status_update)


class TestSourceVMode:
    def test_source_v_runs_and_emits_voltage_current(self, qapp, fake_rm, tmp_path):
        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "vtest", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=2)

        assert spies.error_occurred == []
        assert len(spies.data_point) >= 2
        ts, d, comp, _ = spies.data_point[0]
        assert "voltage" in d and "current" in d
        assert d["voltage"] == pytest.approx(0.1, rel=0.01)
        assert d["current"] == pytest.approx(0.001, rel=0.05)


class TestSourceIMode:
    def test_source_i_runs_and_emits_voltage_current(self, qapp, fake_rm, tmp_path):
        settings = _source_i_settings(tmp_path)
        worker = MeasurementWorker("source_i", "itest", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=2)

        assert spies.error_occurred == []
        ts, d, comp, _ = spies.data_point[0]
        assert d["current"] == pytest.approx(0.001, rel=0.01)
        assert d["voltage"] == pytest.approx(0.1, rel=0.05)


class TestFourPointMode:
    def test_four_point_stops_after_target_samples(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=3)
        worker = MeasurementWorker("four_point", "wafer1", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)

        assert spies.error_occurred == []
        # Should auto-stop at exactly 3 samples
        assert len(spies.data_point) == 3
        assert spies.measurement_complete == ["four_point"]

    def test_four_point_delta_mode_alternates_polarity(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=2, delta_mode=True)
        worker = MeasurementWorker("four_point", "wafer1", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)

        assert spies.error_occurred == []
        fake = fake_rm.opened[0]
        sour_curr_writes = [c for op, c in fake.command_log
                             if op == "write"
                             and c.upper().startswith(":SOUR:CURR ")
                             and not c.upper().startswith(":SOUR:CURR:")]
        # Per delta sample: write +I, READ?, write -I, READ?, write +I (restore).
        # For 2 samples + initial config, expect both polarities.
        def _value(c: str) -> float:
            return float(c.split()[1])
        seen_pos = any(_value(c) > 0 for c in sour_curr_writes)
        seen_neg = any(_value(c) < 0 for c in sour_curr_writes)
        assert seen_pos, f"no +I write in delta mode: {sour_curr_writes}"
        assert seen_neg, f"no -I write in delta mode: {sour_curr_writes}"


class TestSweepMode:
    def test_sweep_up_emits_one_sweep_complete(self, qapp, fake_rm, tmp_path):
        settings = _sweep_settings(tmp_path, direction="up")
        worker = MeasurementWorker("sweep", "swp1", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)

        assert spies.error_occurred == []
        assert len(spies.sweep_complete) == 1
        v, i, comp = spies.sweep_complete[0]
        assert len(v) == 5
        assert len(i) == 5
        assert v[0] == pytest.approx(0.0, abs=1e-3)
        assert v[-1] == pytest.approx(0.5, rel=0.01)
        # Linear V vs I on a 100Ω DUT
        assert i[-1] == pytest.approx(5e-3, rel=0.05)

    def test_sweep_up_down_emits_two_sweep_completes(self, qapp, fake_rm, tmp_path):
        settings = _sweep_settings(tmp_path, direction="up_down")
        worker = MeasurementWorker("sweep", "swp1", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=15.0)

        assert spies.error_occurred == []
        # Forward + reverse
        assert len(spies.sweep_complete) == 2


# ============================================================================
# Compliance tests
# ============================================================================

class TestCompliance:
    def test_compliance_emits_compliance_hit(self, qapp, fake_rm, tmp_path):
        settings = _source_v_settings(tmp_path)
        # Force compliance: 0.5V into 100Ω with 1mA limit
        settings["measurement"]["vsource_voltage"] = 0.5
        settings["measurement"]["vsource_current_compliance"] = 1e-3
        worker = MeasurementWorker("source_v", "comp1", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=2)

        assert spies.error_occurred == []
        assert len(spies.compliance_hit) >= 1
        assert spies.compliance_hit[0] == "Current"
        # Data point should be marked I_COMP
        assert any(c == "I_COMP" for ts, d, c, e in spies.data_point)

    def test_stop_on_compliance_terminates_run(self, qapp, fake_rm, tmp_path):
        settings = _source_v_settings(tmp_path)
        settings["measurement"]["vsource_voltage"] = 0.5
        settings["measurement"]["vsource_current_compliance"] = 1e-3
        settings["measurement"]["stop_on_compliance"] = True
        worker = MeasurementWorker("source_v", "comp2", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=5.0)

        # Should stop after the first compliance-hit sample
        assert spies.error_occurred == []
        assert len(spies.data_point) <= 2
        assert spies.measurement_complete == ["source_v"]


# ============================================================================
# Retry / error handling
# ============================================================================

class TestRetryAndErrors:
    def test_transient_visa_error_recovers(self, qapp, fake_rm, tmp_path):
        """Failing 2 queries then recovering should NOT escalate to error_occurred."""
        import pyvisa

        # skip_first=2 lets *IDN? and :SYST:LFR? succeed during connect, then
        # fails the first 2 :READ? queries, then recovers.
        fake_rm.fail_next_open(
            n=2, skip_first=2,
            exception=pyvisa.errors.VisaIOError(-1073807339),
        )
        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "retry1", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=2, timeout_s=10.0)

        assert spies.error_occurred == [], (
            f"expected recovery, got errors: {spies.error_occurred}"
        )
        assert any("recovered" in s.lower() or "retry" in s.lower()
                    for s in spies.status_update), (
            f"expected retry/recovery message in status updates, got: {spies.status_update}"
        )

    def test_persistent_visa_error_escalates(self, qapp, fake_rm, tmp_path):
        """Failing >=5 queries in a row should emit error_occurred and stop."""
        import pyvisa

        fake_rm.fail_next_open(
            n=20, skip_first=2,
            exception=pyvisa.errors.VisaIOError(-1073807339),
        )
        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "retry2", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)

        assert any("retries" in e for e in spies.error_occurred), (
            f"expected escalation, got: {spies.error_occurred}"
        )


# ============================================================================
# Path safety
# ============================================================================

class TestPathSafety:
    def test_traversal_in_sample_name_is_sanitized(self, qapp, fake_rm, tmp_path):
        settings = _source_v_settings(tmp_path)
        # Sample name with path traversal attempt
        worker = MeasurementWorker("source_v", "../../etc/passwd",
                                     "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)

        assert spies.error_occurred == []
        # All output files MUST be inside tmp_path/data
        all_files = list((tmp_path / "data").rglob("*"))
        for f in all_files:
            resolved = f.resolve()
            assert str(resolved).startswith(str((tmp_path / "data").resolve())), (
                f"file escaped data dir: {resolved}"
            )

    def test_traversal_in_username_is_sanitized(self, qapp, fake_rm, tmp_path):
        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "sample", "../../../bob", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)

        assert spies.error_occurred == []
        all_files = list((tmp_path / "data").rglob("*"))
        for f in all_files:
            resolved = f.resolve()
            assert str(resolved).startswith(str((tmp_path / "data").resolve()))


# ============================================================================
# Output integrity
# ============================================================================

class TestOutputIntegrity:
    def test_json_output_has_metadata_and_rows(self, qapp, fake_rm, tmp_path):
        settings = _resistance_settings(tmp_path)
        worker = MeasurementWorker("resistance", "intg", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=3)

        assert spies.error_occurred == []
        json_files = list((tmp_path / "data").rglob("*.json"))
        assert json_files
        payload = json.loads(json_files[0].read_text())
        # DualExporter schema: meta + columns + data
        assert "meta" in payload, f"missing meta key: {list(payload.keys())}"
        assert "data" in payload, f"missing data key: {list(payload.keys())}"
        assert "columns" in payload
        assert payload["meta"]["instrument"].startswith("KEITHLEY")
        assert len(payload["data"]) >= 1

    def test_csv_row_count_matches_signals(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=4)
        worker = MeasurementWorker("four_point", "rows", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)

        assert spies.error_occurred == []
        csv_files = list((tmp_path / "data").rglob("*.csv"))
        assert csv_files
        lines = csv_files[0].read_text().strip().splitlines()
        # CSV has header line + N data lines
        data_lines = [l for l in lines if not l.startswith("#")]
        # Skip the header — data lines = N samples
        assert len(data_lines) >= len(spies.data_point), (
            f"CSV ({len(data_lines)} rows) lost data vs signals ({len(spies.data_point)})"
        )
