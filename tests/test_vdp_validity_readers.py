"""An invalid van der Pauw result reaches every reader as invalid.

``test_vdp_validity`` covers the calculation, the run and the file. These are
the readers after them: the ``vdp_result`` and ``vdp_geometry_complete``
events, the Qt result panel, and the run summary an agent is given. Before,
the event dropped ``valid`` and the reason, so a run with a reading in
compliance was shown as NON-HOMOGENEOUS with an asymmetry of 0 %.
"""
import math

import pytest

from resistamet_gui.constants import KEITHLEY_STAT_BIT_COMPLIANCE
from resistamet_gui.mcp import summary as mcp_summary
from resistamet_gui.mcp.summary import summarise
from resistamet_gui.session.events import (VdpGeometryCompletePayload,
                                           VdpResultPayload)

from .test_vdp_validity import (CLEAN, _footer, _no_sleep_inhibitor,  # noqa: F401
                                _run, _script_reads)

#: Sense leads swapped at geometries 2 and 4.
SWAPPED = [0.1, -0.1, -0.3, 0.3, 0.1, -0.1, -0.3, 0.3]


def _compliance_run(tmp_path, monkeypatch, **measurement):
    reads = list(CLEAN)
    reads[2] = (0.1, KEITHLEY_STAT_BIT_COMPLIANCE)        # geometry 2, +I
    _script_reads(monkeypatch, reads)
    return _run(tmp_path, **measurement)


def _result(sink):
    return sink.of_type('vdp_result')[0].payload


def _file_text(sink):
    with open(sink.of_type('run_ended')[0].payload['path'], encoding='utf-8') as handle:
        return handle.read()


# --------------------------------------------------------------------------
# The events
# --------------------------------------------------------------------------


class TestTheEventsCarryValidity:
    def test_a_clean_run(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, CLEAN)
        sink = _run(tmp_path)
        payload = _result(sink)
        assert payload['valid'] is True and payload['invalid_reason'] == ''
        assert payload['compliance'] is False and payload['compliance_readings'] == ''
        assert [e.payload['compliance'] for e in sink.of_type('vdp_geometry_complete')] == \
            [[], [], [], []]

    def test_a_reading_in_compliance(self, fake_rm, tmp_path, monkeypatch):
        sink = _compliance_run(tmp_path, monkeypatch)
        payload, footer = _result(sink), _footer(sink)
        assert payload['valid'] is False
        assert payload['invalid_reason'] == footer['vdp_result.invalid_reason']
        assert payload['compliance'] is True
        assert payload['compliance_readings'] == 'V_32,41'
        assert [e.payload['compliance'] for e in sink.of_type('vdp_geometry_complete')] == \
            [[], ['V_32,41'], [], []]

    def test_signs_that_are_not_one_samples(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, SWAPPED)
        payload = _result(_run(tmp_path))
        assert payload['valid'] is False and payload['compliance'] is False
        assert 'sign inconsistency' in payload['invalid_reason']

    def test_a_payload_from_before_the_fields_still_validates(self):
        old = dict(rho_a=1.0, rho_b=1.0, rho_avg=1.0, sheet_resistance=1.0, q_a=1.0,
                   q_b=1.0, f_a=1.0, f_b=1.0, homogeneous=True, asymmetry_pct=0.0,
                   current_a=1e-3, thickness_cm=0.05)
        result = VdpResultPayload(**old)
        assert (result.valid, result.invalid_reason, result.compliance,
                result.compliance_readings) == (True, '', False, '')
        geometry = VdpGeometryCompletePayload(
            index=0, name='G1', group='A', label_pos='V_21,34', v_pos=0.1,
            label_neg='V_12,34', v_neg=-0.1, current_a=1e-3)
        assert geometry.compliance == []


# --------------------------------------------------------------------------
# The run summary (MCP)
# --------------------------------------------------------------------------


