"""Tests for loop-accelerator timing accounting."""

from __future__ import annotations

from alphasim.bus.memory_bus import MemoryBus
from alphasim.cpu.accelerators import LoopAccelerator
from alphasim.cpu.mc68010 import MC68010
from alphasim.cpu.opcodes import build_opcode_table
from alphasim.devices.ram import RAM


def _build_cpu() -> tuple[MC68010, MemoryBus]:
    bus = MemoryBus()
    bus.set_ram(RAM(0x2000))
    cpu = MC68010(bus)
    cpu.opcode_table = build_opcode_table()
    cpu.a[7] = 0x1000
    cpu.ssp = cpu.a[7]
    return cpu, bus


def test_step_includes_trace_hook_timing_credit() -> None:
    cpu, bus = _build_cpu()
    cpu.pc = 0x0100
    bus.write_word(0x0100, 0x4E71)  # NOP

    def hook(cpu_ref: MC68010) -> None:
        cpu_ref.add_timing_cycles(12)

    cpu.trace_hook = hook

    cycles = cpu.step()

    assert cycles == 16
    assert cpu.cycles == 16
    assert cpu.pc == 0x0102


class _FakeBus:
    def __init__(self, words: dict[int, int]) -> None:
        self._words = words

    def read_word(self, addr: int) -> int:
        return self._words[addr]


class _FakeCPU:
    def __init__(self) -> None:
        self.pc = 0
        self.d = [0] * 8
        self.timing_credit = 0

    def add_timing_cycles(self, cycles: int) -> None:
        self.timing_credit += cycles


def test_dbcc_accelerator_charges_skipped_cycles() -> None:
    accel = LoopAccelerator(_FakeBus({0x5000: 0x51C8}))
    cpu = _FakeCPU()
    cpu.pc = 0x5000
    cpu.d[0] = 5

    accel.hook(cpu)
    accel.hook(cpu)

    assert cpu.d[0] == 0
    assert cpu.timing_credit == 50
    assert accel.dbcc_accel_count == 1


def test_subq_bne_accelerator_charges_skipped_cycles() -> None:
    accel = LoopAccelerator(
        _FakeBus(
            {
                0x4000: 0x5380,  # SUBQ.L #1,D0
                0x4002: 0x66FC,  # BNE $4000
            }
        )
    )
    cpu = _FakeCPU()
    cpu.d[0] = 100

    for _ in range(12):
        cpu.pc = 0x4000
        accel.hook(cpu)
        cpu.pc = 0x4002
        accel.hook(cpu)

    assert cpu.d[0] == 1
    assert cpu.timing_credit == 99 * 14
    assert accel.subq_accel_count == 1


def test_subq_bne_accelerator_applies_to_low_memory_monitor_loops() -> None:
    accel = LoopAccelerator(
        _FakeBus(
            {
                0x3430: 0x5387,  # SUBQ.L #1,D7
                0x3432: 0x66FC,  # BNE $3430
            }
        )
    )
    cpu = _FakeCPU()
    cpu.d[7] = 64

    for _ in range(12):
        cpu.pc = 0x3430
        accel.hook(cpu)
        cpu.pc = 0x3432
        accel.hook(cpu)

    assert cpu.d[7] == 1
    assert cpu.timing_credit == 63 * 14
    assert accel.subq_accel_count == 1
