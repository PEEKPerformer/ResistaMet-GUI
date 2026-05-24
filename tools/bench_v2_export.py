"""Bench preflight for the v2.0 export pipeline.

Drives a real MeasurementWorker against a connected Keithley 2400-series
sourcemeter with a known DUT (100 Ω resistor recommended). Verifies that:

1. The default ``csv`` backend writes a .csv with a ``#`` metadata header,
   a column row, real data rows, and an end-metadata block.
2. ``compression='always'`` produces a .csv.gz, fires the status callback,
   and the gzipped file round-trips through ``parse_metadata``.
3. ``csv+legacy_json`` preserves the pre-2.0 dual-emit behavior.

Usage (on the Windows bench box):
    python tools/bench_v2_export.py --gpib GPIB0::24::INSTR --outdir bench_out
"""

import argparse
import gzip
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from resistamet_gui.workers import MeasurementWorker
from resistamet_gui.data_export import parse_metadata


def _settings(outdir: Path, gpib: str, output_format: str, compression: str = "never"):
    return {
        "measurement": {
            "sampling_rate": 5.0,
            "nplc": 1.0,
            "settling_time": 0.0,
            "gpib_address": gpib,
            "stop_on_compliance": False,
            "auto_zero": "on",
            "filter_enabled": False,
            "filter_type": "repeat",
            "filter_count": 10,
            "res_test_current": 1e-3,
            "res_voltage_compliance": 5.0,
            "res_measurement_type": "4-wire",
            "res_auto_range": True,
            "res_offset_comp": False,
            "res_cable_null": 0.0,
        },
        "display": {"enable_plot": False, "plot_update_interval": 100, "buffer_size": 100},
        "file": {"auto_save_interval": 60, "data_directory": str(outdir)},
        "output": {
            "format": output_format,
            "compression": compression,
            "compression_threshold_mb": 5,
        },
    }


def _drive(app, worker, n_points: int, timeout_s: float = 15.0):
    status = []
    points = []
    errors = []
    worker.status_update.connect(status.append)
    worker.error_occurred.connect(errors.append)
    worker.data_point.connect(lambda ts, d, c, e: points.append(d))

    def maybe_stop(*_):
        if len(points) >= n_points:
            worker.stop_measurement()
    worker.data_point.connect(maybe_stop)

    worker.start()
    deadline = time.time() + timeout_s
    while worker.isRunning() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.wait(3000)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.01)
    return status, points, errors


def run_csv_default(app, outdir: Path, gpib: str) -> bool:
    print("\n=== [1/3] csv default, no compression ===")
    settings = _settings(outdir, gpib, output_format="csv", compression="never")
    worker = MeasurementWorker("resistance", "bench_csv_default", "preflight", settings)
    status, points, errors = _drive(app, worker, n_points=3)

    if errors:
        print("FAIL: errors:", errors)
        return False
    r = points[-1].get('resistance') if points else None
    print(f"  collected {len(points)} points; last resistance = {r}")

    csvs = list(outdir.rglob("*bench_csv_default*.csv"))
    jsons = list(outdir.rglob("*bench_csv_default*.json"))
    if not csvs or jsons:
        print(f"FAIL: csv={csvs}, json={jsons}")
        return False

    text = csvs[0].read_text(encoding="utf-8")
    must_have = [
        "# resistamet_format_version: 2.0",
        "# user: preflight",
        "# mode: resistance",
        "# params.test_current_A: 0.001",
        "# units: s,",
        "# --- run completed ---",
        "# total_samples:",
    ]
    for needle in must_have:
        if needle not in text:
            print(f"FAIL: missing '{needle}' in {csvs[0]}")
            return False
    meta = parse_metadata(csvs[0])
    print(f"  parse_metadata: mode={meta.get('mode')} sample={meta.get('sample')} "
          f"total_samples={meta.get('total_samples')}")
    print(f"  PASS: {csvs[0].name}")
    return True


def run_csv_gzip(app, outdir: Path, gpib: str) -> bool:
    print("\n=== [2/3] csv with compression=always ===")
    settings = _settings(outdir, gpib, output_format="csv", compression="always")
    worker = MeasurementWorker("resistance", "bench_csv_gzip", "preflight", settings)
    status, points, errors = _drive(app, worker, n_points=3)

    if errors:
        print("FAIL: errors:", errors)
        return False

    gzs = list(outdir.rglob("*bench_csv_gzip*.csv.gz"))
    raw = list(outdir.rglob("*bench_csv_gzip*.csv"))
    if not gzs:
        print(f"FAIL: no .csv.gz produced (raw={raw})")
        return False
    if any(p.suffix == ".csv" for p in raw):
        print(f"FAIL: original .csv was not removed: {raw}")
        return False

    compress_status = [s for s in status if "Compressed" in s]
    if not compress_status:
        print(f"FAIL: no 'Compressed' status message fired. Status tail: {status[-5:]}")
        return False
    print(f"  status fired: {compress_status[0]}")

    with gzip.open(gzs[0], "rt", encoding="utf-8") as f:
        head = f.read(2048)
    if "# resistamet_format_version: 2.0" not in head:
        print("FAIL: gz header missing format version")
        return False

    meta = parse_metadata(gzs[0])
    if meta.get("mode") != "resistance":
        print(f"FAIL: parse_metadata on .gz returned {meta}")
        return False
    print(f"  parse_metadata on .gz: mode={meta.get('mode')} samples={meta.get('total_samples')}")
    print(f"  PASS: {gzs[0].name}")
    return True


def run_legacy_dual(app, outdir: Path, gpib: str) -> bool:
    print("\n=== [3/3] csv+legacy_json back-compat ===")
    settings = _settings(outdir, gpib, output_format="csv+legacy_json")
    worker = MeasurementWorker("resistance", "bench_legacy", "preflight", settings)
    status, points, errors = _drive(app, worker, n_points=3)

    if errors:
        print("FAIL: errors:", errors)
        return False

    csvs = list(outdir.rglob("*bench_legacy*.csv"))
    jsons = list(outdir.rglob("*bench_legacy*.json"))
    if not csvs or not jsons:
        print(f"FAIL: csv={csvs}, json={jsons}")
        return False

    import json as _json
    payload = _json.loads(jsons[0].read_text(encoding="utf-8"))
    if payload.get("format_version") != "1.0":
        print(f"FAIL: legacy JSON format_version != 1.0: {payload.get('format_version')}")
        return False
    if "data" not in payload or not payload["data"]:
        print(f"FAIL: legacy JSON missing data array")
        return False
    print(f"  legacy JSON: {len(payload['data'])} rows, schema {list(payload.keys())}")
    print(f"  PASS: {csvs[0].name} + {jsons[0].name}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpib", default="GPIB0::24::INSTR")
    parser.add_argument("--outdir", default="bench_out")
    args = parser.parse_args()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    app = QCoreApplication(sys.argv)

    results = []
    results.append(("csv default", run_csv_default(app, outdir, args.gpib)))
    results.append(("csv gzip", run_csv_gzip(app, outdir, args.gpib)))
    results.append(("legacy dual", run_legacy_dual(app, outdir, args.gpib)))

    print("\n=== summary ===")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()
