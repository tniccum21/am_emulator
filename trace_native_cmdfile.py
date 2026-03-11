#!/usr/bin/env python3
"""Trace the native post-boot command-file path without injecting input."""

from __future__ import annotations

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
DDT_ADDR = 0x7038
JCB_CMD_FILE = DDT_ADDR + 0x20
JCB_TCB_PTR = DDT_ADDR + 0x38
JCB_MOD_CHAIN = DDT_ADDR + 0x0C

WATCH_PCS = {
    0x36F4,
    0x3710,
    0x3720,
    0x375E,
    0x390A,
    0x392C,
    0x3932,
    0x1C30,
    0x1C6E,
    0x006B68,
}
WATCH_OPS = {
    0xA008: "TTYLIN",
    0xA01C: "SCNMOD",
    0xA03C: "QUEUEIO",
    0xA03E: "IOWAIT",
    0xA052: "A052",
    0xA064: "A064",
    0xA0DC: "A0DC",
}


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


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


def dump_state(prefix: str, cpu, bus, recording_target: RecordingTarget, op: int | None = None) -> None:
    last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None
    opname = f" op=${op:04X}" if op is not None else ""
    print(
        f"{prefix} pc=${cpu.pc:06X}{opname} D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} "
        f"D6=${cpu.d[6]:08X} A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
        f"A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X} "
        f"JOBCUR=${read_long(bus, JOBCUR):08X} SYSBAS=${read_long(bus, SYSBAS):08X} "
        f"JCB+$0C=${read_long(bus, JCB_MOD_CHAIN):08X} JCB+$20=${bus.read_word(JCB_CMD_FILE):04X} "
        f"JCB+$38=${read_long(bus, JCB_TCB_PTR):08X} last_lba={last_lba}"
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
    sasi = next(device for _, _, device in bus._devices if isinstance(device, SASIController))
    assert sasi.target is not None
    recording_target = RecordingTarget(sasi.target)
    sasi.target = recording_target

    first_tx: tuple[int, int, int, int] | None = None

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx
        if first_tx is None:
            first_tx = (cpu.pc, cpu.cycles, port, value)

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
        f"First TX at pc=${first_tx[0]:06X} cycles={first_tx[1]} "
        f"port={first_tx[2]} value=${first_tx[3]:02X}"
    )
    dump_state("State at first TX:", cpu, bus, recording_target)

    seen_counts: dict[int, int] = {}
    a03e_count = 0
    max_after_tx = 8_000_000

    for _ in range(max_after_tx):
        if cpu.halted:
            break
        pc = cpu.pc
        try:
            op = bus.read_word(pc)
        except Exception:
            op = None

        if pc in WATCH_PCS:
            count = seen_counts.get(pc, 0) + 1
            seen_counts[pc] = count
            if count <= 12:
                dump_state(f"[PC {count}] ", cpu, bus, recording_target, op)

        if op in WATCH_OPS:
            label = WATCH_OPS[op]
            if op == 0xA03E:
                a03e_count += 1
                if a03e_count <= 12 or a03e_count in (20, 50, 100, 200, 500, 1000):
                    dump_state(f"[{label} {a03e_count}] ", cpu, bus, recording_target, op)
            else:
                count = seen_counts.get(op, 0) + 1
                seen_counts[op] = count
                if count <= 12:
                    dump_state(f"[{label} {count}] ", cpu, bus, recording_target, op)

        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1

    print("\nFinal:")
    dump_state("  ", cpu, bus, recording_target)
    print(f"  pc=${cpu.pc:06X} cycles={cpu.cycles} leds={[f'{x:02X}' for x in led.history]}")
    print("  Recent disk reads:")
    for read in recording_target.read_calls[-16:]:
        print(f"    LBA={read.lba} count={read.count} size={read.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