class TestTheSummarySaysInvalid:
    def test_a_reading_in_compliance(self, fake_rm, tmp_path, monkeypatch):
        """Was headlined 'NOT homogeneous by ASTM F76 (asymmetry 0 % > 10 %)'."""
        sink = _compliance_run(tmp_path, monkeypatch)
        summary = summarise(_file_text(sink))
        result = summary['result']
        assert result['valid'] is False
        assert 'compliance at V_32,41' in result['invalid_reason']
        assert result['compliance_readings'] == ['V_32,41']
        assert result['headline'].startswith('INVALID result: ')
        assert result['invalid_reason'] in result['headline']
        assert 'homogeneous' not in result['headline']
        assert result['homogeneity']['homogeneous'] is None
        assert 'not assessed' in result['homogeneity']['note']
        # The numbers are still there, and the row is counted.
        assert result['R_s']['value'] > 0
        assert summary['compliance'] == {'rows': 1, 'kinds': {'V_COMP': 1}}

    def test_signs_that_are_not_one_samples(self, fake_rm, tmp_path, monkeypatch):
        """R_s is NaN in the file: that is an invalid result, not a missing one."""
        _script_reads(monkeypatch, SWAPPED)
        result = summarise(_file_text(_run(tmp_path)))['result']
        assert result['valid'] is False
        assert 'sign inconsistency' in result['invalid_reason']
        assert result['headline'].startswith('INVALID result: ')
        assert result['R_s']['value'] is None
        assert 'no result' not in result.get('note', '')

    def test_a_clean_run(self, fake_rm, tmp_path, monkeypatch):
        _script_reads(monkeypatch, CLEAN)
        result = summarise(_file_text(_run(tmp_path)))['result']
        assert result['valid'] is True and 'invalid_reason' not in result
        assert '; homogeneous by ASTM F76' in result['headline']
        assert result['homogeneity']['homogeneous'] is True

    def test_a_file_from_before_validity_was_written_is_valid(self):
        result = summarise("# mode: vdp\na\n1\n# --- run completed ---\n"
                           "# vdp_result.sheet_resistance: 1.0\n"
                           "# vdp_result.homogeneous: false\n"
                           "# vdp_result.asymmetry_pct: 12.0\n"
                           "# vdp_result.thickness_cm: 0.0\n")['result']
        assert result['valid'] is True
        assert 'NOT homogeneous' in result['headline']

    def test_the_uncertainty_text_is_the_model_the_calculation_uses(self):
        text = mcp_summary.UNCERTAINTY_VDP
        assert 'reciprocal pairs' in text
        assert '(1/4) sqrt((R_1 - R_3)^2 + (R_2 - R_4)^2)' in text
        assert 'standard error of the four' not in text
        assert 'source accuracy of I' in mcp_summary.UNCERTAINTY_FOUR_POINT


# --------------------------------------------------------------------------
# The Qt result panel
# --------------------------------------------------------------------------


def _valid_payload(**changes):
    volts = dict(zip(("V_21,34", "V_12,34", "V_32,41", "V_23,41",
                      "V_43,12", "V_34,12", "V_14,23", "V_41,23"), CLEAN))
    payload = dict(rho_a=1.0, rho_b=1.0, rho_avg=1.0, sheet_resistance=453.2, q_a=1.0,
                   q_b=1.0, f_a=1.0, f_b=1.0, homogeneous=True, asymmetry_pct=0.0,
                   voltages=volts, current_a=1e-3, thickness_cm=0.05)
    payload.update(changes)
    return payload


