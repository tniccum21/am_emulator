#!/usr/bin/env python3
"""Trace the native SYSMSG.USA path with optional ACIA TX IRQ gating."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from collections import deque

sys.path.insert(0, ".")

from alphasim.config import SystemConfig
from alphasim.devices.sasi import SASIController
from alphasim.main import build_system


REPO_ROOT = Path(__file__).resolve().parent
ROM_EVEN = REPO_ROOT / "roms" / "AM-178-01-B05.BIN"
ROM_ODD = REPO_ROOT / "roms" / "AM-178-00-B05.BIN"
BOOT_IMAGE = REPO_ROOT / "images" / "AMOS_1-3_Boot_OS.img"

DDT_ADDR = 0x7038
JOB_QUEUE = DDT_ADDR + 0x78
JOBCUR = 0x041C
SYSBAS = 0x0414
TARGET_PC = 0x003752
SYSMSG_LBA = 3326
AMOSL_INI_LBA = 3335
WATCH_PCS = {
    0x003752,
    0x00375E,
    0x003772,
    0x0037A2,
    0x0037B8,
    0x001C30,
    0x000D82,
    0x000DD8,
}
WATCH_OPS = {
    0xA052: "A052",
    0xA064: "A064",
    0xA0DC: "A0DC",
}


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def raw_write_word(orig_write, addr: int, value: int) -> None:
    addr &= ~1
    orig_write(addr, value & 0xFF)
    orig_write(addr + 1, (value >> 8) & 0xFF)


def raw_write_long(orig_write, addr: int, value: int) -> None:
    raw_write_word(orig_write, addr, (value >> 16) & 0xFFFF)
    raw_write_word(orig_write, addr + 2, value & 0xFFFF)


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


def make_system():
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
    return config, cpu, bus, led, acia, recording_target


def install_jobq_fix(bus, cpu):
    queue_fixed = False
    orig_write = bus._write_byte_physical

    def wrapped_write(address: int, value: int) -> None:
        nonlocal queue_fixed
        addr = address & 0xFFFFFF
        orig_write(address, value)
        if not queue_fixed and read_long(bus, JOB_QUEUE) == 0:
            raw_write_long(orig_write, JOB_QUEUE, DDT_ADDR)
            queue_fixed = True
            print(
                f"Forced JOBQ self-link at pc=${cpu.pc:06X} cycles={cpu.cycles} "
                f"JOBCUR=${read_long(bus, JOBCUR):08X}"
            )
        if queue_fixed and JOB_QUEUE <= addr <= JOB_QUEUE + 3 and read_long(bus, JOB_QUEUE) == 0:
            raw_write_long(orig_write, JOB_QUEUE, DDT_ADDR)
            print(f"Restored JOBQ self-link at pc=${cpu.pc:06X} cycles={cpu.cycles}")

    bus._write_byte_physical = wrapped_write


def gate_tx_irq_on_tdre(acia):
    original = acia.get_interrupt_level

    def gated(self) -> int:
        for port in range(3):
            if self._is_master_reset(port):
                continue
            if self._rdrf[port] and self._rx_irq_enabled(port):
                return 1
            if self._tx_irq_enabled(port) and self._tdre[port] and self._rx_cooldown[port] <= 0:
                return 1
        return 0

    acia.get_interrupt_level = MethodType(gated, acia)
    return original


def disable_control_echo(acia):
    original = acia._start_shift

    def filtered(self, port: int, byte_val: int, from_tdr: bool = False) -> None:
        self._tsr_active[port] = True
        self._tsr_countdown[port] = 8000
        if self._echo_enabled[port] and not from_tdr:
            if byte_val >= 0x20 or byte_val in (0x0A, 0x0D):
                self._echo_pending[port].append((16000, byte_val))

    acia._start_shift = MethodType(filtered, acia)
    return original


def dump_acia(acia, label: str) -> None:
    status = acia.read(0xFFFE20, 1)
    print(
        f"{label} ACIA0 CR=${acia._control[0]:02X} status=${status:02X} "
        f"RDRF={int(acia._rdrf[0])} RX=${acia._rx_data[0]:02X} TDRE={int(acia._tdre[0])} "
        f"TDR_FULL={int(acia._tdr_full[0])} TSR_ACTIVE={int(acia._tsr_active[0])} "
        f"TSR_CD={acia._tsr_countdown[0]} RX_CD={acia._rx_cooldown[0]} "
        f"ECHO={len(acia._echo_pending[0])}"
    )


def dump_a064_args(bus, a6: int) -> None:
    ptr = read_long(bus, a6)
    print(f"    A064 arg_ptr=${ptr:08X}")
    if 0 < ptr < 0x400000:
        words = " ".join(f"{bus.read_word(ptr + off):04X}" for off in range(0, 16, 2))
        print(f"    A064 arg_words={words}")


def run_case(
    gate_tx_irq: bool,
    suppress_one_irq: bool,
    clear_rdrf_at_target: bool,
    no_control_echo: bool,
    max_after_fix: int,
    absolute_limit: int,
) -> int:
    config, cpu, bus, led, acia, recording_target = make_system()
    install_jobq_fix(bus, cpu)

    if gate_tx_irq:
        gate_tx_irq_on_tdre(acia)
    if no_control_echo:
        disable_control_echo(acia)

    first_tx: tuple[int, int, int, int] | None = None
    suppressed = False
    saw_target = False
    target_hits = 0
    post_target_steps = 0
    restore_pending = False
    pc_counts: dict[int, int] = {}
    op_counts: dict[int, int] = {}
    acia_ops: deque[tuple[int, int, int, str, int, str, int, int, bool, int, int]] = deque(maxlen=64)
    acia_state_events: deque[tuple[int, int, int, str]] = deque(maxlen=64)

    orig_acia_read = acia.read
    orig_acia_write = acia.write
    orig_acia_tick = acia.tick

    def trace_read(address: int, size: int) -> int:
        value = orig_acia_read(address, size)
        result = acia._addr_to_port_reg(address)
        if result is not None:
            port, reg = result
            acia_ops.append(
                (
                    instructions,
                    cpu.cycles,
                    cpu.pc,
                    "R",
                    address,
                    reg,
                    value & 0xFF,
                    acia._control[port],
                    acia._rdrf[port],
                    acia._rx_data[port],
                    len(acia._echo_pending[port]),
                )
            )
        return value

    def trace_write(address: int, size: int, value: int) -> None:
        result = acia._addr_to_port_reg(address)
        if result is not None:
            port, reg = result
            acia_ops.append(
                (
                    instructions,
                    cpu.cycles,
                    cpu.pc,
                    "W",
                    address,
                    reg,
                    value & 0xFF,
                    acia._control[port],
                    acia._rdrf[port],
                    acia._rx_data[port],
                    len(acia._echo_pending[port]),
                )
            )
        orig_acia_write(address, size, value)

    def trace_tick(cycles: int) -> None:
        old_rdrf = acia._rdrf[0]
        old_echo = len(acia._echo_pending[0])
        old_rx = acia._rx_data[0]
        orig_acia_tick(cycles)
        new_rdrf = acia._rdrf[0]
        new_echo = len(acia._echo_pending[0])
        new_rx = acia._rx_data[0]
        if old_echo != new_echo:
            acia_state_events.append(
                (
                    instructions,
                    cpu.cycles,
                    cpu.pc,
                    f"echo {old_echo}->{new_echo} rx=${new_rx:02X} rdrf={int(new_rdrf)}",
                )
            )
        if old_rdrf != new_rdrf:
            acia_state_events.append(
                (
                    instructions,
                    cpu.cycles,
                    cpu.pc,
                    f"rdrf {int(old_rdrf)}->{int(new_rdrf)} rx ${old_rx:02X}->${new_rx:02X} echo={new_echo}",
                )
            )

    acia.read = trace_read
    acia.write = trace_write
    acia.tick = trace_tick

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx
        if first_tx is None:
            first_tx = (cpu.pc, cpu.cycles, port, value)

    acia.tx_callback = tx_callback
    cpu.reset()

    instructions = 0
    while instructions < config.max_instructions and instructions < absolute_limit and not cpu.halted:
        pc = cpu.pc
        op = bus.read_word(pc)
        last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None

        if saw_target and (pc in WATCH_PCS or op in WATCH_OPS):
            if pc in WATCH_PCS:
                count = pc_counts.get(pc, 0) + 1
                pc_counts[pc] = count
                if count <= 8:
                    print(
                        f"  Watch PC ${pc:06X} hit={count} op=${op:04X} "
                        f"D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} "
                        f"A1=${cpu.a[1]&0xFFFFFF:06X} A4=${cpu.a[4]&0xFFFFFF:06X} "
                        f"A6=${cpu.a[6]&0xFFFFFF:06X} last_lba={last_lba}"
                    )
            if op in WATCH_OPS:
                count = op_counts.get(op, 0) + 1
                op_counts[op] = count
                if count <= 8:
                    print(
                        f"  Watch {WATCH_OPS[op]} hit={count} pc=${pc:06X} "
                        f"D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} "
                        f"A1=${cpu.a[1]&0xFFFFFF:06X} A4=${cpu.a[4]&0xFFFFFF:06X} "
                        f"A6=${cpu.a[6]&0xFFFFFF:06X} last_lba={last_lba}"
                    )
                    if op == 0xA064:
                        dump_a064_args(bus, cpu.a[6] & 0xFFFFFF)

        if pc == TARGET_PC:
            target_hits += 1
            saw_target = True
            print(
                f"At ${TARGET_PC:06X} hit={target_hits} cycles={cpu.cycles} "
                f"JOBCUR=${read_long(bus, JOBCUR):08X} SYSBAS=${read_long(bus, SYSBAS):08X} "
                f"last_lba={last_lba}"
            )
            dump_acia(acia, "  ")
            print("  Recent ACIA ops:")
            for op_step, op_cycles, op_pc, direction, addr, reg, value, cr, rdrf, rx, echo in list(acia_ops)[-16:]:
                print(
                    f"    {direction} step={op_step} cyc={op_cycles} pc=${op_pc:06X} "
                    f"addr=${addr:06X} {reg}=${value:02X} CR=${cr:02X} "
                    f"RDRF={int(rdrf)} RX=${rx:02X} echo={echo}"
                )
            print("  Recent ACIA state changes:")
            for op_step, op_cycles, op_pc, detail in list(acia_state_events)[-16:]:
                print(f"    step={op_step} cyc={op_cycles} pc=${op_pc:06X} {detail}")
            if suppress_one_irq and not suppressed:
                old_cr = acia._control[0]
                acia._control[0] = old_cr & ~0x60
                print(f"  Suppressing one TX IRQ by CR ${old_cr:02X} -> ${acia._control[0]:02X}")
                suppressed = True
                restore_pending = True
            if clear_rdrf_at_target and acia._rdrf[0]:
                stale = acia._rx_data[0]
                acia._rdrf[0] = False
                acia._ovrn[0] = False
                print(f"  Cleared pending RX byte ${stale:02X} at target edge")

        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1

        if saw_target:
            post_target_steps += 1

        if suppress_one_irq and restore_pending and acia._control[0] != 0:
            # Restore TX IRQ enable once the flow has moved on from the edge.
            if cpu.pc not in (0x000D82, 0x000D86, 0x000D92, 0x000D9E, 0x000DAA):
                acia._control[0] |= 0x20
                restore_pending = False

        if saw_target and post_target_steps >= max_after_fix:
            break

        if last_lba is not None and last_lba >= AMOSL_INI_LBA:
            break

    last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None
    print(
        f"Final: pc=${cpu.pc:06X} cycles={cpu.cycles} last_lba={last_lba} "
        f"JOBCUR=${read_long(bus, JOBCUR):08X} SYSBAS=${read_long(bus, SYSBAS):08X} "
        f"leds={[f'{x:02X}' for x in led.history]}"
    )
    dump_acia(acia, "  ")
    print("  Recent disk reads:")
    for read in recording_target.read_calls[-12:]:
        print(f"    LBA={read.lba} count={read.count} size={read.size}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-tx-irq", action="store_true")
    parser.add_argument("--suppress-one-irq", action="store_true")
    parser.add_argument("--clear-rdrf-at-target", action="store_true")
    parser.add_argument("--no-control-echo", action="store_true")
    parser.add_argument("--max-after-fix", type=int, default=200_000)
    parser.add_argument("--absolute-limit", type=int, default=30_000_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_case(
        gate_tx_irq=args.gate_tx_irq,
        suppress_one_irq=args.suppress_one_irq,
        clear_rdrf_at_target=args.clear_rdrf_at_target,
        no_control_echo=args.no_control_echo,
        max_after_fix=args.max_after_fix,
        absolute_limit=args.absolute_limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
