#!/usr/bin/env python3
"""Dump native-boot module chains at a chosen cut point.

By default this stops at the first native terminal transmit, which is the
last point where JOBCUR/JCB module pointers are still sane in current traces.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, ".")

from alphasim.config import SystemConfig
from alphasim.devices.sasi import SASIController
from alphasim.main import build_system


REPO_ROOT = Path(__file__).resolve().parent
ROM_EVEN = REPO_ROOT / "roms" / "AM-178-01-B05.BIN"
ROM_ODD = REPO_ROOT / "roms" / "AM-178-00-B05.BIN"
BOOT_IMAGE = REPO_ROOT / "images" / "AMOS_1-3_Boot_OS.img"

JOBCUR = 0x041C
SYSBAS = 0x0414
RAD50_ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def rad50_decode(word: int) -> str:
    c3 = word % 40
    word //= 40
    c2 = word % 40
    word //= 40
    c1 = word % 40
    return RAD50_ALPHABET[c1] + RAD50_ALPHABET[c2] + RAD50_ALPHABET[c3]


@dataclass(frozen=True)
class DiskRead:
    lba: int
    count: int
    size: int | None


class RecordingTarget:
    def __init__(self, backend) -> None:
        self._backend = backend
        self.read_calls: list[DiskRead] = []

    def read_sectors(self, lba: int, count: int) -> bytes | None:
        data = self._backend.read_sectors(lba, count)
        self.read_calls.append(
            DiskRead(lba=lba, count=count, size=len(data) if data is not None else None)
        )
        return data

    @property
    def sector_count(self) -> int:
        return self._backend.sector_count


def module_name(bus, header_addr: int) -> tuple[str, str, tuple[int, int, int]]:
    w1 = bus.read_word(header_addr + 6)
    w2 = bus.read_word(header_addr + 8)
    w3 = bus.read_word(header_addr + 10)
    name = (rad50_decode(w1) + rad50_decode(w2)).rstrip()
    ext = rad50_decode(w3).rstrip()
    return name, ext, (w1, w2, w3)


def dump_modules(bus, start: int, label: str, limit: int) -> None:
    print(f"\n=== {label} @ ${start:08X} ===")
    if start == 0:
        print("  <null>")
        return

    addr = start & 0xFFFFFF
    for index in range(limit):
        if addr == 0 or addr >= 0x400000:
            print(f"  stop: invalid addr ${addr:06X}")
            return

        size = read_long(bus, addr)
        if size == 0:
            print(f"  [{index}] sentinel zero long at ${addr:06X}")
            return

        if size < 12 or size > 0x100000 or (size & 1):
            print(f"  [{index}] invalid size ${size:08X} at ${addr:06X}")
            for offset in range(0, 16, 2):
                print(f"    +${offset:02X}: ${bus.read_word(addr + offset):04X}")
            return

        flags = bus.read_word(addr + 4)
        name, ext, raw = module_name(bus, addr)
        data_addr = addr + 12
        title = f"{name}.{ext}" if name or ext else "<unnamed>"
        print(
            f"  [{index}] hdr=${addr:06X} data=${data_addr:06X} size=${size:08X} "
            f"flags=${flags:04X} name={title} raw={raw[0]:04X}/{raw[1]:04X}/{raw[2]:04X}"
        )
        addr += size

    print(f"  stop: limit {limit} reached")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stop",
        choices=("first-tx", "extra"),
        default="first-tx",
        help="Where to stop the dump.",
    )
    parser.add_argument(
        "--extra-instructions",
        type=int,
        default=5_000_000,
        help="Extra instructions to run after first TX when --stop=extra.",
    )
    parser.add_argument(
        "--module-limit",
        type=int,
        default=16,
        help="Maximum modules to print per chain.",
    )
    args = parser.parse_args()

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
    sasi = next(device for _, _, device in bus._devices if isinstance(device, SASIController))
    assert sasi.target is not None
    recording_target = RecordingTarget(sasi.target)
    sasi.target = recording_target

    first_tx: tuple[int, int, int, int, int, int | None] | None = None

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx
        if first_tx is None:
            last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None
            first_tx = (cpu.pc, cpu.cycles, port, value, len(recording_target.read_calls), last_lba)

    acia.tx_callback = tx_callback
    cpu.reset()

    instructions = 0
    while instructions < config.max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1
        if first_tx is not None:
            break

    if first_tx is None:
        print("Did not reach first TX")
        return 1

    print(
        f"first_tx=(pc=${first_tx[0]:06X}, cycles={first_tx[1]}, port={first_tx[2]}, "
        f"value=${first_tx[3]:02X}, reads={first_tx[4]}, last_lba={first_tx[5]})"
    )
    print(f"instructions={instructions} pc=${cpu.pc:06X} leds={[f'{x:02X}' for x in led.history]}")

    if args.stop == "extra":
        for _ in range(args.extra_instructions):
            if cpu.halted:
                break
            cycles = cpu.step()
            bus.tick(cycles)
            instructions += 1
        print(f"after extra={args.extra_instructions}: instructions={instructions} pc=${cpu.pc:06X}")

    jobcur = read_long(bus, JOBCUR)
    sysbas = read_long(bus, SYSBAS)
    print(f"JOBCUR=${jobcur:08X} SYSBAS=${sysbas:08X}")

    jcb_chain = 0
    if 0 < (jobcur & 0xFFFFFF) < 0x400000:
        jcb_chain = read_long(bus, (jobcur & 0xFFFFFF) + 0x0C)
        print(f"JCB+$0C=${jcb_chain:08X}")
    else:
        print("JCB+$0C=<invalid JOBCUR>")

    print("\nRecent disk reads:")
    for read in recording_target.read_calls[-12:]:
        print(f"  LBA={read.lba} count={read.count} size={read.size}")

    dump_modules(bus, sysbas, "SYSBAS chain", args.module_limit)
    dump_modules(bus, jcb_chain, "JCB+$0C chain", args.module_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
