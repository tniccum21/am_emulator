#!/usr/bin/env python3
"""Trace the first loss of JOBCUR/JOBQ after native AMOSL.MON handoff."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

from alphasim.config import SystemConfig
from alphasim.main import build_system


REPO_ROOT = Path(__file__).resolve().parent
ROM_EVEN = REPO_ROOT / "roms" / "AM-178-01-B05.BIN"
ROM_ODD = REPO_ROOT / "roms" / "AM-178-00-B05.BIN"
BOOT_IMAGE = REPO_ROOT / "images" / "AMOS_1-3_Boot_OS.img"

DDT_ADDR = 0x7038
JOBQ = DDT_ADDR + 0x78
JCB_CMD_FILE = DDT_ADDR + 0x20
JOBCUR = 0x041C
SYSBAS = 0x0414


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def dump_state(prefix: str, cpu, bus, led) -> None:
    print(
        f"{prefix} pc=${cpu.pc:06X} cycles={cpu.cycles} "
        f"JOBCUR=${read_long(bus, JOBCUR):08X} JOBQ=${read_long(bus, JOBQ):08X} "
        f"SYSBAS=${read_long(bus, SYSBAS):08X} JCB+$20=${bus.read_word(JCB_CMD_FILE):04X} "
        f"A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
        f"A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X} "
        f"leds={[f'{x:02X}' for x in led.history]}"
    )


def main() -> int:
    config = SystemConfig(
        rom_even_path=ROM_EVEN,
        rom_odd_path=ROM_ODD,
        ram_size=0x400000,
        config_dip=0x0A,
        disk_image_path=BOOT_IMAGE,
        trace_enabled=False,
        max_instructions=40_000_000,
        breakpoints=[],
    )
    cpu, bus, led, _acia = build_system(config)

    handoff_complete = False
    write_log: list[str] = []
    stop_reason: str | None = None
    post_handoff_steps = 0

    orig_write = bus._write_byte_physical

    def wrapped_write(address: int, value: int) -> None:
        nonlocal stop_reason
        addr = address & 0xFFFFFF
        before_jobcur = read_long(bus, JOBCUR)
        before_jobq = read_long(bus, JOBQ)
        orig_write(address, value)

        if not handoff_complete:
            return
        if addr not in (
            JOBCUR, JOBCUR + 1, JOBCUR + 2, JOBCUR + 3,
            JOBQ, JOBQ + 1, JOBQ + 2, JOBQ + 3,
            JCB_CMD_FILE, JCB_CMD_FILE + 1,
        ):
            return

        after_jobcur = read_long(bus, JOBCUR)
        after_jobq = read_long(bus, JOBQ)
        line = (
            f"WRITE pc=${cpu.pc:06X} addr=${addr:06X} value=${value & 0xFF:02X} "
            f"JOBCUR ${before_jobcur:08X}->{after_jobcur:08X} "
            f"JOBQ ${before_jobq:08X}->{after_jobq:08X} "
            f"JCB+$20=${bus.read_word(JCB_CMD_FILE):04X} "
            f"A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
            f"A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X}"
        )
        if len(write_log) < 64:
            write_log.append(line)

        if stop_reason is None and JOBQ <= addr <= JOBQ + 3:
            stop_reason = line

    bus._write_byte_physical = wrapped_write

    cpu.reset()
    instructions = 0

    while instructions < config.max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1

        if not handoff_complete and tuple(led.history[:6]) == (0x06, 0x0B, 0x00, 0x0E, 0x0F, 0x00):
            handoff_complete = True
            print("Reached native handoff milestone.")
            dump_state("Handoff:", cpu, bus, led)

        if handoff_complete:
            post_handoff_steps += 1

        if stop_reason is not None:
            break
        if handoff_complete and post_handoff_steps >= 2_000_000:
            break

    print()
    if stop_reason is None:
        print("No JOBQ writes observed within post-handoff trace window.")
    else:
        print(f"Stop reason: {stop_reason}")
    dump_state("Final:", cpu, bus, led)

    if write_log:
        print("\nRelevant writes:")
        for line in write_log:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
