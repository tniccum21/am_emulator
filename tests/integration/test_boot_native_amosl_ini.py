"""Integration target: native boot reaches AMOSL.INI before terminal output."""

from __future__ import annotations

import pytest

from .boot_helpers import (
    RecordingTarget,
    build_native_boot_system,
    find_amosl_ini_start_block,
    find_boot_image,
    require_native_boot_assets,
    run_native_boot,
)


BOOT_IMAGE = find_boot_image()
AMOSL_INI_START_BLOCK = find_amosl_ini_start_block()


@pytest.mark.skipif(
    not require_native_boot_assets() or AMOSL_INI_START_BLOCK is None,
    reason="ROM files, boot image, or AMOSL.INI location not available",
)
@pytest.mark.xfail(
    reason="Native boot emits ACIA output before it reaches the AMOSL.INI load stage.",
)
def test_native_boot_reads_amosl_ini_before_terminal_output():
    cpu, _bus, led, acia, sasi = build_native_boot_system(BOOT_IMAGE)
    assert sasi.target is not None

    recording_target = RecordingTarget(sasi.target)
    sasi.target = recording_target
    state: dict[str, object] = {
        "first_tx": None,
        "first_ini_read": None,
        "first_ini_pc": None,
        "first_ini_cycles": None,
    }

    def tx_callback(port: int, value: int) -> None:
        if state["first_tx"] is None:
            state["first_tx"] = {
                "port": port,
                "value": value,
                "pc": cpu.pc,
                "cycles": cpu.cycles,
                "read_count": len(recording_target.read_calls),
                "last_lba": recording_target.read_calls[-1].lba if recording_target.read_calls else None,
                "leds": tuple(led.history),
            }

    acia.tx_callback = tx_callback

    cpu.reset()
    target_lba = AMOSL_INI_START_BLOCK + 1

    def stop() -> bool:
        if state["first_ini_read"] is None:
            for read in recording_target.read_calls[-2:]:
                if read.lba >= target_lba:
                    state["first_ini_read"] = read
                    state["first_ini_pc"] = cpu.pc
                    state["first_ini_cycles"] = cpu.cycles
                    break
        return state["first_tx"] is not None or state["first_ini_read"] is not None

    result = run_native_boot(
        cpu,
        _bus,
        led,
        stop=stop,
        max_instructions=25_000_000,
    )

    assert state["first_ini_read"] is not None, (
        f"Native boot did not reach AMOSL.INI before terminal output. "
        f"target_lba={target_lba} last_lba={recording_target.read_calls[-1].lba if recording_target.read_calls else 'none'} "
        f"first_tx={state['first_tx']} "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
    assert state["first_tx"] is None, (
        f"Native boot reached AMOSL.INI only after ACIA output began. "
        f"first_tx={state['first_tx']} first_ini_lba={state['first_ini_read'].lba} "
        f"pc=${result.pc:06X} leds={[f'{value:02X}' for value in result.led_history]}"
    )
