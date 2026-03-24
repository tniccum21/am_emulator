"""Integration target: native 68020 boot wakes the native PIT path."""

from __future__ import annotations

from pathlib import Path

import pytest

from .boot_helpers import build_native_boot_system, require_native_boot_assets, run_native_boot


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTOR_IMAGE = REPO_ROOT / "images" / "HD0-V1.4C-Bootable-on-1400.img"


@pytest.mark.skipif(
    not require_native_boot_assets() or not SELECTOR_IMAGE.exists(),
    reason="ROM files or selector-trace disk image not present",
)
def test_native_68020_boot_takes_native_pit_level_six_irq():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        SELECTOR_IMAGE,
        cpu_model="68020",
    )
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: (cpu.pc & 0xFFFFFF) == 0x0018E0,
        max_instructions=4_200_000,
    )

    assert result.completed, (
        "Native 68020 boot did not reach the native PIT level-6 handler at $0018E0. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]} "
        f"QHEAD=${bus.read_long(0x042A):08X} EVBUSY=${bus.read_word(0x046E):04X}"
    )


@pytest.mark.skipif(
    not require_native_boot_assets() or not SELECTOR_IMAGE.exists(),
    reason="ROM files or selector-trace disk image not present",
)
def test_native_68020_boot_reaches_later_monitor_code_after_native_pit_wake():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        SELECTOR_IMAGE,
        cpu_model="68020",
    )
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: (cpu.pc & 0xFFFFFF) == 0x001DBE,
        max_instructions=6_000_000,
    )

    assert result.completed, (
        "Native 68020 boot never reached the later monitor frontier at $001DBE after the PIT wake. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]} "
        f"QHEAD=${bus.read_long(0x042A):08X} EVBUSY=${bus.read_word(0x046E):04X}"
    )
    assert bus.read_long(0x042A) == 0, f"QHEAD=${bus.read_long(0x042A):08X}"
    assert bus.read_word(0x046E) == 0, f"EVBUSY=${bus.read_word(0x046E):04X}"