class TestTheResultPanel:
    def test_a_reading_in_compliance_is_shown_invalid_with_its_reason(
            self, fake_rm, main_window, tmp_path, monkeypatch):
        payload = _result(_compliance_run(tmp_path, monkeypatch))
        main_window._vdp_on_complete(dict(payload))
        banner = main_window.tab_vdp.vdp_homogeneity_banner
        assert banner.text() == f"INVALID  ({payload['invalid_reason']})"
        assert 'HOMOGENEOUS' not in banner.text()
        assert '#c62828' in banner.styleSheet()

    def test_signs_that_are_not_one_samples(self, fake_rm, main_window, tmp_path,
                                            monkeypatch):
        """R_s NaN and an infinite asymmetry: shown, not raised on."""
        _script_reads(monkeypatch, SWAPPED)
        payload = _result(_run(tmp_path))
        assert math.isnan(payload['sheet_resistance'])
        main_window._vdp_on_complete(dict(payload))
        banner = main_window.tab_vdp.vdp_homogeneity_banner.text()
        assert banner.startswith('INVALID  (sign inconsistency')
        # A thickness was given: the missing ρ is not for want of one.
        assert main_window.tab_vdp.vdp_rho_label.text() == "ρ: —"

    def test_a_valid_result_keeps_its_verdict(self, main_window):
        banner = main_window.tab_vdp.vdp_homogeneity_banner
        main_window._vdp_on_complete(_valid_payload(valid=True))
        assert banner.text().startswith('HOMOGENEOUS')
        main_window._vdp_on_complete(_valid_payload(homogeneous=False, asymmetry_pct=12.0))
        assert banner.text().startswith('NON-HOMOGENEOUS')

    def test_an_invalid_banner_does_not_outlive_its_result(self, main_window):
        banner = main_window.tab_vdp.vdp_homogeneity_banner
        main_window._vdp_on_complete(_valid_payload(
            valid=False, invalid_reason='x', homogeneous=False))
        assert banner.text() == 'INVALID  (x)'
        main_window._vdp_on_complete(_valid_payload())
        assert banner.text().startswith('HOMOGENEOUS')

    def test_the_readings_table_marks_a_reading_in_compliance(self, main_window):
        table = main_window.tab_vdp.vdp_readings_table
        readings = dict(name='G2', group='B', label_pos='V_32,41', v_pos=0.1,
                        label_neg='V_23,41', v_neg=-0.1, current_a=1e-3)
        main_window._vdp_on_geometry_complete(0, dict(readings, label_pos='V_21,34',
                                                      label_neg='V_12,34', compliance=[]))
        main_window._vdp_on_geometry_complete(1, dict(readings, compliance=['V_32,41']))
        marked, unmarked = table.item(2, 2), table.item(3, 2)
        assert marked.text().endswith('V_COMP') and marked.text().startswith('1.000000e-01')
        assert 'V_COMP' not in unmarked.text()
        assert 'V_COMP' not in table.item(0, 2).text()
        assert marked.background().color().name() != unmarked.background().color().name()
        # A payload without the key (an older producer) marks nothing.
        main_window._vdp_on_geometry_complete(2, dict(readings, label_pos='V_43,12',
                                                      label_neg='V_34,12'))
        assert 'V_COMP' not in table.item(4, 2).text()

    def test_the_uncertainty_shown_is_the_files_on_a_fixed_range(
            self, fake_rm, main_window, tmp_path, monkeypatch):
        """Auto range off: the panel used the auto-range specification."""
        from resistamet_gui.ui.widgets import format_engineering
        widget = main_window.tab_vdp
        widget.nplc.setValue(1.0)
        shown = {}
        for name, auto in (('auto', True), ('fixed', False)):
            _script_reads(monkeypatch, CLEAN)
            sink = _run(tmp_path / name, vdp_voltage_range_auto=auto,
                        vdp_voltage_compliance=20.0)
            # What the worker's instrument_identified signal does in the app.
            main_window._active_model_name = \
                sink.of_type('instrument_connected')[0].payload['model']
            widget.vdp_voltage_range_auto.setChecked(auto)
            widget.vdp_voltage_compliance.setValue(20.0)
            main_window._vdp_on_complete(dict(_result(sink)))
            u_file = _footer(sink)['vdp_result.sheet_resistance_uncertainty']
            shown[name] = format_engineering(u_file, 'Ω/sq', precision=2)
            assert widget.vdp_rs_label.text().endswith(f"± {shown[name]}")
        assert shown['auto'] != shown['fixed']
