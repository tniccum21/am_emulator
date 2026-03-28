"""Integration target: native CPU-feature probe follows the 68010 fallback path."""

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
def test_native_cpu_probe_reaches_selector_with_d1_two():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(SELECTOR_IMAGE)
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: (cpu.pc & 0xFFFFFF) == 0x00ED40,
        max_instructions=1_200_000,
    )

    assert result.completed, (
        "Native boot did not reach the CPU-feature selector at $00ED40. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert cpu.d[1] == 0x00000002, f"D1=${cpu.d[1]:08X}"
    assert bus.read_long(0x0400) == 0x00300404, f"SYSTEM=${bus.read_long(0x0400):08X}"


@pytest.mark.skipif(
    not require_native_boot_assets() or not SELECTOR_IMAGE.exists(),
    reason="ROM files or selector-trace disk image not present",
)
def test_native_cpu_probe_68020_reaches_selector_with_d1_eight():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        SELECTOR_IMAGE,
        cpu_model="68020",
    )
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: (cpu.pc & 0xFFFFFF) == 0x00ED40,
        max_instructions=1_200_000,
    )

    assert result.completed, (
        "Native boot did not reach the CPU-feature selector at $00ED40. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert cpu.d[1] == 0x00000008, f"D1=${cpu.d[1]:08X}"
    assert bus.read_long(0x0400) == 0x00300404, f"SYSTEM=${bus.read_long(0x0400):08X}"
