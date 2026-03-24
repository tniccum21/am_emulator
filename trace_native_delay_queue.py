#!/usr/bin/env python3
"""Trace the native low-memory delayed-event queue on the 68020 selector path."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

from tests.integration.boot_helpers import build_native_boot_system


IMAGE = Path("images/HD0-V1.4C-Bootable-on-1400.img")
MAX_INSTRUCTIONS = 8_000_000

QHEAD = 0x042A
EVBUSY = 0x046E
WAKE0 = 0x04C0
JOBCUR = 0x041C
NODE = 0x7BC2
NODE_DELAY = NODE + 0x04
NODE_CALLBACK = NODE + 0x08
NODE_OWNER = NODE + 0x0C

TARGET_PCS = (
    0x00199C,
    0x001B00,
    0x001D10,
    0x001D80,
    0x001D86,
    0x001902,
    0x001910,
    0x001920,
    0x001924,
    0x00227C,
    0x002280,
)


def read_word(bus, addr: int) -> int:
    return bus.read_word(addr) & 0xFFFF


def read_long(bus, addr: int) -> int:
    return ((bus.read_word(addr) << 16) | bus.read_word(addr + 2)) & 0xFFFFFFFF


def main() -> int:
    cpu, bus, led, _acia, _sasi = build_native_boot_system(
        IMAGE,
        cpu_model="68020",
    )
    cpu.reset()

    seen: dict[int, tuple[int, int, int, int, int, int, int]] = {}
    enqueue_log: list[str] = []

    orig_write = bus._write_byte_physical

    def wrapped_write(address: int, value: int) -> None:
        addr = address & 0xFFFFFF
        orig_write(address, value)
        if addr in range(NODE, NODE_OWNER + 4) and len(enqueue_log) < 48:
            base = addr & ~0x3
            enqueue_log.append(
                f"step={steps} pc=${cpu.pc & 0xFFFFFF:06X} "
                f"addr=${addr:06X} long@${base:06X}=${read_long(bus, base):08X} "
                f"qhead=${read_long(bus, QHEAD):08X}"
            )

    bus._write_byte_physical = wrapped_write

    steps = 0
    while steps < MAX_INSTRUCTIONS and not cpu.halted:
        pc = cpu.pc & 0xFFFFFF
        if pc in TARGET_PCS and pc not in seen:
            seen[pc] = (
                steps,
                cpu.sr,
                read_long(bus, QHEAD),
                read_word(bus, EVBUSY),
                read_word(bus, WAKE0),
                read_long(bus, JOBCUR),
                read_long(bus, NODE_DELAY),
            )
        cycles = cpu.step()
        bus.tick(cycles)
        steps += 1

    print("Queue writes:")
    for line in enqueue_log:
        print(f"  {line}")

    print("\nTarget PCs:")
    for pc in TARGET_PCS:
        hit = seen.get(pc)
        if hit is None:
            print(f"  no hit pc=${pc:06X}")
            continue
        step, sr, qhead, evbusy, wake0, jobcur, delay = hit
        print(
            f"  hit pc=${pc:06X} step={step} sr=${sr:04X} "
            f"qhead=${qhead:08X} 046E=${evbusy:04X} 04C0=${wake0:04X} "
            f"JOBCUR=${jobcur:08X} delay=${delay:08X}"
        )

    print("\nFinal:")
    print(
        f"  pc=${cpu.pc & 0xFFFFFF:06X} steps={steps} "
        f"qhead=${read_long(bus, QHEAD):08X} 046E=${read_word(bus, EVBUSY):04X} "
        f"04C0=${read_word(bus, WAKE0):04X} JOBCUR=${read_long(bus, JOBCUR):08X}"
    )
    print(
        f"  node link=${read_long(bus, NODE):08X} "
        f"delay=${read_long(bus, NODE_DELAY):08X} "
        f"callback=${read_long(bus, NODE_CALLBACK):08X} "
        f"owner=${read_long(bus, NODE_OWNER):08X}"
    )
    print(f"  leds={[f'{value:02X}' for value in led.history]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
