"""Integration target: native handoff enters the loaded AMOSL.MON monitor."""

from __future__ import annotations

import pytest

from .boot_helpers import (
    build_native_boot_system,
    find_boot_image,
    require_native_boot_assets,
    run_native_boot,
)


BOOT_IMAGE = find_boot_image()


@pytest.mark.skipif(
    not require_native_boot_assets(),
    reason="ROM files or native boot disk image not present",
)
def test_native_boot_handoff_reaches_monitor_memory_sizing():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(BOOT_IMAGE)
    cpu.reset()

    handoff = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0x0E,
        max_instructions=2_000_000,
    )
    assert handoff.completed, (
        f"Native boot never reached LED=0E. "
        f"pc=${handoff.pc:06X} leds={[f'{value:02X}' for value in handoff.led_history]}"
    )

    monitor_entry = bus.read_long(0x30)
    assert monitor_entry == 0x0000805A, f"OS entry vector at $0030 = ${monitor_entry:08X}"

    monitor_start = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0x0F,
        max_instructions=10_000,
    )
    assert monitor_start.completed, (
        f"AMOSL.MON did not advance into the LED=0F memory-sizing stage. "
        f"pc=${monitor_start.pc:06X} leds={[f'{value:02X}' for value in monitor_start.led_history]}"
    )

    sizing_complete = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: led.value == 0x00 and len(led.history) >= 6,
        max_instructions=4_000_000,
    )
    assert sizing_complete.completed, (
        f"AMOSL.MON did not complete the LED=0F memory-sizing stage. "
        f"pc=${sizing_complete.pc:06X} leds={[f'{value:02X}' for value in sizing_complete.led_history]}"
    )
    assert tuple(led.history[:6]) == (0x06, 0x0B, 0x00, 0x0E, 0x0F, 0x00)
    assert cpu.pc == 0x00811C, f"PC after memory sizing = ${cpu.pc:06X}"
