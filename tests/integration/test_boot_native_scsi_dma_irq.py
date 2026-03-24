"""Integration target: native 68020 boot takes the low-memory SCSI DMA IRQ on level 2."""

from __future__ import annotations

from pathlib import Path

import pytest

from alphasim.devices.scsi_bus import SCSIBusInterface

from .boot_helpers import build_native_boot_system, require_native_boot_assets, run_native_boot


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTOR_IMAGE = REPO_ROOT / "images" / "HD0-V1.4C-Bootable-on-1400.img"


@pytest.mark.skipif(
    not require_native_boot_assets() or not SELECTOR_IMAGE.exists(),
    reason="ROM files or selector-trace disk image not present",
)
def test_native_68020_boot_acknowledges_scsi_dma_irq_on_level_two():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        SELECTOR_IMAGE,
        cpu_model="68020",
    )
    scsi = next(
        device
        for _, _, device in bus._devices
        if isinstance(device, SCSIBusInterface)
    )
    trace: list[str] = []
    scsi.trace_callback = trace.append
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: any("SCSI IRQ ack level=2 vector=26" in entry for entry in trace),
        max_instructions=5_000_000,
    )

    assert result.completed, (
        "Native 68020 boot did not acknowledge the low-memory SCSI DMA interrupt "
        f"on level 2. pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]} "
        f"trace_tail={trace[-12:]!r}"
    )
    assert any("SCSI READ lba=2 count=1" in entry for entry in trace), trace
    assert not any("level=5" in entry for entry in trace), trace


@pytest.mark.skipif(
    not require_native_boot_assets() or not SELECTOR_IMAGE.exists(),
    reason="ROM files or selector-trace disk image not present",
)
def test_native_68020_scsi_irq_helper_rte_returns_to_monitor_code():
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        SELECTOR_IMAGE,
        cpu_model="68020",
    )
    cpu.reset()

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: (cpu.pc & 0xFFFFFF) == 0x004EF8,
        max_instructions=4_200_000,
    )

    assert result.completed, (
        "Native 68020 boot did not reach the low-memory SCSI IRQ return helper "
        f"at $004EF8. pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )

    cpu.step()
    bus.tick(0)
    assert (cpu.pc & 0xFFFFFF) == 0x004EFC, f"pc=${cpu.pc & 0xFFFFFF:06X}"

    cpu.step()
    bus.tick(0)
    assert (cpu.pc & 0xFFFFFF) == 0x001C98, f"pc=${cpu.pc & 0xFFFFFF:06X}"
    assert cpu.sr == 0x0019, f"sr=${cpu.sr:04X}"
