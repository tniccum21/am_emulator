"""Tests for native RTE compatibility quirks."""

from __future__ import annotations

from alphasim.bus.memory_bus import MemoryBus
from alphasim.cpu.exceptions import execute_rte
from alphasim.cpu.mc68010 import MC68010
from alphasim.cpu.opcodes import build_opcode_table
from alphasim.devices.ram import RAM


def _build_cpu() -> tuple[MC68010, MemoryBus]:
    bus = MemoryBus()
    bus.set_ram(RAM(0x4000))
    cpu = MC68010(bus, cpu_model="68020")
    cpu.opcode_table = build_opcode_table()
    cpu.use_68000_frames = True
    cpu.a[7] = 0x2000
    cpu.ssp = cpu.a[7]
    return cpu, bus


def test_native_vector26_helper_rte_preserves_supervisor_state() -> None:
    cpu, bus = _build_cpu()

    cpu.pc = 0x004EFE
    bus.write_word(0x2000, 0x0019)
    bus.write_long(0x2002, 0x00001C98)

    cycles = execute_rte(cpu)

    assert cycles == 20
    assert cpu.pc == 0x001C98
    assert cpu.sr == 0x2019
    assert cpu.a[7] == 0x2006


def test_rte_does_not_force_supervisor_for_unrelated_return() -> None:
    cpu, bus = _build_cpu()

    cpu.pc = 0x004EFE
    bus.write_word(0x2000, 0x0019)
    bus.write_long(0x2002, 0x00002000)

    execute_rte(cpu)

    assert cpu.pc == 0x002000
    assert cpu.sr == 0x0019
