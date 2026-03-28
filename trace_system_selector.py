"""Trace the native SYSTEM/D1 selector that feeds the $00ED40 branch ladder.

This reproduces the native path to the first live $00ED40 hit and records the
last D1/D6 changes leading into that decision. Optional forcing of high SYSTEM
bits at $00F7B8 lets us test which upstream selector bit turns the clean
68010-style path into the branches that later set SYSTEM|=$00008000.
"""

from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from alphasim.config import SystemConfig
from alphasim.main import _patch_boot_monitor_override, build_system
from alphasim.cpu.disassemble import disassemble_one


def parse_int(value: str) -> int:
    return int(value, 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trace the native SYSTEM selector that produces D1 for $00ED40."
    )
    parser.add_argument(
        "--system-mask",
        type=parse_int,
        default=0,
        help="OR this mask into SYSTEM once at --force-pc before tracing",
    )
    parser.add_argument(
        "--force-pc",
        type=parse_int,
        default=0x00F7B8,
        help="PC at which --system-mask is applied once",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=1_200_000,
        help="Maximum instructions to execute",
    )
    parser.add_argument(
        "--change-limit",
        type=int,
        default=24,
        help="How many D1/D6 changes to retain",
    )
    parser.add_argument(
        "--boot-monitor",
        type=str,
        default=None,
        help="Override the ROM monitor lookup filename, for example TEST4.MON",
    )
    parser.add_argument(
        "--latch-ffff59",
        action="store_true",
        help="Make byte reads/writes at $FFFF59 behave like a simple latch",
    )
    parser.add_argument(
        "--cpu-model",
        choices=["68010", "68020", "68030", "68040"],
        default="68010",
        help="Expose the requested CPU model to MOVEC-based monitor probes",
    )
    args = parser.parse_args()

    config = SystemConfig(
        rom_even_path=Path("roms/AM-178-01-B05.BIN"),
        rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
        ram_size=0x400000,
        config_dip=0x0A,
        disk_image_path=Path("images/HD0-V1.4C-Bootable-on-1400.img"),
        cpu_model=args.cpu_model,
    )
    cpu, bus, led, _acia = build_system(config)
    if args.boot_monitor:
        _patch_boot_monitor_override(bus, args.boot_monitor)

    if args.latch_ffff59:
        orig_read = bus._read_byte_physical
        orig_write = bus._write_byte_physical
        latch = {"value": 0xFF}

        def wrapped_read(address: int) -> int:
            addr = address & 0xFFFFFF
            if addr == 0xFFFF59:
                return latch["value"]
            return orig_read(address)

        def wrapped_write(address: int, value: int) -> None:
            addr = address & 0xFFFFFF
            if addr == 0xFFFF59:
                latch["value"] = value & 0xFF
                return
            orig_write(address, value)

        bus._read_byte_physical = wrapped_read
        bus._write_byte_physical = wrapped_write

    cpu.reset()

    d1_changes: deque[str] = deque(maxlen=args.change_limit)
    d6_changes: deque[str] = deque(maxlen=args.change_limit)
    prev_d1 = cpu.d[1] & 0xFFFFFFFF
    prev_d6 = cpu.d[6] & 0xFFFFFFFF
    forced = False
    d1_at_ed40: int | None = None
    system_before: int | None = None

    for step in range(args.max_steps):
        pc = cpu.pc & 0xFFFFFF
        if args.system_mask and not forced and pc == args.force_pc:
            bus.write_long(0x0400, bus.read_long(0x0400) | args.system_mask)
            forced = True

        if pc == 0x00ED40:
            d1_at_ed40 = cpu.d[1] & 0xFFFFFFFF
            system_before = bus.read_long(0x0400) & 0xFFFFFFFF

        try:
            disasm, _ = disassemble_one(bus, pc)
        except Exception:
            disasm = "???"

        cycles = cpu.step()
        new_d1 = cpu.d[1] & 0xFFFFFFFF
        new_d6 = cpu.d[6] & 0xFFFFFFFF
        if new_d1 != prev_d1:
            d1_changes.append(
                f"{step:7d} pc=${pc:06X} {disasm} "
                f"D1 ${prev_d1:08X}->${new_d1:08X} "
                f"D0=${cpu.d[0] & 0xFFFFFFFF:08X} "
                f"D6=${cpu.d[6] & 0xFFFFFFFF:08X} "
                f"D7=${cpu.d[7] & 0xFFFFFFFF:08X}"
            )
            prev_d1 = new_d1
        if new_d6 != prev_d6:
            d6_changes.append(
                f"{step:7d} pc=${pc:06X} {disasm} "
                f"D6 ${prev_d6:08X}->${new_d6:08X} "
                f"D0=${cpu.d[0] & 0xFFFFFFFF:08X} "
                f"D1=${cpu.d[1] & 0xFFFFFFFF:08X} "
                f"D7=${cpu.d[7] & 0xFFFFFFFF:08X}"
            )
            prev_d6 = new_d6
        bus.tick(cycles)

        if pc == 0x00EDA0 and d1_at_ed40 is not None:
            break

    system_after = bus.read_long(0x0400) & 0xFFFFFFFF
    print(
        f"cpu_model={args.cpu_model} "
        f"forced={forced} mask=${args.system_mask:08X} "
        f"pc=${cpu.pc & 0xFFFFFF:06X} cycles={cpu.cycles} "
        f"leds={[f'{value:02X}' for value in led.history]}"
    )
    if d1_at_ed40 is not None:
        print(
            f"ed40_d1=${d1_at_ed40:08X} "
            f"system_before=${(system_before or 0):08X} "
            f"system_after=${system_after:08X}"
        )
    else:
        print("ed40_d1=<not reached>")

    print("recent D1 changes:")
    for entry in d1_changes:
        print(entry)

    print("recent D6 changes:")
    for entry in d6_changes:
        print(entry)


if __name__ == "__main__":
    main()
