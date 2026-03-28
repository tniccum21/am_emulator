"""Integration target: ROM self-test accepts a raw space-match on any main port."""

from __future__ import annotations

import pytest

from .boot_helpers import build_native_boot_system, roms_available, run_native_boot


@pytest.mark.skipif(
    not roms_available(),
    reason="ROM files not present",
)
@pytest.mark.parametrize("port_base", [0xFFFE20, 0xFFFE24, 0xFFFE30])
def test_rom_selftest_space_match_reaches_b5_for_each_main_port(port_base: int):
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        disk_image_path=None,
        config_dip=0x2A,
    )

    original_read = bus._read_byte_physical
    state = {
        "status_reads": 0,
        "data_reads": 0,
    }

    def wrapped_read(address: int) -> int:
        addr = address & 0xFFFFFF
        value = original_read(address)
        if led.value == 0x5B:
            if addr == port_base:
                state["status_reads"] += 1
                return value | 0x01
            if addr == port_base + 2:
                state["data_reads"] += 1
                return 0x20
        return value

    bus._read_byte_physical = wrapped_read
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0xB5,
        max_instructions=1_000_000,
    )

    assert result.completed, (
        f"ROM self-test did not reach LED=B5 with injected space on ${port_base:06X}. "
        f"status_reads={state['status_reads']} data_reads={state['data_reads']} "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert state["status_reads"] > 0, f"No status reads observed on ${port_base:06X}"
    assert state["data_reads"] > 0, f"No data reads observed on ${port_base + 2:06X}"


@pytest.mark.skipif(
    not roms_available(),
    reason="ROM files not present",
)
def test_rom_selftest_ignores_hw_ser_alias_for_space_match():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        disk_image_path=None,
        config_dip=0x2A,
    )

    original_read = bus._read_byte_physical
    state = {
        "alias_status_reads": 0,
        "alias_data_reads": 0,
    }

    def wrapped_read(address: int) -> int:
        addr = address & 0xFFFFFF
        value = original_read(address)
        if led.value == 0x5B:
            if addr == 0xFFFFC8:
                state["alias_status_reads"] += 1
                return value | 0x01
            if addr == 0xFFFFC9:
                state["alias_data_reads"] += 1
                return 0x20
        return value

    bus._read_byte_physical = wrapped_read
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0xB5,
        max_instructions=1_000_000,
    )

    assert not result.completed, (
        f"ROM self-test unexpectedly reached LED=B5 from HW.SER alias injection only. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert state["alias_status_reads"] == 0
    assert state["alias_data_reads"] == 0
