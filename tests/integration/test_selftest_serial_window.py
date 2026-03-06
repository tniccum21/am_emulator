"""Integration target: ROM self-test serial wait uses raw port registers."""

from __future__ import annotations

from collections import Counter

import pytest

from .boot_helpers import build_native_boot_system, roms_available, run_native_boot


SERIAL_PORT_WINDOW = {
    0xFFFE20,
    0xFFFE22,
    0xFFFE24,
    0xFFFE26,
    0xFFFE28,
    0xFFFE30,
    0xFFFE32,
}
HW_SER_ALIAS = {0xFFFFC8, 0xFFFFC9}


@pytest.mark.skipif(
    not roms_available(),
    reason="ROM files not present",
)
def test_rom_selftest_5b_window_uses_main_port_registers_not_hw_ser_alias():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        disk_image_path=None,
        config_dip=0x2A,
    )

    original_read = bus._read_byte_physical
    original_write = bus._write_byte_physical
    io_counts: Counter[int] = Counter()
    write_events: list[tuple[int, int]] = []

    capture_active = False

    def wrapped_read(address: int) -> int:
        value = original_read(address)
        addr = address & 0xFFFFFF
        if capture_active and addr >= 0xFFFE00:
            io_counts[addr] += 1
        return value

    def wrapped_write(address: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if capture_active and addr >= 0xFFFE00:
            io_counts[addr] += 1
            if addr in SERIAL_PORT_WINDOW or addr in HW_SER_ALIAS:
                write_events.append((addr, value & 0xFF))
        original_write(address, value)

    bus._read_byte_physical = wrapped_read
    bus._write_byte_physical = wrapped_write
    cpu.reset()

    first_5b = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0x5B,
        max_instructions=500_000,
    )
    assert first_5b.completed, (
        f"ROM self-test never reached LED=5B. "
        f"pc=${first_5b.pc:06X} leds={[f'{value:02X}' for value in first_5b.led_history]}"
    )

    capture_active = True
    first_zero = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0x00,
        max_instructions=10_000_000,
    )
    capture_active = False

    assert first_zero.completed, (
        f"ROM self-test never returned from the first LED=5B window. "
        f"pc=${first_zero.pc:06X} leds={[f'{value:02X}' for value in first_zero.led_history]}"
    )

    touched = set(io_counts)
    missing = SERIAL_PORT_WINDOW - touched
    assert not missing, (
        f"Self-test LED=5B window missed expected main port addresses: "
        f"{[f'${addr:06X}' for addr in sorted(missing)]} "
        f"touched={[f'${addr:06X}' for addr in sorted(touched)]}"
    )
    assert not (touched & HW_SER_ALIAS), (
        f"Self-test LED=5B window unexpectedly touched HW.SER alias addresses: "
        f"{[f'${addr:06X}' for addr in sorted(touched & HW_SER_ALIAS)]}"
    )
    assert write_events[:10] == [
        (0xFFFE28, 0x15),
        (0xFFFE28, 0x25),
        (0xFFFE28, 0x45),
        (0xFFFE28, 0x85),
        (0xFFFE20, 0x03),
        (0xFFFE20, 0x15),
        (0xFFFE24, 0x03),
        (0xFFFE24, 0x15),
        (0xFFFE30, 0x03),
        (0xFFFE30, 0x15),
    ], (
        f"Unexpected self-test serial setup prefix: "
        f"{[(f'${addr:06X}', f'${value:02X}') for addr, value in write_events[:10]]}"
    )
