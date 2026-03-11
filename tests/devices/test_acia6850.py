"""Tests for the ACIA6850 device."""

from alphasim.devices.acia6850 import ACIA6850


class TestACIA6850:
    def test_main_port_data_write_calls_tx_callback(self):
        acia = ACIA6850()
        calls: list[tuple[int, int]] = []
        acia.tx_callback = lambda port, value: calls.append((port, value))

        acia.write(0xFFFE22, 1, 0x41)

        assert calls == [(0, 0x41)]

    def test_hw_ser_alias_data_write_does_not_call_tx_callback(self):
        acia = ACIA6850()
        calls: list[tuple[int, int]] = []
        acia.tx_callback = lambda port, value: calls.append((port, value))

        acia.write(0xFFFFC9, 1, 0x41)

        assert calls == []
        assert acia.get_tx_output(0) == [0x41]
