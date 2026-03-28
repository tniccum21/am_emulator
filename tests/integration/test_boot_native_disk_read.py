"""Integration test: native boot reaches the real SASI disk path."""

from __future__ import annotations

import pytest

from .boot_helpers import (
    RecordingTarget,
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
def test_native_boot_reads_disk_and_loads_low_ram():
    cpu, bus, led, _acia, sasi = build_native_boot_system(BOOT_IMAGE)
    assert sasi.target is not None

    recording_target = RecordingTarget(sasi.target)
    sasi.target = recording_target

    cpu.reset()
    initial_low_ram = bytes(bus._ram.data[:0x200])

    result = run_native_boot(
        cpu,
        bus,
        led,
        stop=lambda: bool(recording_target.read_calls) and bytes(bus._ram.data[:0x200]) != initial_low_ram,
        max_instructions=2_000_000,
    )

    low_ram = bytes(bus._ram.data[:0x200])
    assert result.completed, (
        f"Native boot did not reach disk-read + low-RAM-load milestone. "
        f"reads={len(recording_target.read_calls)} pc=${result.pc:06X} "
        f"leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert recording_target.read_calls, "Boot never reached the native SASI backend"
    assert low_ram != initial_low_ram, "Low RAM did not change after native disk reads"
    assert any(value != 0 for value in low_ram), "Low RAM changed, but remained all zero"
    assert recording_target.read_calls[0].count == 1
