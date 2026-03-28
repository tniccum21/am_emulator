"""Tests for the ACIA6850 device."""

from alphasim.devices.acia6850 import ACIA6850


class TestACIA6850:
    def test_main_port_data_write_calls_tx_callback(self):
        acia = ACIA6850()
        calls: list[tuple[int, int]] = []
        acia.tx_callback = lambda port, value: calls.append((port, value))

        acia.write(0xFFFE22, 1, 0x41)

        assert calls == [(0, 0x41)]

    def test_unrecognized_address_ignored(self):
        """Writes to addresses outside the port map are silently ignored."""
        acia = ACIA6850()
        calls: list[tuple[int, int]] = []
        acia.tx_callback = lambda port, value: calls.append((port, value))

        # $FFFFC9 is the SCSI bus, not an ACIA alias
        acia.write(0xFFFFC9, 1, 0x41)

        assert calls == []
        assert acia.get_tx_output(0) == []
