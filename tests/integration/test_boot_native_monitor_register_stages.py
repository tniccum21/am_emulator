"""Integration targets: raw AMOSL.MON register access stages during native boot."""

from __future__ import annotations

from typing import Callable

import pytest

from .boot_helpers import find_boot_image, require_native_boot_assets, build_native_boot_system, run_native_boot


BOOT_IMAGE = find_boot_image()
MAIN_BLOCK = {0xFFFE20, 0xFFFE22, 0xFFFE24, 0xFFFE26, 0xFFFE28, 0xFFFE30, 0xFFFE32}
ALIAS_BLOCK = {0xFFFFC8, 0xFFFFC9}
WATCH_ADDRESSES = MAIN_BLOCK | ALIAS_BLOCK


def _capture_native_monitor_accesses(
    *,
    phase: Callable[[int, object, object], bool],
    stop_after: int,
    max_instructions: int = 25_000_000,
):
    assert BOOT_IMAGE is not None
    cpu, bus, led, _acia, _sasi = build_native_boot_system(BOOT_IMAGE)
    accesses: list[tuple[int, str, int, int | None]] = []
    original_read = bus._read_byte_physical
    original_write = bus._write_byte_physical

    def wrapped_read(address: int) -> int:
        addr = address & 0xFFFFFF
        if addr in WATCH_ADDRESSES and phase(cpu.pc, cpu, led):
            accesses.append((cpu.pc, "R", addr, None))
        return original_read(address)

    def wrapped_write(address: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if addr in WATCH_ADDRESSES and phase(cpu.pc, cpu, led):
            accesses.append((cpu.pc, "W", addr, value & 0xFF))
        original_write(address, value)

    bus._read_byte_physical = wrapped_read
    bus._write_byte_physical = wrapped_write
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: len(accesses) >= stop_after,
        max_instructions=max_instructions,
    )
    return result, accesses


@pytest.mark.skipif(
    not require_native_boot_assets(),
    reason="ROM files or boot image not present",
)
def test_native_monitor_high_stage_touches_main_port_block():
    def phase(pc: int, _cpu, led) -> bool:
        return 0x0F in led.history and led.value == 0x00 and pc >= 0x8000

    result, accesses = _capture_native_monitor_accesses(phase=phase, stop_after=3)

    assert result.completed, (
        "Native boot did not reach the first high-memory AMOSL.MON port setup writes. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert accesses == [
        (0x0082B6, "W", 0xFFFE20, 0x03),
        (0x0082BC, "W", 0xFFFE24, 0x03),
        (0x0082C2, "W", 0xFFFE30, 0x03),
    ], f"Unexpected high-memory AMOSL.MON access sequence: {accesses!r}"


@pytest.mark.skipif(
    not require_native_boot_assets(),
    reason="ROM files or boot image not present",
)
def test_native_monitor_low_stage_uses_only_alias_block():
    def phase(pc: int, _cpu, led) -> bool:
        return 0x0F in led.history and led.value == 0x00 and pc < 0x8000

    result, accesses = _capture_native_monitor_accesses(
        phase=phase,
        stop_after=10,
        max_instructions=40_000_000,
    )

    assert result.completed, (
        "Native boot did not reach the low-memory AMOSL.MON alias-access path. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )

    assert accesses[:5] == [
        (0x006B6A, "R", 0xFFFFC8, None),
        (0x006B74, "W", 0xFFFFC8, 0x00),
        (0x006B7A, "W", 0xFFFFC9, 0x00),
        (0x006B96, "W", 0xFFFFC8, 0x01),
        (0x006B9C, "W", 0xFFFFC8, 0x11),
    ], f"Unexpected low-memory AMOSL.MON alias prefix: {accesses!r}"

    unexpected_main_block = [access for access in accesses if access[2] in MAIN_BLOCK]
    assert not unexpected_main_block, (
        f"Low-memory AMOSL.MON unexpectedly touched main port block: "
        f"{unexpected_main_block!r}"
    )
