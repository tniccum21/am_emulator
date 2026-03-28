"""Integration target: ROM self-test banner follows the matched serial port."""

from __future__ import annotations

import pytest

from .boot_helpers import build_native_boot_system, roms_available, run_native_boot


MAIN_PORT_BASES = (0xFFFE20, 0xFFFE24, 0xFFFE30)
EXPECTED_LINE = b"300 baud detected\r\n"


@pytest.mark.skipif(
    not roms_available(),
    reason="ROM files not present",
)
@pytest.mark.parametrize("matched_port_base", MAIN_PORT_BASES)
def test_rom_selftest_banner_stays_on_matched_port(matched_port_base: int):
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        disk_image_path=None,
        config_dip=0x2A,
    )

    original_read = bus._read_byte_physical
    original_write = bus._write_byte_physical
    port_writes: dict[int, list[int]] = {base + 2: [] for base in MAIN_PORT_BASES}

    def wrapped_read(address: int) -> int:
        addr = address & 0xFFFFFF
        value = original_read(address)
        if led.value == 0x5B:
            if addr == matched_port_base:
                return value | 0x01
            if addr == matched_port_base + 2:
                return 0x20
        return value

    def wrapped_write(address: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if led.value >= 0xB5 and addr in port_writes:
            port_writes[addr].append(value & 0xFF)
        original_write(address, value)

    bus._read_byte_physical = wrapped_read
    bus._write_byte_physical = wrapped_write
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0x84,
        max_instructions=2_000_000,
    )

    assert result.completed, (
        f"ROM self-test did not reach LED=84 after matching ${matched_port_base:06X}. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )

    matched_data = matched_port_base + 2
    assert bytes(port_writes[matched_data][: len(EXPECTED_LINE)]) == EXPECTED_LINE, (
        f"Expected self-test banner on ${matched_data:06X}, got {port_writes[matched_data]!r}. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )

    other_writes = {
        f"${addr:06X}": values
        for addr, values in port_writes.items()
        if addr != matched_data and values
    }
    assert not other_writes, (
        f"Unexpected banner writes on non-matched ports: {other_writes}. "
        f"matched=${matched_data:06X} pc=${result.pc:06X}"
    )
