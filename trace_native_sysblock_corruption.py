#!/usr/bin/env python3
"""Stop on the first post-TX corruption of the native system variable block."""

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

TOPRAM = 0x0410
SYSBAS = 0x0414
JOBCUR = 0x041C
JOBMAX = 0x0426
MEMBAS = 0x0430
SVSTK = 0x0434
MEMEND = 0x0438
WATCH_START = 0x0410
WATCH_END = 0x043B


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def snapshot(bus) -> tuple[int, int, int, int, int, int]:
    return (
        read_long(bus, TOPRAM),
        read_long(bus, SYSBAS),
        read_long(bus, JOBCUR),
        bus.read_word(JOBMAX),
        read_long(bus, MEMBAS),
        read_long(bus, SVSTK),
        read_long(bus, MEMEND),
    )


def format_snapshot(bus) -> str:
    topram, sysbas, jobcur, jobmax, membas, svstk, memend = snapshot(bus)
    return (
        f"TOPRAM=${topram:08X} SYSBAS=${sysbas:08X} JOBCUR=${jobcur:08X} "
        f"JOBMAX=${jobmax:04X} MEMBAS=${membas:08X} SVSTK=${svstk:08X} MEMEND=${memend:08X}"
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
    baseline: tuple[int, int, int, int, int, int, int] | None = None
    write_log: list[str] = []
    stop_reason: str | None = None

    orig_write = bus._write_byte_physical

    def wrapped_write(address: int, value: int) -> None:
        nonlocal baseline, stop_reason
        addr = address & 0xFFFFFF
        before = snapshot(bus) if first_tx and WATCH_START <= addr <= WATCH_END else None
        orig_write(address, value)
        if not first_tx or not (WATCH_START <= addr <= WATCH_END):
            return

        after = snapshot(bus)
        if len(write_log) < 256:
            write_log.append(
                f"PC=${cpu.pc:06X} WRITE ${addr:06X} <- ${value & 0xFF:02X} "
                f"A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
                f"A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X} "
                f"before=[TOP=${before[0]:08X} SYS=${before[1]:08X} JOB=${before[2]:08X} "
                f"JMAX=${before[3]:04X} MB=${before[4]:08X} SS=${before[5]:08X} ME=${before[6]:08X}] "
                f"after=[TOP={after[0]:08X} SYS={after[1]:08X} JOB={after[2]:08X} "
                f"JMAX={after[3]:04X} MB={after[4]:08X} SS={after[5]:08X} ME={after[6]:08X}]"
            )

        assert baseline is not None
        if after != baseline and stop_reason is None:
            stop_reason = (
                f"first divergent write at PC=${cpu.pc:06X} addr=${addr:06X} value=${value & 0xFF:02X}"
            )

    bus._write_byte_physical = wrapped_write

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx, tx_info, baseline
        if not first_tx:
            first_tx = True
            tx_info = (cpu.pc, cpu.cycles, port, value)
            baseline = snapshot(bus)

    acia.tx_callback = tx_callback
    cpu.reset()

    instructions = 0
    while instructions < config.max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1
        if first_tx:
            break

    if not first_tx or tx_info is None or baseline is None:
        print("Did not reach first TX")
        return 1

    print(
        f"First TX at pc=${tx_info[0]:06X} cycles={tx_info[1]} "
        f"port={tx_info[2]} value=${tx_info[3]:02X}"
    )
    print(f"Baseline: {format_snapshot(bus)}")

    pc_hits: list[str] = []
    max_after_tx = 200_000
    for step in range(max_after_tx):
        if cpu.halted or stop_reason is not None:
            break

        if cpu.pc in (0x006B68, 0x006C10, 0x001230, 0x001338, 0x000B48, 0x000E68, 0x001C30):
            if len(pc_hits) < 64:
                pc_hits.append(
                    f"[{step}] pc=${cpu.pc:06X} A5=${cpu.a[5]&0xFFFFFF:06X} "
                    f"A6=${cpu.a[6]&0xFFFFFF:06X} {format_snapshot(bus)}"
                )

        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1

    print("\nStop:")
    if stop_reason is not None:
        print(f"  {stop_reason}")
    else:
        print(f"  no divergence in {max_after_tx} instructions post-TX")

    print(f"\nFinal: pc=${cpu.pc:06X} cycles={cpu.cycles} {format_snapshot(bus)}")

    if pc_hits:
        print("\nPC hits:")
        for line in pc_hits:
            print(f"  {line}")

    if write_log:
        print("\nWrites:")
        for line in write_log:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
