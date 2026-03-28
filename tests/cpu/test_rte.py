"""Tests for native exception-frame and RTE compatibility quirks."""

from __future__ import annotations

from alphasim.cpu.exceptions import execute_exception
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


def test_vector8_uses_standard_68000_short_frame_layout() -> None:
    cpu, bus = _build_cpu()

    cpu.pc = 0x001290
    cpu.sr = 0x0000
    cpu.usp = 0
    cpu.a[7] = 0
    cpu.ssp = 0x2000

    execute_exception(cpu, 8, pc_override=0x001290)

    assert cpu.pc == 0
    assert cpu.a[7] == 0x1FFA
    assert bus.read_word(0x1FFA) == 0x0000
    assert bus.read_long(0x1FFC) == 0x00001290

    # The native $000AFC privilege helper saves A1 with MOVE.L A1,-(A7)
    # and then reads the faulting PC via MOVEA.L 6(A7),A1.
    cpu.a[7] = 0x1FF6
    assert bus.read_long(cpu.a[7] + 6) == 0x00001290


def test_rte_does_not_force_supervisor_for_unrelated_return() -> None:
    cpu, bus = _build_cpu()

    cpu.pc = 0x004EFE
    bus.write_word(0x2000, 0x0019)
    bus.write_long(0x2002, 0x00002000)

    execute_rte(cpu)

    assert cpu.pc == 0x002000
    assert cpu.sr == 0x0019


def test_native_vector41_helper_rte_preserves_supervisor_state() -> None:
    cpu, bus = _build_cpu()

    cpu.pc = 0x003DDC
    bus.write_word(0x2000, 0x0001)
    bus.write_long(0x2002, 0x0000128C)

    execute_rte(cpu)

    assert cpu.pc == 0x00128C
    assert cpu.sr == 0x2001
