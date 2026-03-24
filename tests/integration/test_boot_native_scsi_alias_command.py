"""Integration target: native 68020 boot reaches the low-memory SCSI data-write stage."""

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
def test_native_68020_boot_reaches_low_memory_scsi_data_write():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        SELECTOR_IMAGE,
        cpu_model="68020",
    )

    accesses: list[tuple[int, str, int, int | None]] = []
    original_read = bus._read_byte_physical
    original_write = bus._write_byte_physical

    def wrapped_read(address: int) -> int:
        value = original_read(address)
        addr = address & 0xFFFFFF
        if addr == 0xFFFFC8 and 0x0F in led.history and led.value == 0x00:
            accesses.append((cpu.pc & 0xFFFFFF, "R", addr, None))
        return value

    def wrapped_write(address: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if addr in {0xFFFFC8, 0xFFFFC9} and 0x0F in led.history and led.value == 0x00:
            accesses.append((cpu.pc & 0xFFFFFF, "W", addr, value & 0xFF))
        original_write(address, value)

    bus._read_byte_physical = wrapped_read
    bus._write_byte_physical = wrapped_write
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: any(
            op == "W" and addr == 0xFFFFC9 and value == 0x28
            for _, op, addr, value in accesses
        ),
        max_instructions=5_000_000,
    )

    assert result.completed, (
        "Native 68020 boot did not reach the low-memory SCSI command-write stage. "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]} "
        f"accesses={accesses[:12]!r}"
    )
    assert accesses[:6] == [
        (0x00A2B6, "R", 0xFFFFC8, None),
        (0x00A2C0, "W", 0xFFFFC8, 0x00),
        (0x00A2C6, "W", 0xFFFFC9, 0x00),
        (0x00A2E2, "W", 0xFFFFC8, 0x01),
        (0x00A2E8, "W", 0xFFFFC8, 0x11),
        (0x00A2B6, "R", 0xFFFFC8, None),
    ]
    assert any(
        pc == 0x00A352 and op == "W" and addr == 0xFFFFC9 and value == 0x28
        for pc, op, addr, value in accesses
    ), accesses
