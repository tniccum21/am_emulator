"""Integration target: self-test setup writes land on the $FFFE28 placeholder."""

from __future__ import annotations

import pytest

from alphasim.devices.primary_serial_setup import PrimarySerialSetup

from .boot_helpers import build_native_boot_system, roms_available, run_native_boot


@pytest.mark.skipif(
    not roms_available(),
    reason="ROM files not present",
)
def test_rom_selftest_fffe28_setup_sequence_hits_placeholder_device():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        disk_image_path=None,
        config_dip=0x2A,
    )
    setup = next(
        device
        for start, end, device in bus._devices
        if start == 0xFFFE28 and end == 0xFFFE28 and isinstance(device, PrimarySerialSetup)
    )

    cpu.reset()
    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: len(setup.write_history) >= 4,
        max_instructions=500_000,
    )

    assert result.completed, (
        f"ROM self-test did not reach the $FFFE28 setup sequence. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert setup.write_history[:4] == [0x15, 0x25, 0x45, 0x85], (
        f"Unexpected $FFFE28 setup prefix: {[f'${value:02X}' for value in setup.write_history[:4]]}"
    )
    assert setup.read_count == 0

