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

# Skip if PySide6 missing
pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

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
        # Existing worker-integration tests were written against the v1
        # dual JSON+CSV emit. Pin them to that backend so they continue to
        # verify the legacy path verbatim; new exporters are exercised by
        # test_data_export_v2.py.
        "output": {
            "format": "csv+legacy_json",
            "compression": "never",
            "compression_threshold_mb": 5,
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


def _four_point_settings(tmp_path, samples=3, delta_mode=False,
                           power_warn_w=1.0e-2, power_stop_w=1.0e-1,
                           stop_on_overpower=True):
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
        "fpp_power_warn_w": power_warn_w,
        "fpp_power_stop_w": power_stop_w,
        "fpp_stop_on_overpower": stop_on_overpower,
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


class TestFourPointPowerSafety:
    """Pre-flight and runtime safety checks for the 4PP probe power envelope."""

    def test_preflight_blocks_run_above_hard_stop(self, qapp, fake_rm, tmp_path):
        """Worst-case I × V_comp = 1mA × 100V = 100mW; hard stop at 50mW
        should refuse to start the run."""
        settings = _four_point_settings(
            tmp_path, samples=3,
            power_warn_w=10e-3, power_stop_w=50e-3, stop_on_overpower=True,
        )
        settings["measurement"]["fpp_current"] = 1e-3
        settings["measurement"]["fpp_voltage_compliance"] = 100.0  # I*V = 100mW > 50mW
        worker = MeasurementWorker("four_point", "preflight", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=4.0)

        # Should error out without ever taking a sample
        assert spies.data_point == []
        assert any("hard stop" in e.lower() for e in spies.error_occurred), (
            f"expected hard-stop error, got: {spies.error_occurred}"
        )
        assert any("100" in e and "mW" in e for e in spies.error_occurred)

    def test_preflight_warns_above_warn_threshold(self, qapp, fake_rm, tmp_path):
        """Worst-case 1mA × 30V = 30mW; warn at 10mW, stop at 100mW.
        Should warn but proceed to sample."""
        settings = _four_point_settings(
            tmp_path, samples=2,
            power_warn_w=10e-3, power_stop_w=100e-3, stop_on_overpower=True,
        )
        settings["measurement"]["fpp_current"] = 1e-3
        settings["measurement"]["fpp_voltage_compliance"] = 30.0
        worker = MeasurementWorker("four_point", "warn", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=8.0)

        assert spies.error_occurred == []
        assert len(spies.data_point) >= 1
        assert any("4PP power envelope" in s for s in spies.status_update)

    # Note on the missing runtime-abort test:
    # In source-I mode (which 4PP always uses), measured power is always
    # ≤ I_setpoint × V_compliance — the same quantity the pre-flight check
    # uses. So the runtime "abort" path can't trigger without pre-flight
    # also blocking. The runtime *condition* (the same `if measured > stop`
    # check that triggers the abort) is exercised by
    # ``test_runtime_overpower_warns_only_when_stop_disabled`` below; the
    # only difference between warn and abort is the boolean flag and which
    # signal fires. The runtime check is genuinely useful as defense in
    # depth against DUTs that exhibit dynamic resistance changes a real
    # instrument would surface but our fake doesn't model (e.g., a sample
    # whose R drops as it heats up under bias).

    def test_runtime_overpower_warns_only_when_stop_disabled(self, qapp, fake_rm, tmp_path):
        """With stop_on_overpower=False, warn but keep running. Exercises the
        same runtime-power check that drives the abort path."""
        settings = _four_point_settings(
            tmp_path, samples=3,
            # Default fake: 1mA × 100Ω = 100µW measured. Set warn at 10µW so
            # every sample trips warn; stop at 1mW so neither pre-flight (1mA
            # × 5V = 5mW) nor measured (100µW) trip stop.
            power_warn_w=10e-6, power_stop_w=10e-3, stop_on_overpower=False,
        )
        worker = MeasurementWorker("four_point", "rt_warn", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=6.0)

        assert spies.error_occurred == []
        assert len(spies.data_point) == 3
        assert any("warn threshold" in s for s in spies.status_update), (
            f"expected warn message in status: {spies.status_update}"
        )

    def test_safe_run_does_not_warn_or_stop(self, qapp, fake_rm, tmp_path):
        """Defaults: 1mA × 5V comp = 5mW pre-flight, 100µW measured.
        Both below default warn (10mW) and stop (100mW)."""
        settings = _four_point_settings(tmp_path, samples=3)
        worker = MeasurementWorker("four_point", "safe", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=8.0)

        assert spies.error_occurred == []
        assert len(spies.data_point) == 3
        # No power-related warnings
        assert not any("envelope" in s or "above warn" in s
                       for s in spies.status_update)


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


class TestCsvDefaultOutput:
    """Verify the v2 default (output.format == 'csv') end-to-end through MeasurementWorker."""

    def test_csv_default_produces_csv_with_metadata_header(self, qapp, fake_rm, tmp_path):
        settings = _resistance_settings(tmp_path)
        # Drop the test-suite legacy pin so we exercise the production default.
        settings["output"] = {
            "format": "csv",
            "compression": "never",
            "compression_threshold_mb": 5,
        }
        worker = MeasurementWorker("resistance", "v2default", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=2)

        assert spies.error_occurred == []
        csv_files = list((tmp_path / "data").rglob("*.csv"))
        json_files = list((tmp_path / "data").rglob("*.json"))
        assert csv_files, "csv format should produce a .csv file"
        assert not json_files, "csv format should not produce a sidecar .json"

        text = csv_files[0].read_text(encoding="utf-8")
        assert "# resistamet_format_version: 2.0" in text
        assert "# user: alice" in text
        assert "# mode: resistance" in text
        # End-block must have been written at finalize.
        assert "# --- run completed ---" in text
        assert "# total_samples:" in text


# ============================================================================
# SCPI-contract tests
# ----------------------------------------------------------------------------
# These pin down the SCPI commands each mode MUST send to the instrument during
# setup. They are paranoid by design: silent inversions (RSEN OFF when ON is
# required, source/measure swapped, compliance never set, etc.) are exactly
# the kind of bug that produces plausible-looking-but-wrong data.
#
# The fake Keithley records every write() in command_log. We capture the
# sequence between start-of-setup and first :READ?, and assert membership of
# critical commands by exact-match — not by substring, since SCPI argument
# values change between runs.
# ============================================================================

def _setup_writes(fake) -> list[str]:
    """All write()s issued before the first :READ?, in order."""
    out = []
    for op, cmd in fake.command_log:
        if op == "query" and cmd.strip().endswith(":READ?"):
            break
        if op == "write":
            out.append(cmd)
    return out


class TestSCPIContract:
    """Each mode's setup SCPI sequence must match the documented contract.

    Why this class exists: in 2026-05 we discovered four_point mode shipped
    `:SYST:RSEN OFF` for over a year, silently converting every 4-point probe
    measurement into a 2-wire measurement across the outer (current-carrying)
    pair. The test infrastructure to catch this already existed — only the
    assertion was missing. Don't let it happen again.
    """

    def test_resistance_2wire_sends_rsen_off(self, qapp, fake_rm, tmp_path):
        settings = _resistance_settings(tmp_path)
        settings["measurement"]["res_measurement_type"] = "2-wire"
        worker = MeasurementWorker("resistance", "sample", "alice", settings)
        _drive_worker(qapp, worker, stop_after_n_points=1)
        cmds = _setup_writes(fake_rm.opened[0])
        assert ":SYST:RSEN OFF" in cmds
        assert ":SYST:RSEN ON" not in cmds

    def test_resistance_4wire_sends_rsen_on(self, qapp, fake_rm, tmp_path):
        settings = _resistance_settings(tmp_path)
        settings["measurement"]["res_measurement_type"] = "4-wire"
        worker = MeasurementWorker("resistance", "sample", "alice", settings)
        _drive_worker(qapp, worker, stop_after_n_points=1)
        cmds = _setup_writes(fake_rm.opened[0])
        assert ":SYST:RSEN ON" in cmds
        assert ":SYST:RSEN OFF" not in cmds

    def test_source_v_sends_rsen_off(self, qapp, fake_rm, tmp_path):
        # Source-V on a 2-wire DUT must not leave Sense floating.
        settings = _source_v_settings(tmp_path)
        worker = MeasurementWorker("source_v", "sample", "alice", settings)
        _drive_worker(qapp, worker, stop_after_n_points=1)
        cmds = _setup_writes(fake_rm.opened[0])
        assert ":SYST:RSEN OFF" in cmds

    def test_source_i_sends_rsen_off(self, qapp, fake_rm, tmp_path):
        # See project_keithley_scpi_bugs note: source_i had RSEN hard-coded
        # ON in v1.3 and earlier, causing floating sense leads on 2-wire DUTs.
        settings = _source_i_settings(tmp_path)
        worker = MeasurementWorker("source_i", "sample", "alice", settings)
        _drive_worker(qapp, worker, stop_after_n_points=1)
        cmds = _setup_writes(fake_rm.opened[0])
        assert ":SYST:RSEN OFF" in cmds

    def test_four_point_sends_rsen_on(self, qapp, fake_rm, tmp_path):
        """4PP REQUIRES 4-wire mode. The S-302 probe head wires outer tips to
        Force HI/LO and inner tips to Sense HI/LO; RSEN OFF would route V to
        the Force terminals and silently produce a 2-wire reading across the
        current-carrying pair. This is the regression that prompted the
        SCPIContract test class — see Signatone S-302 manual page 6.
        """
        settings = _four_point_settings(tmp_path, samples=1)
        worker = MeasurementWorker("four_point", "sample", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        cmds = _setup_writes(fake_rm.opened[0])
        assert ":SYST:RSEN ON" in cmds, (
            "4PP setup MUST send :SYST:RSEN ON to enable 4-wire sensing. "
            f"Setup writes were: {cmds}"
        )
        assert ":SYST:RSEN OFF" not in cmds, (
            "4PP must not turn remote sense off. "
            f"Setup writes were: {cmds}"
        )

    def test_four_point_sources_current_measures_voltage(self, qapp, fake_rm, tmp_path):
        # Inverting source/measure functions is another silent failure mode.
        settings = _four_point_settings(tmp_path, samples=1)
        worker = MeasurementWorker("four_point", "sample", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        cmds = _setup_writes(fake_rm.opened[0])
        assert ":SOUR:FUNC CURR" in cmds
        assert ":SENS:FUNC 'VOLT:DC'" in cmds
        # Must not source voltage in 4PP mode.
        assert ":SOUR:FUNC VOLT" not in cmds

    def test_four_point_sets_voltage_compliance(self, qapp, fake_rm, tmp_path):
        # Compliance must be programmed; missing :SENS:VOLT:PROT would leave
        # the previous run's compliance in effect.
        settings = _four_point_settings(tmp_path, samples=1)
        worker = MeasurementWorker("four_point", "sample", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        cmds = _setup_writes(fake_rm.opened[0])
        assert any(c.startswith(":SENS:VOLT:PROT") for c in cmds), (
            f"4PP setup must set voltage compliance. Writes: {cmds}"
        )

    def test_four_point_form_elem_includes_status(self, qapp, fake_rm, tmp_path):
        # Compliance detection (STAT bit 3) requires STAT in FORM:ELEM.
        # Dropping it would silently disable hardware compliance detection.
        settings = _four_point_settings(tmp_path, samples=1)
        worker = MeasurementWorker("four_point", "sample", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        cmds = _setup_writes(fake_rm.opened[0])
        form_elem = [c for c in cmds if c.startswith(":FORM:ELEM")]
        assert form_elem, f"no :FORM:ELEM write in 4PP setup: {cmds}"
        assert "STAT" in form_elem[-1], (
            f":FORM:ELEM in 4PP must include STAT: {form_elem[-1]}"
        )


class TestFourPointF84Path:
    """Exercise the F84 calculation branch end-to-end through the worker."""

    def test_legacy_path_when_defaults(self, qapp, fake_rm, tmp_path):
        # All F84-only inputs at defaults → legacy path. Should produce
        # the same numbers as before this refactor.
        settings = _four_point_settings(tmp_path, samples=2)
        worker = MeasurementWorker("four_point", "legacy", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)
        assert spies.error_occurred == []
        assert len(spies.data_point) == 2

    def test_f84_path_with_diameter(self, qapp, fake_rm, tmp_path):
        # Setting a finite diameter should trigger F84 path and produce a
        # smaller geometric factor (F2 < 4.5324 at finite D).
        settings = _four_point_settings(tmp_path, samples=2)
        settings["measurement"]["fpp_diameter_cm"] = 1.0  # D=1cm, S/D=0.1016
        settings["measurement"]["fpp_thickness_um"] = 100.0  # 100 um film
        worker = MeasurementWorker("four_point", "f84_d", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)
        assert spies.error_occurred == []
        assert len(spies.data_point) == 2

    def test_f84_path_with_geometry_square(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=2)
        settings["measurement"]["fpp_geometry"] = "square"
        settings["measurement"]["fpp_diameter_cm"] = 2.0
        settings["measurement"]["fpp_thickness_um"] = 100.0
        worker = MeasurementWorker("four_point", "f84_sq", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)
        assert spies.error_occurred == []
        assert len(spies.data_point) == 2

    def test_f84_path_with_temperature_correction(self, qapp, fake_rm, tmp_path):
        # T + dopant should activate F_T branch and produce finite numbers.
        settings = _four_point_settings(tmp_path, samples=2)
        settings["measurement"]["fpp_diameter_cm"] = 1.0
        settings["measurement"]["fpp_thickness_um"] = 100.0
        settings["measurement"]["fpp_temperature_c"] = 25.0
        settings["measurement"]["fpp_dopant_type"] = "n"
        worker = MeasurementWorker("four_point", "f84_t", "alice", settings)
        spies = _drive_worker(qapp, worker, timeout_s=10.0)
        assert spies.error_occurred == []
        assert len(spies.data_point) == 2


class TestFourPointDeltaPerPolarity:
    """Verify V+, V-, R_f, R_r columns appear in delta-mode output."""

    def test_csv_header_includes_per_polarity_when_delta(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=2, delta_mode=True)
        worker = MeasurementWorker("four_point", "delta", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        csv_files = list((tmp_path / "data").rglob("*.csv"))
        assert csv_files
        header = next(
            (l for l in csv_files[0].read_text().splitlines()
             if not l.startswith("#") and l.strip()),
            "",
        )
        assert "V_plus" in header
        assert "V_minus" in header
        assert "R_f" in header
        assert "R_r" in header

    def test_csv_header_excludes_per_polarity_when_no_delta(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=2, delta_mode=False)
        worker = MeasurementWorker("four_point", "nodelta", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        csv_files = list((tmp_path / "data").rglob("*.csv"))
        assert csv_files
        header = next(
            (l for l in csv_files[0].read_text().splitlines()
             if not l.startswith("#") and l.strip()),
            "",
        )
        # No per-polarity pollution in non-delta runs
        assert "V_plus" not in header
        assert "R_f" not in header

    def test_csv_row_width_matches_delta_header(self, qapp, fake_rm, tmp_path):
        settings = _four_point_settings(tmp_path, samples=2, delta_mode=True)
        worker = MeasurementWorker("four_point", "delta_row", "alice", settings)
        _drive_worker(qapp, worker, timeout_s=10.0)
        csv_files = list((tmp_path / "data").rglob("*.csv"))
        assert csv_files
        non_comment_lines = [
            l for l in csv_files[0].read_text().splitlines()
            if not l.startswith("#") and l.strip()
        ]
        # Header + at least one data row, all same column count
        assert len(non_comment_lines) >= 2
        header_cols = non_comment_lines[0].count(',') + 1
        for row in non_comment_lines[1:]:
            assert row.count(',') + 1 == header_cols, (
                f"row width mismatch: header={header_cols}, row={row!r}"
            )


# ============================================================================
# Van der Pauw worker (ASTM F76 Method A)
# ============================================================================

from resistamet_gui.workers import VdpMeasurementWorker


def _vdp_settings(tmp_path):
    s = _base_settings(tmp_path)
    s["measurement"].update({
        "vdp_current": 1.0e-3,
        "vdp_voltage_compliance": 5.0,
        "vdp_voltage_range_auto": True,
        "vdp_thickness_cm": 1.0e-4,
        "vdp_settling_s": 0.0,           # speed up the test
        "vdp_readings_per_polarity": 1,
    })
    return s


def _drive_vdp_worker(qapp, worker: VdpMeasurementWorker, timeout_s: float = 15.0):
    """Drive a vdP worker by auto-confirming each geometry_ready prompt.

    Real lab workflow: user reconnects leads then presses "Measure". Test:
    fire proceed() immediately on each geometry_ready signal.
    """
    spies = {
        "geometry_ready": [],
        "geometry_complete": [],
        "vdp_complete": [],
        "status_update": [],
        "error_occurred": [],
        "compliance_hit": [],
    }
    worker.geometry_ready.connect(
        lambda idx, g: (spies["geometry_ready"].append((idx, dict(g))),
                        worker.proceed())
    )
    worker.geometry_complete.connect(
        lambda idx, r: spies["geometry_complete"].append((idx, dict(r)))
    )
    worker.vdp_complete.connect(
        lambda r: spies["vdp_complete"].append(dict(r))
    )
    worker.status_update.connect(spies["status_update"].append)
    worker.error_occurred.connect(spies["error_occurred"].append)
    worker.compliance_hit.connect(spies["compliance_hit"].append)

    worker.start()
    deadline = time.time() + timeout_s
    while worker.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    worker.wait(3000)
    for _ in range(5):
        qapp.processEvents()
        time.sleep(0.01)
    return spies


class TestVdpWorkerEndToEnd:
    """vdP worker drives the fake through all 4 geometries and emits a result."""

    def test_completes_all_4_geometries(self, qapp, fake_rm, tmp_path):
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        spies = _drive_vdp_worker(qapp, worker)
        assert not spies["error_occurred"], spies["error_occurred"]
        assert len(spies["geometry_ready"]) == 4
        assert len(spies["geometry_complete"]) == 4
        assert len(spies["vdp_complete"]) == 1

    def test_result_has_required_fields(self, qapp, fake_rm, tmp_path):
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        spies = _drive_vdp_worker(qapp, worker)
        assert spies["vdp_complete"]
        result = spies["vdp_complete"][0]
        for field in (
            "rho_a", "rho_b", "rho_avg", "sheet_resistance",
            "q_a", "q_b", "f_a", "f_b",
            "homogeneous", "asymmetry_pct",
            "voltages", "current_a", "thickness_cm",
        ):
            assert field in result, f"missing {field}"
        # Uniform fake DUT -> homogeneous result.
        assert result["homogeneous"] is True
        assert result["asymmetry_pct"] < 10.0
        # 8 F76 labels captured.
        assert len(result["voltages"]) == 8

    def test_csv_and_json_files_written(self, qapp, fake_rm, tmp_path):
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        csv_files = list((tmp_path / "data").rglob("*_vdP_*.csv"))
        json_files = list((tmp_path / "data").rglob("*_vdP_*.json"))
        assert csv_files, "vdP run should produce a CSV"
        assert json_files, "vdP run should produce a JSON"
        # 4 data rows + header = 5 non-comment lines
        rows = [
            l for l in csv_files[0].read_text().splitlines()
            if not l.startswith("#") and l.strip()
        ]
        assert len(rows) == 5

    def test_json_contains_vdp_result_summary(self, qapp, fake_rm, tmp_path):
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        json_files = list((tmp_path / "data").rglob("*_vdP_*.json"))
        payload = json.loads(json_files[0].read_text())
        # The exporter stuffs the final summary under meta.end_metadata.
        # Exact path depends on DualExporter; assert at least that the
        # vdp_result keys are reachable somewhere.
        flat = json.dumps(payload)
        assert "sheet_resistance" in flat or "vdp_result" in flat


class TestVdpSCPIContract:
    """Pin the SCPI commands emitted at setup and at each geometry."""

    def test_setup_uses_rsen_on(self, qapp, fake_rm, tmp_path):
        # F76 sec. 7.3.2 / S-302 wiring: voltmeter must use sense terminals.
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        cmds = [c for op, c in fake_rm.opened[0].command_log if op == "write"]
        assert ":SYST:RSEN ON" in cmds
        assert ":SYST:RSEN OFF" not in cmds

    def test_setup_sources_current_measures_voltage(self, qapp, fake_rm, tmp_path):
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        cmds = [c for op, c in fake_rm.opened[0].command_log if op == "write"]
        assert ":SOUR:FUNC CURR" in cmds
        assert ":SENS:FUNC 'VOLT:DC'" in cmds

    def test_setup_form_elem_contains_volt_stat(self, qapp, fake_rm, tmp_path):
        # We need STAT to detect compliance via bit 3.
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        cmds = [c for op, c in fake_rm.opened[0].command_log if op == "write"]
        form_cmds = [c for c in cmds if c.startswith(":FORM:ELEM")]
        assert form_cmds, "no :FORM:ELEM write found"
        # All FORM:ELEM writes should include VOLT and STAT.
        for fc in form_cmds:
            assert "VOLT" in fc
            assert "STAT" in fc

    def test_each_geometry_writes_plus_then_minus_current(self, qapp, fake_rm, tmp_path):
        # F76 sec. 11.1: current reversal at each geometry.
        # SOUR:CURR pattern after setup should alternate +I / -I per geometry.
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        sour_curr_writes = [
            c for op, c in fake_rm.opened[0].command_log
            if op == "write" and c.startswith(":SOUR:CURR ") and not c.startswith(":SOUR:CURR:")
        ]
        # Setup writes once with +I, then per geometry: +I (after OUTP ON),
        # then -I, then back to +I -> at least 4 geometries x 3 = 12 writes
        # plus the initial setup. Just sanity-check that BOTH polarities
        # appear and that there are matched flips.
        polarities = []
        for c in sour_curr_writes:
            # Parse the trailing value
            try:
                val = float(c.split()[-1])
                polarities.append(1 if val > 0 else -1 if val < 0 else 0)
            except ValueError:
                continue
        assert 1 in polarities and -1 in polarities, (
            "vdP must source both +I and -I; got "
            f"{set(polarities)} from {len(sour_curr_writes)} writes"
        )
        # There should be at least 4 sign flips (one per geometry).
        flips = sum(1 for a, b in zip(polarities, polarities[1:]) if a != b)
        assert flips >= 4, f"expected >=4 polarity flips, got {flips}"

    def test_output_off_between_geometries(self, qapp, fake_rm, tmp_path):
        # Output should be disabled after each geometry so the user can
        # safely reconnect leads.
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        _drive_vdp_worker(qapp, worker)
        cmds = [c for op, c in fake_rm.opened[0].command_log if op == "write"]
        # Count :OUTP OFF after first :OUTP ON (i.e. during run, not just
        # final cleanup). We expect >= 4 OFFs for the 4 geometries.
        outp_off_count = sum(1 for c in cmds if c == ":OUTP OFF")
        outp_on_count = sum(1 for c in cmds if c == ":OUTP ON")
        assert outp_on_count >= 4, f"expected >=4 :OUTP ON, got {outp_on_count}"
        assert outp_off_count >= 4, f"expected >=4 :OUTP OFF, got {outp_off_count}"

    def test_voltage_compliance_set_to_configured_value(self, qapp, fake_rm, tmp_path):
        settings = _vdp_settings(tmp_path)
        settings["measurement"]["vdp_voltage_compliance"] = 3.5
        worker = VdpMeasurementWorker("sample", "alice", settings)
        _drive_vdp_worker(qapp, worker)
        cmds = [c for op, c in fake_rm.opened[0].command_log if op == "write"]
        assert ":SENS:VOLT:PROT 3.5" in cmds


class TestVdpStop:
    """User can abort a vdP run mid-sequence."""

    def test_stop_during_wait_terminates_cleanly(self, qapp, fake_rm, tmp_path):
        # Don't auto-proceed: let the worker block in the first geometry,
        # then call stop_measurement.
        worker = VdpMeasurementWorker("sample", "alice", _vdp_settings(tmp_path))
        ready = []
        worker.geometry_ready.connect(lambda i, g: ready.append(i))
        worker.start()
        # Wait until the worker has emitted the first ready signal.
        deadline = time.time() + 5.0
        while not ready and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert ready, "worker never emitted geometry_ready"
        worker.stop_measurement()
        deadline = time.time() + 3.0
        while worker.isRunning() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert not worker.isRunning()


class TestFourPointAutoselectCurrent:
    """Pre-run current finder: probe -> pick current, or block for a decision."""

    def _autoselect_settings(self, tmp_path, sigfigs=4, seed=1e-3):
        s = _four_point_settings(tmp_path, samples=2)
        s["measurement"]["fpp_current"] = seed
        s["measurement"]["fpp_autoselect_current"] = True
        s["measurement"]["fpp_autoselect_sigfigs"] = sigfigs
        return s

    def test_measurable_sample_gets_current_auto_selected(self, qapp, fake_rm, tmp_path):
        # Default fake DUT is 100 Ω. 4 figs (SNR 1e4) at a ~1 µV floor -> 10 mV
        # -> ~100 µA, within the 20 mA power ceiling (0.1 W / 5 V), no decision.
        settings = self._autoselect_settings(tmp_path, sigfigs=4, seed=1e-3)
        worker = MeasurementWorker("four_point", "s", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)

        assert spies.error_occurred == []
        assert len(spies.data_point) >= 1
        _, d, _, _ = spies.data_point[0]
        # V = I*R with the chosen ~100 µA into 100 Ω -> ~10 mV.
        assert d["current"] == pytest.approx(1e-4, rel=0.15)
        assert d["voltage"] == pytest.approx(0.01, rel=0.15)
        # The chosen current is written back into the settings (drives filename
        # + CSV metadata) and announced on the status stream.
        assert settings["measurement"]["fpp_current"] == pytest.approx(1e-4, rel=0.15)
        assert any("uto-select" in s or "Probe:" in s for s in spies.status_update)

    def test_too_conductive_sample_aborts_when_user_declines(self, qapp, fake_rm, tmp_path):
        from tests.fakes.fake_keithley import FakeKeithley

        def _open_low_r(*_a, **_k):
            return FakeKeithley(dut_resistance_ohms=1e-5)  # 10 µΩ: unmeasurable
        fake_rm.open_resource = _open_low_r

        settings = self._autoselect_settings(tmp_path)
        worker = MeasurementWorker("four_point", "s", "alice", settings)
        decisions = []
        worker.autoselect_decision.connect(
            lambda info: (decisions.append(dict(info)), worker.autoselect_respond(False))
        )
        spies = _drive_worker(qapp, worker, timeout_s=8.0)

        assert len(decisions) == 1
        assert decisions[0]["verdict"] == "too_conductive"
        # Aborted before the acquisition loop: no points, no completion.
        assert spies.data_point == []
        assert spies.measurement_complete == []

    def test_too_conductive_sample_runs_when_user_accepts(self, qapp, fake_rm, tmp_path):
        from tests.fakes.fake_keithley import FakeKeithley

        def _open_low_r(*_a, **_k):
            return FakeKeithley(dut_resistance_ohms=1e-5)
        fake_rm.open_resource = _open_low_r

        settings = self._autoselect_settings(tmp_path)
        worker = MeasurementWorker("four_point", "s", "alice", settings)
        worker.autoselect_decision.connect(lambda info: worker.autoselect_respond(True))
        spies = _drive_worker(qapp, worker, stop_after_n_points=1, timeout_s=8.0)

        # Proceeded despite the low signal -> data flows.
        assert len(spies.data_point) >= 1

    def test_autoselect_off_leaves_current_unchanged(self, qapp, fake_rm, tmp_path):
        # With the toggle off, the seed current is used verbatim (no probe).
        settings = _four_point_settings(tmp_path, samples=2)
        settings["measurement"]["fpp_current"] = 1e-3
        settings["measurement"]["fpp_autoselect_current"] = False
        worker = MeasurementWorker("four_point", "s", "alice", settings)
        spies = _drive_worker(qapp, worker, stop_after_n_points=1)
        assert settings["measurement"]["fpp_current"] == 1e-3
        _, d, _, _ = spies.data_point[0]
        assert d["current"] == pytest.approx(1e-3, rel=0.1)

    def test_insulator_reports_conductivity_upper_bound(self, qapp, fake_rm, tmp_path):
        # A 1 TOhm sample stays in compliance even at the lowest current -> the
        # finder reports a conductivity UPPER bound rather than a bogus value.
        from tests.fakes.fake_keithley import FakeKeithley

        def _open_insulator(*_a, **_k):
            return FakeKeithley(dut_resistance_ohms=1e12)
        fake_rm.open_resource = _open_insulator

        settings = self._autoselect_settings(tmp_path)
        settings["measurement"]["fpp_thickness_um"] = 100.0  # enables sigma bound
        worker = MeasurementWorker("four_point", "s", "alice", settings)
        decisions = []
        worker.autoselect_decision.connect(
            lambda info: (decisions.append(dict(info)), worker.autoselect_respond(False))
        )
        spies = _drive_worker(qapp, worker, timeout_s=8.0)

        assert len(decisions) == 1
        assert decisions[0]["verdict"] == "too_resistive"
        assert "S/m" in decisions[0]["reason"]  # conductivity upper bound reported
        assert spies.data_point == []           # aborted before the loop

    def test_high_resistance_sample_measured_at_low_current(self, qapp, fake_rm, tmp_path):
        # 1 MOhm: the seed (1 mA) hits compliance, but re-probing at the lowest
        # range measures it and the run proceeds at a small current (no dialog).
        from tests.fakes.fake_keithley import FakeKeithley

        def _open_highr(*_a, **_k):
            return FakeKeithley(dut_resistance_ohms=1e6)
        fake_rm.open_resource = _open_highr

        settings = self._autoselect_settings(tmp_path, seed=1e-3)
        worker = MeasurementWorker("four_point", "s", "alice", settings)
        decisions = []
        worker.autoselect_decision.connect(lambda info: decisions.append(dict(info)))
        spies = _drive_worker(qapp, worker, stop_after_n_points=1, timeout_s=8.0)

        assert decisions == []                    # measurable -> no prompt
        assert len(spies.data_point) >= 1
        _, d, _, _ = spies.data_point[0]
        assert d["current"] == pytest.approx(1e-6, rel=0.5)          # dropped to ~1 uA
        assert d["voltage"] == pytest.approx(d["current"] * 1e6, rel=0.1)
