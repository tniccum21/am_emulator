#!/usr/bin/env python3
"""Trace system-area corruption after the first native low-memory output."""

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

JOBCUR = 0x041C
SYSBAS = 0x0414
WATCH_ADDRS = {0x0414, 0x0415, 0x0416, 0x0417, 0x041C, 0x041D, 0x041E, 0x041F}


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def dump(prefix: str, cpu, bus) -> None:
    print(
        f"{prefix} pc=${cpu.pc:06X} D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} "
        f"A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} A5=${cpu.a[5]&0xFFFFFF:06X} "
        f"A6=${cpu.a[6]&0xFFFFFF:06X} JOBCUR=${read_long(bus, JOBCUR):08X} SYSBAS=${read_long(bus, SYSBAS):08X}"
    )


def main() -> int:
    config = SystemConfig(
        rom_even_path=ROM_EVEN,
        rom_odd_path=ROM_ODD,
        ram_size=0x400000,
        config_dip=0x0A,
        disk_image_path=BOOT_IMAGE,
        trace_enabled=False,
        max_instructions=80_000_000,
        breakpoints=[],
    )
    cpu, bus, led, acia = build_system(config)

    first_tx = False
    tx_info: tuple[int, int, int, int] | None = None
    write_log: list[str] = []

    orig_write = bus._write_byte_physical

    def wrapped_write(address: int, value: int) -> None:
        nonlocal first_tx
        addr = address & 0xFFFFFF
        if first_tx and addr in WATCH_ADDRS and len(write_log) < 128:
            write_log.append(
                f"PC=${cpu.pc:06X} WRITE ${addr:06X} <- ${value & 0xFF:02X} "
                f"JOBCUR=${read_long(bus, JOBCUR):08X} SYSBAS=${read_long(bus, SYSBAS):08X} "
                f"A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
                f"A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X}"
            )
        orig_write(address, value)

    bus._write_byte_physical = wrapped_write

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx, tx_info
        if not first_tx:
            first_tx = True
            tx_info = (cpu.pc, cpu.cycles, port, value)

    acia.tx_callback = tx_callback
    cpu.reset()

    instructions = 0
    while instructions < config.max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1
        if first_tx:
            break

    if not first_tx or tx_info is None:
        print("Did not reach first TX")
        return 1

    print(
        f"First TX at pc=${tx_info[0]:06X} cycles={tx_info[1]} "
        f"port={tx_info[2]} value=${tx_info[3]:02X}"
    )
    dump("State at first TX:", cpu, bus)

    corrupt_seen = False
    max_after_tx = 3_000_000
    pc_hits: list[str] = []

    for step in range(max_after_tx):
        if cpu.halted:
            break

        pc = cpu.pc
        if pc in (0x001230, 0x001338, 0x001C30, 0x001C6E, 0x006B68, 0x006C10) and len(pc_hits) < 64:
            pc_hits.append(
                f"[{step}] pc=${pc:06X} JOBCUR=${read_long(bus, JOBCUR):08X} "
                f"SYSBAS=${read_long(bus, SYSBAS):08X} A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X}"
            )

        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1

        jobcur = read_long(bus, JOBCUR)
        sysbas = read_long(bus, SYSBAS)
        if jobcur not in (0, 0x00007038) or sysbas != 0:
            corrupt_seen = True
            print(f"\nCorruption detected after {step + 1} instructions post-TX:")
            dump("  ", cpu, bus)
            break

    if not corrupt_seen:
        print(f"\nNo corruption detected in {max_after_tx} instructions post-TX")
        dump("  ", cpu, bus)

    if pc_hits:
        print("\nPC hits:")
        for line in pc_hits:
            print(f"  {line}")

    if write_log:
        print("\nWrites to SYSBAS/JOBCUR after first TX:")
        for line in write_log:
            print(f"  {line}")

    print(f"\nFinal pc=${cpu.pc:06X} cycles={cpu.cycles} leds={[f'{x:02X}' for x in led.history]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
