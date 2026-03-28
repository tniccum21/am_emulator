"""Tests for MOVEC control-register semantics."""

from __future__ import annotations

from alphasim.bus.memory_bus import MemoryBus
from alphasim.cpu.mc68010 import MC68010
from alphasim.cpu.opcodes import build_opcode_table
from alphasim.devices.ram import RAM


def _build_cpu(cpu_model: str = "68010") -> tuple[MC68010, MemoryBus]:
    bus = MemoryBus()
    bus.set_ram(RAM(0x2000))
    cpu = MC68010(bus, cpu_model=cpu_model)
    cpu.opcode_table = build_opcode_table()
    cpu.a[7] = 0x1000
    cpu.ssp = cpu.a[7]
    return cpu, bus


def test_movec_vbr_to_address_register_succeeds() -> None:
    cpu, bus = _build_cpu()
    cpu.vbr = 0x00123456
    cpu.pc = 0x0100

    # MOVEC VBR,A6
    bus.write_word(0x0100, 0x4E7A)
    bus.write_word(0x0102, 0xE801)

    cycles = cpu.step()

    assert cycles == 12
    assert cpu.a[6] == 0x00123456
    assert cpu.pc == 0x0104
    assert cpu.a[7] == 0x1000


def test_movec_cacr_on_68010_raises_illegal_instruction() -> None:
    cpu, bus = _build_cpu()
    cpu.pc = 0x0100

    # Illegal-instruction vector -> $000200
    bus.write_long(0x0010, 0x00000200)

    # MOVEC CACR,D6
    bus.write_word(0x0100, 0x4E7A)
    bus.write_word(0x0102, 0x6002)

    cycles = cpu.step()

    assert cycles == 34
    assert cpu.pc == 0x000200
    assert cpu.a[7] == 0x0FFA
    assert bus.read_word(0x0FFA) == 0x2700
    assert bus.read_long(0x0FFC) == 0x00000104
    assert cpu.d[6] == 0


def test_movec_cacr_on_68020_preserves_bit9_only() -> None:
    cpu, bus = _build_cpu(cpu_model="68020")
    cpu.pc = 0x0100
    cpu.d[6] = 0x80000200

    # MOVEC D6,CACR
    bus.write_word(0x0100, 0x4E7B)
    bus.write_word(0x0102, 0x6002)
    # MOVEC CACR,D7
    bus.write_word(0x0104, 0x4E7A)
    bus.write_word(0x0106, 0x7002)

    assert cpu.step() == 12
    assert cpu.cacr == 0x00000200

    assert cpu.step() == 12
    assert cpu.d[7] == 0x00000200
