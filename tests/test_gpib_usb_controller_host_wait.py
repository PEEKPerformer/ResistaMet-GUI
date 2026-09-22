"""Controller: the host waits of §7.2 and the expiry table of §7.3 they rest on."""
import pytest

from resistamet_gui.gpib_usb import protocol as p
from resistamet_gui.gpib_usb import tables as t
from resistamet_gui.gpib_usb.controller import RAW_READ_SLICE_S, Controller
from resistamet_gui.gpib_usb.protocol import GpibTimeout
from tests.fakes.gpib_usb import (SHORT_MS, STOP, T3S, WAIT_3S_MS, WAIT_10S_MS, WAIT_30S_MS, ScriptedTransport,
                                  address_listener, address_talker, attach_script, attached, attached_ni,
                                  raw_read_reply, raw_wait_ms, raw_wait_slices, raw_write_reply, read_reply,
                                  regread_reply, status_reply)


class TestHostWait:
    def test_five_second_request_uses_the_ten_second_row(self):
        controller, transport = attached(address_talker(code=0xFD) + [
            ('out', p.read_message(8, 0xFD)), ('in', read_reply(b'x', 8), 512),
        ])
        controller.read(22, max_bytes=8, timeout_s=5.0)
        # 5 s goes out as 0xfd, which one adapter runs for 16.78 s and the other for 20.0 s
        # (§7.3); the 15 s of nominal + 50 % would stop them 1.78 s and 5 s early, and the
        # 18.78 s of the first unit alone stopped the second 1.2 s early on the bench.
        assert transport.in_timeouts_after(0x0C) == [WAIT_10S_MS]
        assert transport.in_timeouts_after(0x06) == [SHORT_MS]
        assert transport.in_timeouts_after(0x0A) == [WAIT_10S_MS + 8]  # and 1 ms per byte asked for

    def test_three_second_request(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        assert transport.in_timeouts_after(0x0C) == [WAIT_3S_MS]
        assert transport.in_timeouts_after(0x0D) == [WAIT_3S_MS + 1]  # and 1 ms for the byte

    @pytest.mark.parametrize('timeout_s, code, base_ms', [(1.0, 0xFB, 3250), (3.0, T3S, WAIT_3S_MS)])
    def test_the_reply_to_a_framed_read_allows_for_the_bytes_it_carries(self, timeout_s, code, base_ms):
        # The code bounds a handshake, not the transfer (§10.1.8): a 2420 took 4.0 s over a
        # 20480-byte chunk and finished with error 0. pyvisa asks for 20480 bytes every time;
        # the framed path asks the adapter for 1024 of them per 0x0a, and the wait for each
        # piece allows for that piece's bytes at BUS_MIN_RATE_BPS on top of the expiry.
        controller, transport = attached([
            ('out', p.read_message(1024, code)),
            ('in', read_reply(b'x', 1024), p.read_reply_buffer_size(1024, 512)),
        ])
        controller.read_raw(20480, timeout_s=timeout_s)
        assert transport.in_timeouts_after(0x0A) == [base_ms + 1024]

    def test_the_reply_to_a_framed_write_allows_for_what_the_adapter_still_holds(self):
        # The OUT completes once the adapter has the message; up to its buffer (about 4 KB,
        # §8.17) has then still to reach the instrument before the reply can come.
        for length, allowance_ms in ((1, 1), (2048, 2048), (4096, 4096), (30000, 4096)):
            controller, transport = attached([
                ('out', p.write_message(bytes(length), T3S, True)), ('in', status_reply(0x0D)),
            ])
            controller.write_raw(bytes(length), timeout_s=3.0)
            assert transport.in_timeouts_after(0x0D) == [WAIT_3S_MS + allowance_ms], length

    def test_disabled_timeout_uses_the_application_wait(self):
        controller, transport = attached(address_listener(code=0xF0) + [
            ('out', p.write_message(b'A', 0xF0, True)), ('in', status_reply(0x0D)),
        ], infinite_wait_s=42.0)
        controller.write(22, b'A', timeout_s=None)
        assert transport.in_timeouts_after(0x0D) == [42001]

    def test_only_a_message_that_carries_write_data_waits_longer_on_the_out(self):
        controller, transport = attached(address_listener() + [
            ('out', p.write_message(b'A', T3S, True)), ('in', status_reply(0x0D)),
        ])
        controller.write(22, b'A', timeout_s=3.0)
        outs = [(opcode, tm) for kind, opcode, tm in transport.timeouts if kind == 'out']
        assert {tm for opcode, tm in outs if opcode != 0x0D} == {SHORT_MS}
        assert [tm for opcode, tm in outs if opcode == 0x0D] == [raw_wait_ms(1)]

    @pytest.mark.parametrize('timeout_s, base_ms', [(3.0, WAIT_3S_MS), (20.0, WAIT_30S_MS), (0.3, 2375)])
    def test_the_out_of_a_framed_write_follows_the_device_timeout_and_the_length(self, timeout_s, base_ms):
        # §7.2, §10.5.2: the tail of NI's 2080-byte 0x0d message took 103 ms on 0x02 with a
        # fast listener; the 1 s of the §7.2 table is too short for a slow one.
        data = bytes(2048)  # the longest framed write on a model with the alternate pair
        code, _ = p.effective_timeout(timeout_s)
        controller, transport = attached([
            ('out', p.write_message(data, code, True)), ('in', status_reply(0x0D)),
        ])
        controller.write_raw(data, timeout_s=timeout_s)
        assert transport.timeouts[-2:] == [('out', 0x0D, base_ms + 2048), ('in', 12, base_ms + 2048)]

    def test_a_framed_write_of_a_full_instruction_on_a_model_without_the_pair(self):
        data = bytes(0xFFFF)
        script = [
            ('out', p.register_read_message(t.USB_B_SERIAL_REGISTERS)),
            ('in', regread_reply([0x78, 0x56, 0x34, 0x12]), 32),
        ] + attach_script()[2:] + [('out', p.write_message(data, T3S, True)), ('in', status_reply(0x0D))]
        transport = ScriptedTransport(script)
        controller = Controller(transport, t.PID_USB_B, sleep=lambda s: None)
        controller.attach()
        controller.write_raw(data, timeout_s=3.0)
        assert transport.timeouts[-2] == ('out', 0x0D, raw_wait_ms(0xFFFF))  # 6.2 s + 65.5 s

    def test_the_raw_out_and_its_reply_follow_the_device_timeout_and_the_length(self):
        # §10.5.2: 2049 bytes took 368 ms to complete on 0x06.
        controller, transport = attached_ni([
            ('out', p.write_raw_message(2049, 0xFE, True)), ('raw_out', bytes(2049)),
            ('in', raw_write_reply(2049, 2049), 512),
        ])
        controller.write_raw(bytes(2049), timeout_s=20.0)
        assert transport.timeouts[-3:] == [('out', 0x0E, SHORT_MS), ('raw_out', 2049, WAIT_30S_MS + 2049),
                                           ('in', 512, WAIT_30S_MS + 2049)]

    def test_disabled_timeout_waits_the_application_wait_on_the_out_too(self):
        controller, transport = attached([
            ('out', p.write_message(bytes(1000), 0xF0, True)), ('in', status_reply(0x0D)),
        ], infinite_wait_s=42.0)
        controller.write_raw(bytes(1000), timeout_s=None)
        assert transport.timeouts[-2:] == [('out', 0x0D, 43000), ('in', 12, 43000)]

    #: §7.3 as the specification prints it, (code, measured expiry in seconds), every timed
    #: case on GPIB-USB-HS 013CC9DF under NI's driver: written out here so the test does not
    #: read the figures it checks from the code.
    MEASURED = [(0xF9, 0.132272), (0xFA, 0.263541), (0xFB, 1.049837),
                (0xFC, 4.195609), (0xFC, 4.195640), (0xFC, 4.195316), (0xFC, 4.196156),
                (0xFC, 4.195943), (0xFC, 4.195767),
                (0xFD, 16.778423), (0xFE, 33.555345), (0xFE, 33.555258)]
    #: §7.3, "A second unit expires at other times": GPIB-USB-HS 01CEE482 under this driver's
    #: messages (bench 2026-09-21), (code, expiry in seconds): 1.25 times 0.1, 0.3, 1, 3, 16
    #: and 33 s.
    BENCH = [(0xF9, 0.127), (0xFA, 0.375), (0xFB, 1.250), (0xFC, 3.750), (0xFD, 20.000), (0xFE, 41.250)]
    #: §7.3's inference column, the larger candidate: (code, power of two in microseconds).
    INFERRED = [(0xF1, 4), (0xF2, 5), (0xF3, 7), (0xF4, 9), (0xF5, 10), (0xF6, 12), (0xF7, 14),
                (0xF8, 15), (0xFF, 27), (0x01, 29), (0x02, 30)]
    #: The same column for the six timed codes, to back-test the rule for the untimed ones.
    INFERRED_FOR_TIMED = [(0xF9, 17), (0xFA, 19), (0xFB, 20), (0xFC, 22), (0xFD, 24), (0xFE, 25)]

    @pytest.mark.parametrize('code, measured_s', MEASURED + BENCH)
    def test_the_host_outlasts_every_measured_expiry_of_either_unit(self, code, measured_s):
        # §7.2: the reply trailed the power of two by at most 1.9 ms; the wait must clear the
        # measured expiry by more than that, and the recommendation is 2 s. §7.3: until the
        # cause of the two units' difference is established the wait outlasts both tables.
        wait = p.host_wait_s(code, 600.0)
        assert wait > measured_s + 1.9e-3
        assert wait >= measured_s + 2.0
        assert t.TIMEOUT_EXPIRY_JITTER_S == 1.9e-3

    def test_no_code_waits_less_than_it_did_from_the_first_unit_alone(self):
        # The wait before the second unit was timed: the first unit's figure, or the bare
        # power of two for a code nobody timed, plus 2 s. No code's wait may have got shorter.
        first_unit = dict(self.MEASURED)
        first_unit.update((code, 2 ** exponent / 1e6) for code, exponent in self.INFERRED)
        for _, code in t.TIMEOUT_TABLE:
            assert p.host_wait_s(code, 600.0) >= first_unit[code] + 2.0 - 1e-9, hex(code)

    @pytest.mark.parametrize('code, exponent', INFERRED)
    def test_an_untimed_code_waits_1_25_times_the_larger_of_nominal_and_the_power_of_two(self, code, exponent):
        nominal = dict((c, limit) for limit, c in t.TIMEOUT_TABLE)[code]
        expiry = 1.25 * max(nominal, 2 ** exponent / 1e6)
        assert t.timeout_expiry_s(code) == pytest.approx(expiry, rel=1e-12)
        assert p.host_wait_s(code, 600.0) == pytest.approx(expiry + 2.0, rel=1e-12)

    def test_the_untimed_code_rule_undershoots_neither_unit_on_any_timed_code(self):
        # Why that rule (§7.3): the second unit's round figure is the nominal limit up to 0xfc
        # and the first unit's power of two for 0xfd and 0xfe. The bare power of two, the rule
        # before, gives 16.78 s for 0xfd against a measured 20.0; 1.25 x nominal gives 12.5.
        nominal = dict((c, limit) for limit, c in t.TIMEOUT_TABLE)
        longest = {}
        for code, seconds in self.MEASURED + self.BENCH:
            longest[code] = max(longest.get(code, 0.0), seconds)
        for code, exponent in self.INFERRED_FOR_TIMED:
            power_of_two = 2 ** exponent / 1e6
            assert 1.25 * max(nominal[code], power_of_two) >= longest[code], hex(code)
        assert 2 ** 24 / 1e6 < longest[0xFD] and 1.25 * nominal[0xFD] < longest[0xFD]

    def test_every_row_of_the_timeout_table_has_an_expiry_the_host_outlasts(self):
        nominal = dict((code, limit) for limit, code in t.TIMEOUT_TABLE)
        assert set(nominal) == set(t.TIMEOUT_EXPIRY_MEASURED_S) | set(t.TIMEOUT_EXPIRY_INFERRED_S)
        assert not set(t.TIMEOUT_EXPIRY_MEASURED_S) & set(t.TIMEOUT_EXPIRY_INFERRED_S)
        assert set(t.TIMEOUT_EXPIRY_BENCH_S) == set(t.TIMEOUT_EXPIRY_MEASURED_S)
        assert t.TIMEOUT_EXPIRY_BENCH_S == dict(self.BENCH)
        for code, limit in nominal.items():
            expiry = t.timeout_expiry_s(code)
            assert p.host_wait_s(code, 600.0) > expiry + 1.9e-3
            assert expiry >= t.TIMEOUT_EXPIRY_MEASURED_S.get(code, 0.0), hex(code)
            assert expiry >= t.TIMEOUT_EXPIRY_BENCH_S.get(code, 0.0), hex(code)
            # The larger of the two units' figures is never below nominal (§7.3): 0xfa is the
            # one code the first unit ends early, 0.264 s for 300 ms, and the second runs 0.375.
            assert expiry >= limit, hex(code)
        assert t.TIMEOUT_EXPIRY_MEASURED_S[0xFA] < nominal[0xFA] < t.TIMEOUT_EXPIRY_BENCH_S[0xFA]

    def test_the_ten_second_code_is_waited_past_both_units_expiries(self):
        # The code the application's 5 s and 10 s timeouts go out as. Nominal + 50 % is 15 s;
        # the first unit runs it 16.78 s and the second 20.0 s.
        for asked in (5.0, 10.0):
            assert p.timeout_code(asked) == 0xFD
        assert p.host_wait_s(0xFD, 600.0) >= 22.0

    def test_the_disabled_code_has_no_expiry_and_an_unknown_code_is_refused(self):
        assert t.timeout_expiry_s(0xF0) is None
        assert p.host_wait_s(0xF0, 42.0) == 42.0
        with pytest.raises(ValueError):
            t.timeout_expiry_s(0x03)

    def test_the_raw_in_wait_is_never_shorter_than_the_reply_wait_of_a_framed_read(self):
        # §10.9: give the 0x88 read the same host wait as the reply rather than cancelling it
        # early; ours adds the transfer allowance on top, and spends it in slices.
        total_ms = WAIT_3S_MS + 20480
        controller, transport = attached_ni([
            ('out', p.read_raw_message(20480, T3S)),
        ] + raw_wait_slices(total_ms, 20992) + [
            STOP, ('raw_in', b'', 20992),
            ('in', raw_read_reply(20480, 0, end=False, error=1), 512),
        ])
        with pytest.raises(GpibTimeout):
            controller.read_raw(20480, timeout_s=3.0)
        transport.assert_done()
        slices = [tm for kind, _, tm in transport.timeouts if kind == 'raw_in'][:-1]
        assert sum(slices) == total_ms and total_ms > 4196 and max(slices) == int(RAW_READ_SLICE_S * 1000)
