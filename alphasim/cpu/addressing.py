"""68010 addressing mode decoding and effective address calculation.

Supports all 14 addressing modes used by the 68000/68010 instruction set.
EA operations return values or addresses; the caller decides how to use them.

A7 (SP) always stays word-aligned: byte-size operations on A7 use ±2 not ±1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mc68010 import MC68010


# EA types returned by decode_ea
EA_DATA_REG = 0      # Value is in a data register
EA_ADDR_REG = 1      # Value is in an address register
EA_MEMORY = 2        # Value is a memory address
EA_IMMEDIATE = 3     # Value is an immediate constant


def _sign_extend_byte(v: int) -> int:
    if v & 0x80:
        return v | ~0xFF
    return v


def _sign_extend_word(v: int) -> int:
    if v & 0x8000:
        return v | ~0xFFFF
    return v


def _sp_increment(reg: int, size: int) -> int:
    """Return increment for post-increment / pre-decrement.
    A7 always uses at least 2 for byte operations."""
    if size == 1 and reg == 7:
        return 2
    return size


def decode_ea(cpu: MC68010, mode: int, reg: int, size: int) -> tuple[int, int]:
    """Decode effective address from mode and register fields.

    Args:
        cpu: CPU instance (for register reads and fetches).
        mode: 3-bit mode field (0-7).
        reg: 3-bit register field (0-7).
        size: Operand size in bytes (1, 2, or 4).

    Returns:
        (ea_type, value) where:
            EA_DATA_REG:  value = register number
            EA_ADDR_REG:  value = register number
            EA_MEMORY:    value = effective address
            EA_IMMEDIATE: value = immediate data
    """
    match mode:
        case 0:  # Dn — Data register direct
            return (EA_DATA_REG, reg)

        case 1:  # An — Address register direct
            return (EA_ADDR_REG, reg)

        case 2:  # (An) — Address register indirect
            return (EA_MEMORY, cpu.a[reg] & 0xFFFFFFFF)

        case 3:  # (An)+ — Post-increment
            addr = cpu.a[reg] & 0xFFFFFFFF
            cpu.a[reg] = (addr + _sp_increment(reg, size)) & 0xFFFFFFFF
            return (EA_MEMORY, addr)

        case 4:  # -(An) — Pre-decrement
            cpu.a[reg] = (cpu.a[reg] - _sp_increment(reg, size)) & 0xFFFFFFFF
            return (EA_MEMORY, cpu.a[reg] & 0xFFFFFFFF)

        case 5:  # d16(An) — Address register indirect with displacement
            disp = _sign_extend_word(cpu.fetch_word())
            return (EA_MEMORY, (cpu.a[reg] + disp) & 0xFFFFFFFF)

        case 6:  # d8(An,Xn) — Address register indirect with index
            ext = cpu.fetch_word()
            disp = _sign_extend_byte(ext & 0xFF)
            xn_reg = (ext >> 12) & 0x7
            xn_is_addr = bool(ext & 0x8000)
            xn_long = bool(ext & 0x0800)

            if xn_is_addr:
                xn_val = cpu.a[xn_reg]
            else:
                xn_val = cpu.d[xn_reg]

            if not xn_long:
                xn_val = _sign_extend_word(xn_val & 0xFFFF)

            return (EA_MEMORY, (cpu.a[reg] + disp + xn_val) & 0xFFFFFFFF)

        case 7:  # Special modes based on register field
            match reg:
                case 0:  # (xxx).W — Absolute short
                    addr = _sign_extend_word(cpu.fetch_word())
                    return (EA_MEMORY, addr & 0xFFFFFFFF)

                case 1:  # (xxx).L — Absolute long
                    addr = cpu.fetch_long()
                    return (EA_MEMORY, addr & 0xFFFFFFFF)

                case 2:  # d16(PC) — PC indirect with displacement
                    pc_val = cpu.pc  # PC points to extension word
                    disp = _sign_extend_word(cpu.fetch_word())
                    return (EA_MEMORY, (pc_val + disp) & 0xFFFFFFFF)

                case 3:  # d8(PC,Xn) — PC indirect with index
                    pc_val = cpu.pc  # PC points to extension word
                    ext = cpu.fetch_word()
                    disp = _sign_extend_byte(ext & 0xFF)
                    xn_reg = (ext >> 12) & 0x7
                    xn_is_addr = bool(ext & 0x8000)
                    xn_long = bool(ext & 0x0800)

                    if xn_is_addr:
                        xn_val = cpu.a[xn_reg]
                    else:
                        xn_val = cpu.d[xn_reg]

                    if not xn_long:
                        xn_val = _sign_extend_word(xn_val & 0xFFFF)

                    return (EA_MEMORY, (pc_val + disp + xn_val) & 0xFFFFFFFF)

                case 4:  # #imm — Immediate
                    if size == 1:
                        # Byte immediate: stored in low byte of extension word
                        val = cpu.fetch_word() & 0xFF
                    elif size == 2:
                        val = cpu.fetch_word()
                    else:  # size == 4
                        val = cpu.fetch_long()
                    return (EA_IMMEDIATE, val)

    raise ValueError(f"Invalid EA mode={mode} reg={reg}")


def read_ea(cpu: MC68010, ea_type: int, ea_value: int, size: int) -> int:
    """Read the value at the given effective address."""
    match ea_type:
        case 0:  # EA_DATA_REG
            return cpu.get_d(ea_value, size)
        case 1:  # EA_ADDR_REG
            if size == 4:
                return cpu.a[ea_value] & 0xFFFFFFFF
            if size == 2:
                return cpu.a[ea_value] & 0xFFFF
            return cpu.a[ea_value] & 0xFF
        case 2:  # EA_MEMORY
            if size == 1:
                return cpu.read_byte(ea_value)
            if size == 2:
                return cpu.read_word(ea_value)
            return cpu.read_long(ea_value)
        case 3:  # EA_IMMEDIATE
            return ea_value

    raise ValueError(f"Invalid EA type={ea_type}")


def write_ea(cpu: MC68010, ea_type: int, ea_value: int, size: int,
             data: int) -> None:
    """Write a value to the given effective address."""
    match ea_type:
        case 0:  # EA_DATA_REG
            cpu.set_d(ea_value, size, data)
        case 1:  # EA_ADDR_REG
            cpu.set_a(ea_value, data)
        case 2:  # EA_MEMORY
            if size == 1:
                cpu.write_byte(ea_value, data)
            elif size == 2:
                cpu.write_word(ea_value, data)
            else:
                cpu.write_long(ea_value, data)
        case 3:  # EA_IMMEDIATE
            raise ValueError("Cannot write to immediate value")
        case _:
            raise ValueError(f"Invalid EA type={ea_type}")
