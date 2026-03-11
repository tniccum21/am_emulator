#!/usr/bin/env python3
"""Trace native boot toward the real AMOSL.INI open/read path.

This script does not inject terminal input. It runs the normal native ROM boot
path, records early SASI reads, watches command-file-related JCB fields, and
halts when either:
  1. the boot reaches the first AMOSL.INI disk read, or
  2. terminal output starts first.
"""

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

DDT_ADDR = 0x7038
JCB_CMD_FILE = DDT_ADDR + 0x20
JCB_TCB_PTR = DDT_ADDR + 0x38
JOBCUR = 0x041C
SYSBAS = 0x0414

WATCH_WRITE_ADDRS = {
    JOBCUR,
    JOBCUR + 1,
    JOBCUR + 2,
    JOBCUR + 3,
    JCB_CMD_FILE,
    JCB_CMD_FILE + 1,
    JCB_TCB_PTR,
    JCB_TCB_PTR + 1,
    JCB_TCB_PTR + 2,
    JCB_TCB_PTR + 3,
}
WATCH_PCS = {0x390A, 0x392C, 0x3932, 0x3934, 0x006B68}


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def read_word_le(data: bytes | bytearray, offset: int) -> int:
    return (data[offset + 1] << 8) | data[offset]


def find_amosl_ini_start_block() -> int | None:
    try:
        sys.path.insert(0, "/Volumes/RAID0/repos/Alpha-Python/lib")
        from Alpha_Disk_Lib import AlphaDisk
    except Exception:
        return None

    try:
        with AlphaDisk(str(BOOT_IMAGE)) as disk:
            dev = disk.get_logical_device(0)
            ufd = dev.read_user_file_directory((1, 4))
            for entry in ufd.get_active_entries():
                if entry.filename == "AMOSL" and entry.extension == "INI":
                    return int(entry.first_block)
    except Exception:
        return None
    return None


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


def main() -> int:
    target_block = find_amosl_ini_start_block()
    if target_block is None:
        print("Could not locate AMOSL.INI start block")
        return 1
    target_lba = target_block + 1

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
    cpu, bus, led, acia = build_system(config)
    sasi = next(device for _, _, device in bus._devices if isinstance(device, SASIController))
    assert sasi.target is not None
    recording_target = RecordingTarget(sasi.target)
    sasi.target = recording_target

    print(f"Tracing native boot for AMOSL.INI target LBA {target_lba}")

    first_tx: dict[str, int] | None = None
    first_ini_read: DiskRead | None = None
    pc_snapshots: list[str] = []
    write_events: list[str] = []
    seen_pc_counts: dict[int, int] = {}

    orig_write = bus._write_byte_physical

    def wrapped_write(address: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if addr in WATCH_WRITE_ADDRS and len(write_events) < 80:
            desc = f"PC=${cpu.pc:06X} WRITE ${addr:06X} <- ${value & 0xFF:02X}"
            if addr in (JOBCUR, JCB_TCB_PTR):
                desc += (
                    f" JOBCUR=${read_long(bus, JOBCUR):08X}"
                    f" JCB+$20=${bus.read_word(JCB_CMD_FILE):04X}"
                    f" JCB+$38=${read_long(bus, JCB_TCB_PTR):08X}"
                )
            write_events.append(desc)
        orig_write(address, value)

    bus._write_byte_physical = wrapped_write

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx
        if first_tx is None:
            first_tx = {
                "port": port,
                "value": value,
                "pc": cpu.pc,
                "cycles": cpu.cycles,
                "reads": len(recording_target.read_calls),
                "last_lba": recording_target.read_calls[-1].lba if recording_target.read_calls else -1,
            }

    acia.tx_callback = tx_callback
    cpu.reset()

    instructions = 0
    while instructions < config.max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1

        if cpu.pc in WATCH_PCS:
            count = seen_pc_counts.get(cpu.pc, 0) + 1
            seen_pc_counts[cpu.pc] = count
            if count <= 12:
                pc_snapshots.append(
                    f"[{instructions}] PC=${cpu.pc:06X} D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} "
                    f"D6=${cpu.d[6]:08X} A0=${cpu.a[0]&0xFFFFFF:06X} "
                    f"A2=${cpu.a[2]&0xFFFFFF:06X} A5=${cpu.a[5]&0xFFFFFF:06X} "
                    f"A6=${cpu.a[6]&0xFFFFFF:06X} JOBCUR=${read_long(bus, JOBCUR):08X} "
                    f"JCB+$20=${bus.read_word(JCB_CMD_FILE):04X} "
                    f"JCB+$38=${read_long(bus, JCB_TCB_PTR):08X}"
                )

        if first_ini_read is None:
            for read in recording_target.read_calls[-4:]:
                if read.lba >= target_lba:
                    first_ini_read = read
                    break

        if first_tx is not None or first_ini_read is not None:
            break

    print("\nStop reason:")
    if first_ini_read is not None:
        print(
            f"  First AMOSL.INI read: LBA={first_ini_read.lba} count={first_ini_read.count} "
            f"pc=${cpu.pc:06X} cycles={cpu.cycles}"
        )
    if first_tx is not None:
        printable = chr(first_tx["value"]) if 0x20 <= first_tx["value"] < 0x7F else "."
        print(
            f"  First TX: port={first_tx['port']} value=${first_tx['value']:02X} ('{printable}') "
            f"pc=${first_tx['pc']:06X} cycles={first_tx['cycles']} "
            f"reads={first_tx['reads']} last_lba={first_tx['last_lba']}"
        )
    if first_ini_read is None and first_tx is None:
        print(f"  Max instructions/halt with pc=${cpu.pc:06X}")

    print(f"\nFinal state: pc=${cpu.pc:06X} cycles={cpu.cycles} leds={[f'{x:02X}' for x in led.history]}")
    print(
        f"JOBCUR=${read_long(bus, JOBCUR):08X} SYSBAS=${read_long(bus, SYSBAS):08X} "
        f"JCB+$20=${bus.read_word(JCB_CMD_FILE):04X} JCB+$38=${read_long(bus, JCB_TCB_PTR):08X}"
    )

    print("\nRecent disk reads:")
    for read in recording_target.read_calls[-20:]:
        marker = " <<< target" if read.lba >= target_lba else ""
        print(f"  LBA={read.lba} count={read.count} size={read.size}{marker}")

    if pc_snapshots:
        print("\nPC snapshots:")
        for line in pc_snapshots:
            print(f"  {line}")

    if write_events:
        print("\nWatched writes:")
        for line in write_events:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
