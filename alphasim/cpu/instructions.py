"""68010 instruction implementations.

Each handler receives (cpu, opword) and returns a cycle count.
Instructions are grouped by function and registered in the opcode table.

Flag-setting conventions:
    size_mask: {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}
    msb_mask:  {1: 0x80, 2: 0x8000, 4: 0x80000000}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mc68010 import MC68010

from .mc68010 import (
    SR_CARRY, SR_OVERFLOW, SR_ZERO, SR_NEGATIVE, SR_EXTEND, CCR_MASK,
    SR_SUPER, SR_IPL_MASK,
)
from .addressing import (
    decode_ea, read_ea, write_ea,
    EA_DATA_REG, EA_ADDR_REG, EA_MEMORY, EA_IMMEDIATE,
)
from .exceptions import execute_exception, execute_rte

SIZE_MASK = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}
MSB_MASK = {1: 0x80, 2: 0x8000, 4: 0x80000000}


def _sign_extend(value: int, size: int) -> int:
    """Sign-extend a value of the given size to 32 bits (Python int)."""
    msb = MSB_MASK[size]
    mask = SIZE_MASK[size]
    value &= mask
    if value & msb:
        return value - (mask + 1)
    return value


def _set_logic_flags(cpu: MC68010, result: int, size: int) -> None:
    """Set N, Z, clear V and C (used by MOVE, AND, OR, EOR, NOT, etc.)."""
    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    result &= mask
    cpu.set_flag(SR_NEGATIVE, bool(result & msb))
    cpu.set_flag(SR_ZERO, result == 0)
    cpu.set_flag(SR_OVERFLOW, False)
    cpu.set_flag(SR_CARRY, False)


def _set_add_flags(cpu: MC68010, src: int, dst: int, result: int,
                   size: int) -> None:
    """Set flags for ADD/ADDI/ADDQ operations."""
    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    src &= mask
    dst &= mask
    res = result & mask

    cpu.set_flag(SR_NEGATIVE, bool(res & msb))
    cpu.set_flag(SR_ZERO, res == 0)

    # Overflow: both operands same sign, result different sign
    sm = bool(src & msb)
    dm = bool(dst & msb)
    rm = bool(res & msb)
    cpu.set_flag(SR_OVERFLOW, (sm == dm) and (rm != sm))

    # Carry: unsigned overflow
    cpu.set_flag(SR_CARRY, result > mask)
    cpu.set_flag(SR_EXTEND, result > mask)


def _set_sub_flags(cpu: MC68010, src: int, dst: int, result: int,
                   size: int) -> None:
    """Set flags for SUB/SUBI/SUBQ operations."""
    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    src &= mask
    dst &= mask
    res = result & mask

    cpu.set_flag(SR_NEGATIVE, bool(res & msb))
    cpu.set_flag(SR_ZERO, res == 0)

    # Overflow: operands different signs, result sign differs from dst
    sm = bool(src & msb)
    dm = bool(dst & msb)
    rm = bool(res & msb)
    cpu.set_flag(SR_OVERFLOW, (sm != dm) and (rm != dm))

    # Borrow
    borrow = (src & mask) > (dst & mask)
    cpu.set_flag(SR_CARRY, borrow)
    cpu.set_flag(SR_EXTEND, borrow)


def _set_cmp_flags(cpu: MC68010, src: int, dst: int, result: int,
                   size: int) -> None:
    """Set flags for CMP/CMPI/CMPA (same as SUB but no X flag)."""
    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    src &= mask
    dst &= mask
    res = result & mask

    cpu.set_flag(SR_NEGATIVE, bool(res & msb))
    cpu.set_flag(SR_ZERO, res == 0)

    sm = bool(src & msb)
    dm = bool(dst & msb)
    rm = bool(res & msb)
    cpu.set_flag(SR_OVERFLOW, (sm != dm) and (rm != dm))

    borrow = (src & mask) > (dst & mask)
    cpu.set_flag(SR_CARRY, borrow)


# ═══════════════════════════════════════════════════════════════════
#  MOVE  (opcodes $1xxx, $2xxx, $3xxx)
# ═══════════════════════════════════════════════════════════════════

def op_move(cpu: MC68010, opword: int) -> int:
    """MOVE <ea>,<ea> — all sizes."""
    # Decode size from bits 13-12
    size_code = (opword >> 12) & 0x3
    size_map = {1: 1, 3: 2, 2: 4}  # 01=byte, 11=word, 10=long
    size = size_map.get(size_code, 2)

    # Source EA: mode=bits 5-3, reg=bits 2-0
    src_mode = (opword >> 3) & 0x7
    src_reg = opword & 0x7
    src_type, src_val = decode_ea(cpu, src_mode, src_reg, size)
    data = read_ea(cpu, src_type, src_val, size)

    # Destination EA: reg=bits 11-9, mode=bits 8-6 (reversed encoding!)
    dst_reg = (opword >> 9) & 0x7
    dst_mode = (opword >> 6) & 0x7
    dst_type, dst_val = decode_ea(cpu, dst_mode, dst_reg, size)

    write_ea(cpu, dst_type, dst_val, size, data)
    _set_logic_flags(cpu, data, size)

    return 4  # base timing


def op_movea(cpu: MC68010, opword: int) -> int:
    """MOVEA <ea>,An — word or long, no flag changes."""
    size_code = (opword >> 12) & 0x3
    size = 4 if size_code == 2 else 2

    src_mode = (opword >> 3) & 0x7
    src_reg = opword & 0x7
    src_type, src_val = decode_ea(cpu, src_mode, src_reg, size)
    data = read_ea(cpu, src_type, src_val, size)

    dst_reg = (opword >> 9) & 0x7

    # Sign-extend word to long for MOVEA.W
    if size == 2:
        data = _sign_extend(data, 2) & 0xFFFFFFFF

    cpu.set_a(dst_reg, data)
    return 4


# ═══════════════════════════════════════════════════════════════════
#  MOVEQ  (opcode $7xxx)
# ═══════════════════════════════════════════════════════════════════

def op_moveq(cpu: MC68010, opword: int) -> int:
    """MOVEQ #imm8,Dn — sign-extend byte to long."""
    reg = (opword >> 9) & 0x7
    data = opword & 0xFF
    if data & 0x80:
        data |= 0xFFFFFF00
    cpu.d[reg] = data & 0xFFFFFFFF
    _set_logic_flags(cpu, data, 4)
    return 4


# ═══════════════════════════════════════════════════════════════════
#  LEA / PEA
# ═══════════════════════════════════════════════════════════════════

def op_lea(cpu: MC68010, opword: int) -> int:
    """LEA <ea>,An — load effective address."""
    dst_reg = (opword >> 9) & 0x7
    src_mode = (opword >> 3) & 0x7
    src_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, src_mode, src_reg, 4)
    # LEA always uses the address, not the value at the address
    cpu.set_a(dst_reg, ea_val)
    return 4


def op_pea(cpu: MC68010, opword: int) -> int:
    """PEA <ea> — push effective address onto stack."""
    src_mode = (opword >> 3) & 0x7
    src_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, src_mode, src_reg, 4)
    cpu.push_long(ea_val)
    return 12


# ═══════════════════════════════════════════════════════════════════
#  CLR / NEG / NEGX / NOT / TST / EXT / SWAP
# ═══════════════════════════════════════════════════════════════════

def op_clr(cpu: MC68010, opword: int) -> int:
    """CLR <ea> — clear to zero."""
    size = 1 << ((opword >> 6) & 0x3)  # 00=1, 01=2, 10=4
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    write_ea(cpu, ea_type, ea_val, size, 0)
    cpu.set_flag(SR_NEGATIVE, False)
    cpu.set_flag(SR_ZERO, True)
    cpu.set_flag(SR_OVERFLOW, False)
    cpu.set_flag(SR_CARRY, False)
    return 4 if ea_type == EA_DATA_REG else 8


def op_neg(cpu: MC68010, opword: int) -> int:
    """NEG <ea> — negate (0 - ea)."""
    size = 1 << ((opword >> 6) & 0x3)
    mask = SIZE_MASK[size]
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    val = read_ea(cpu, ea_type, ea_val, size)
    result = (0 - val) & mask
    write_ea(cpu, ea_type, ea_val, size, result)

    _set_sub_flags(cpu, val, 0, (0 - val), size)
    # NEG: Z is set if result is zero, X and C are cleared if result is zero
    if result == 0:
        cpu.set_flag(SR_CARRY, False)
        cpu.set_flag(SR_EXTEND, False)
    return 4 if ea_type == EA_DATA_REG else 8


def op_not(cpu: MC68010, opword: int) -> int:
    """NOT <ea> — ones complement."""
    size = 1 << ((opword >> 6) & 0x3)
    mask = SIZE_MASK[size]
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    val = read_ea(cpu, ea_type, ea_val, size)
    result = (~val) & mask
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_logic_flags(cpu, result, size)
    return 4 if ea_type == EA_DATA_REG else 8


def op_tst(cpu: MC68010, opword: int) -> int:
    """TST <ea> — test operand, set N/Z, clear V/C."""
    size = 1 << ((opword >> 6) & 0x3)
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    val = read_ea(cpu, ea_type, ea_val, size)
    _set_logic_flags(cpu, val, size)
    return 4


def op_ext(cpu: MC68010, opword: int) -> int:
    """EXT Dn — sign-extend byte→word or word→long."""
    reg = opword & 0x7
    mode = (opword >> 6) & 0x7
    if mode == 2:  # EXT.W — byte to word
        val = cpu.d[reg] & 0xFF
        if val & 0x80:
            val |= 0xFF00
        cpu.d[reg] = (cpu.d[reg] & 0xFFFF0000) | (val & 0xFFFF)
        _set_logic_flags(cpu, val, 2)
    elif mode == 3:  # EXT.L — word to long
        val = cpu.d[reg] & 0xFFFF
        if val & 0x8000:
            val |= 0xFFFF0000
        cpu.d[reg] = val & 0xFFFFFFFF
        _set_logic_flags(cpu, val, 4)
    return 4


def op_swap(cpu: MC68010, opword: int) -> int:
    """SWAP Dn — swap high and low words."""
    reg = opword & 0x7
    val = cpu.d[reg] & 0xFFFFFFFF
    result = ((val >> 16) | (val << 16)) & 0xFFFFFFFF
    cpu.d[reg] = result
    _set_logic_flags(cpu, result, 4)
    return 4


# ═══════════════════════════════════════════════════════════════════
#  ADD / ADDA / ADDI / ADDQ / ADDX
# ═══════════════════════════════════════════════════════════════════

def op_add(cpu: MC68010, opword: int) -> int:
    """ADD <ea>,Dn  or  ADD Dn,<ea>."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7

    # Decode direction and size from opmode
    if opmode <= 2:
        # <ea> + Dn → Dn
        size = 1 << opmode  # 0=1, 1=2, 2=4
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = read_ea(cpu, ea_type, ea_val, size)
        dst = cpu.get_d(reg, size)
        result = src + dst
        cpu.set_d(reg, size, result)
        _set_add_flags(cpu, src, dst, result, size)
    else:
        # Dn + <ea> → <ea>
        size = 1 << (opmode - 4)  # 4=1, 5=2, 6=4
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = cpu.get_d(reg, size)
        dst = read_ea(cpu, ea_type, ea_val, size)
        result = src + dst
        write_ea(cpu, ea_type, ea_val, size, result)
        _set_add_flags(cpu, src, dst, result, size)

    return 4 if ea_type == EA_DATA_REG else 8


def op_adda(cpu: MC68010, opword: int) -> int:
    """ADDA <ea>,An — add to address register (no flags)."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    size = 2 if opmode == 3 else 4  # 3=word, 7=long

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    src = read_ea(cpu, ea_type, ea_val, size)

    if size == 2:
        src = _sign_extend(src, 2)

    cpu.a[reg] = (cpu.a[reg] + src) & 0xFFFFFFFF
    return 8


def op_addi(cpu: MC68010, opword: int) -> int:
    """ADDI #imm,<ea> — add immediate."""
    size = 1 << ((opword >> 6) & 0x3)
    if size == 1:
        imm = cpu.fetch_word() & 0xFF
    elif size == 2:
        imm = cpu.fetch_word()
    else:
        imm = cpu.fetch_long()

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = imm + dst
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_add_flags(cpu, imm, dst, result, size)
    return 8 if ea_type == EA_DATA_REG else 12


def op_addq(cpu: MC68010, opword: int) -> int:
    """ADDQ #3bit,<ea> — add quick (1-8)."""
    data = (opword >> 9) & 0x7
    if data == 0:
        data = 8
    size = 1 << ((opword >> 6) & 0x3)

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)

    if ea_type == EA_ADDR_REG:
        # ADDQ to An — full 32-bit, no flags
        cpu.a[ea_val] = (cpu.a[ea_val] + data) & 0xFFFFFFFF
        return 8

    dst = read_ea(cpu, ea_type, ea_val, size)
    result = dst + data
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_add_flags(cpu, data, dst, result, size)
    return 4 if ea_type == EA_DATA_REG else 8


def op_addx(cpu: MC68010, opword: int) -> int:
    """ADDX Dy,Dx or ADDX -(Ay),-(Ax) — add with extend."""
    reg_x = (opword >> 9) & 0x7
    reg_y = opword & 0x7
    size = 1 << ((opword >> 6) & 0x3)
    rm = (opword >> 3) & 0x1

    x_flag = 1 if cpu.get_flag(SR_EXTEND) else 0

    if rm:
        # Memory to memory: -(Ay),-(Ax)
        inc = size if not (reg_y == 7 and size == 1) else 2
        cpu.a[reg_y] = (cpu.a[reg_y] - inc) & 0xFFFFFFFF
        if size == 1:
            src = cpu.read_byte(cpu.a[reg_y])
        elif size == 2:
            src = cpu.read_word(cpu.a[reg_y])
        else:
            src = cpu.read_long(cpu.a[reg_y])

        inc_x = size if not (reg_x == 7 and size == 1) else 2
        cpu.a[reg_x] = (cpu.a[reg_x] - inc_x) & 0xFFFFFFFF
        if size == 1:
            dst = cpu.read_byte(cpu.a[reg_x])
        elif size == 2:
            dst = cpu.read_word(cpu.a[reg_x])
        else:
            dst = cpu.read_long(cpu.a[reg_x])

        result = src + dst + x_flag
        if size == 1:
            cpu.write_byte(cpu.a[reg_x], result)
        elif size == 2:
            cpu.write_word(cpu.a[reg_x], result)
        else:
            cpu.write_long(cpu.a[reg_x], result)
    else:
        # Register to register: Dy,Dx
        src = cpu.get_d(reg_y, size)
        dst = cpu.get_d(reg_x, size)
        result = src + dst + x_flag
        cpu.set_d(reg_x, size, result)

    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    res = result & mask
    src_m = src & mask
    dst_m = dst & mask

    cpu.set_flag(SR_NEGATIVE, bool(res & msb))
    # ADDX: Z is cleared if result is non-zero, unchanged if zero
    if res != 0:
        cpu.set_flag(SR_ZERO, False)
    sm = bool(src_m & msb)
    dm = bool(dst_m & msb)
    rm_flag = bool(res & msb)
    cpu.set_flag(SR_OVERFLOW, (sm == dm) and (rm_flag != sm))
    cpu.set_flag(SR_CARRY, result > mask)
    cpu.set_flag(SR_EXTEND, result > mask)

    return 4 if not rm else 18


# ═══════════════════════════════════════════════════════════════════
#  SUB / SUBA / SUBI / SUBQ / SUBX
# ═══════════════════════════════════════════════════════════════════

def op_sub(cpu: MC68010, opword: int) -> int:
    """SUB <ea>,Dn  or  SUB Dn,<ea>."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7

    if opmode <= 2:
        size = 1 << opmode
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = read_ea(cpu, ea_type, ea_val, size)
        dst = cpu.get_d(reg, size)
        result = dst - src
        cpu.set_d(reg, size, result)
        _set_sub_flags(cpu, src, dst, result, size)
    else:
        size = 1 << (opmode - 4)
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = cpu.get_d(reg, size)
        dst = read_ea(cpu, ea_type, ea_val, size)
        result = dst - src
        write_ea(cpu, ea_type, ea_val, size, result)
        _set_sub_flags(cpu, src, dst, result, size)

    return 4 if ea_type == EA_DATA_REG else 8


def op_suba(cpu: MC68010, opword: int) -> int:
    """SUBA <ea>,An — subtract from address register (no flags)."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    size = 2 if opmode == 3 else 4

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    src = read_ea(cpu, ea_type, ea_val, size)

    if size == 2:
        src = _sign_extend(src, 2)

    cpu.a[reg] = (cpu.a[reg] - src) & 0xFFFFFFFF
    return 8


def op_subi(cpu: MC68010, opword: int) -> int:
    """SUBI #imm,<ea> — subtract immediate."""
    size = 1 << ((opword >> 6) & 0x3)
    if size == 1:
        imm = cpu.fetch_word() & 0xFF
    elif size == 2:
        imm = cpu.fetch_word()
    else:
        imm = cpu.fetch_long()

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = dst - imm
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_sub_flags(cpu, imm, dst, result, size)
    return 8 if ea_type == EA_DATA_REG else 12


def op_subq(cpu: MC68010, opword: int) -> int:
    """SUBQ #3bit,<ea> — subtract quick (1-8)."""
    data = (opword >> 9) & 0x7
    if data == 0:
        data = 8
    size = 1 << ((opword >> 6) & 0x3)

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)

    if ea_type == EA_ADDR_REG:
        cpu.a[ea_val] = (cpu.a[ea_val] - data) & 0xFFFFFFFF
        return 8

    dst = read_ea(cpu, ea_type, ea_val, size)
    result = dst - data
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_sub_flags(cpu, data, dst, result, size)
    return 4 if ea_type == EA_DATA_REG else 8


def op_subx(cpu: MC68010, opword: int) -> int:
    """SUBX Dy,Dx or SUBX -(Ay),-(Ax) — subtract with extend."""
    reg_x = (opword >> 9) & 0x7
    reg_y = opword & 0x7
    size = 1 << ((opword >> 6) & 0x3)
    rm = (opword >> 3) & 0x1

    x_flag = 1 if cpu.get_flag(SR_EXTEND) else 0

    if rm:
        inc = size if not (reg_y == 7 and size == 1) else 2
        cpu.a[reg_y] = (cpu.a[reg_y] - inc) & 0xFFFFFFFF
        if size == 1:
            src = cpu.read_byte(cpu.a[reg_y])
        elif size == 2:
            src = cpu.read_word(cpu.a[reg_y])
        else:
            src = cpu.read_long(cpu.a[reg_y])

        inc_x = size if not (reg_x == 7 and size == 1) else 2
        cpu.a[reg_x] = (cpu.a[reg_x] - inc_x) & 0xFFFFFFFF
        if size == 1:
            dst = cpu.read_byte(cpu.a[reg_x])
        elif size == 2:
            dst = cpu.read_word(cpu.a[reg_x])
        else:
            dst = cpu.read_long(cpu.a[reg_x])

        result = dst - src - x_flag
        if size == 1:
            cpu.write_byte(cpu.a[reg_x], result)
        elif size == 2:
            cpu.write_word(cpu.a[reg_x], result)
        else:
            cpu.write_long(cpu.a[reg_x], result)
    else:
        src = cpu.get_d(reg_y, size)
        dst = cpu.get_d(reg_x, size)
        result = dst - src - x_flag
        cpu.set_d(reg_x, size, result)

    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    res = result & mask
    src_m = src & mask
    dst_m = dst & mask

    cpu.set_flag(SR_NEGATIVE, bool(res & msb))
    if res != 0:
        cpu.set_flag(SR_ZERO, False)
    sm = bool(src_m & msb)
    dm = bool(dst_m & msb)
    rm_flag = bool(res & msb)
    cpu.set_flag(SR_OVERFLOW, (sm != dm) and (rm_flag != dm))
    borrow = (src + x_flag) > (dst_m)
    cpu.set_flag(SR_CARRY, borrow)
    cpu.set_flag(SR_EXTEND, borrow)

    return 4 if not rm else 18


# ═══════════════════════════════════════════════════════════════════
#  AND / ANDI / OR / ORI / EOR / EORI
# ═══════════════════════════════════════════════════════════════════

def op_and(cpu: MC68010, opword: int) -> int:
    """AND <ea>,Dn  or  AND Dn,<ea>."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7

    if opmode <= 2:
        size = 1 << opmode
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = read_ea(cpu, ea_type, ea_val, size)
        dst = cpu.get_d(reg, size)
        result = src & dst
        cpu.set_d(reg, size, result)
    else:
        size = 1 << (opmode - 4)
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = cpu.get_d(reg, size)
        dst = read_ea(cpu, ea_type, ea_val, size)
        result = src & dst
        write_ea(cpu, ea_type, ea_val, size, result)

    _set_logic_flags(cpu, result, size)
    return 4 if ea_type == EA_DATA_REG else 8


def op_andi(cpu: MC68010, opword: int) -> int:
    """ANDI #imm,<ea>."""
    size = 1 << ((opword >> 6) & 0x3)
    if size == 1:
        imm = cpu.fetch_word() & 0xFF
    elif size == 2:
        imm = cpu.fetch_word()
    else:
        imm = cpu.fetch_long()

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = imm & dst
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_logic_flags(cpu, result, size)
    return 8 if ea_type == EA_DATA_REG else 12


def op_or(cpu: MC68010, opword: int) -> int:
    """OR <ea>,Dn  or  OR Dn,<ea>."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7

    if opmode <= 2:
        size = 1 << opmode
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = read_ea(cpu, ea_type, ea_val, size)
        dst = cpu.get_d(reg, size)
        result = src | dst
        cpu.set_d(reg, size, result)
    else:
        size = 1 << (opmode - 4)
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
        src = cpu.get_d(reg, size)
        dst = read_ea(cpu, ea_type, ea_val, size)
        result = src | dst
        write_ea(cpu, ea_type, ea_val, size, result)

    _set_logic_flags(cpu, result, size)
    return 4 if ea_type == EA_DATA_REG else 8


def op_ori(cpu: MC68010, opword: int) -> int:
    """ORI #imm,<ea>."""
    size = 1 << ((opword >> 6) & 0x3)
    if size == 1:
        imm = cpu.fetch_word() & 0xFF
    elif size == 2:
        imm = cpu.fetch_word()
    else:
        imm = cpu.fetch_long()

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = imm | dst
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_logic_flags(cpu, result, size)
    return 8 if ea_type == EA_DATA_REG else 12


def op_eor(cpu: MC68010, opword: int) -> int:
    """EOR Dn,<ea>."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    size = 1 << (opmode - 4)  # opmode 4=byte, 5=word, 6=long

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    src = cpu.get_d(reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = src ^ dst
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_logic_flags(cpu, result, size)
    return 4 if ea_type == EA_DATA_REG else 8


def op_eori(cpu: MC68010, opword: int) -> int:
    """EORI #imm,<ea>."""
    size = 1 << ((opword >> 6) & 0x3)
    if size == 1:
        imm = cpu.fetch_word() & 0xFF
    elif size == 2:
        imm = cpu.fetch_word()
    else:
        imm = cpu.fetch_long()

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = imm ^ dst
    write_ea(cpu, ea_type, ea_val, size, result)
    _set_logic_flags(cpu, result, size)
    return 8 if ea_type == EA_DATA_REG else 12


# ═══════════════════════════════════════════════════════════════════
#  CMP / CMPA / CMPI / CMPM
# ═══════════════════════════════════════════════════════════════════

def op_cmp(cpu: MC68010, opword: int) -> int:
    """CMP <ea>,Dn — compare (Dn - ea), set flags only."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    size = 1 << opmode  # 0=byte, 1=word, 2=long

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    src = read_ea(cpu, ea_type, ea_val, size)
    dst = cpu.get_d(reg, size)
    result = dst - src
    _set_cmp_flags(cpu, src, dst, result, size)
    return 4


def op_cmpa(cpu: MC68010, opword: int) -> int:
    """CMPA <ea>,An — compare address register."""
    reg = (opword >> 9) & 0x7
    opmode = (opword >> 6) & 0x7
    size = 2 if opmode == 3 else 4

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    src = read_ea(cpu, ea_type, ea_val, size)

    if size == 2:
        src = _sign_extend(src, 2) & 0xFFFFFFFF

    dst = cpu.a[reg] & 0xFFFFFFFF
    result = dst - src
    _set_cmp_flags(cpu, src, dst, result, 4)
    return 6


def op_cmpi(cpu: MC68010, opword: int) -> int:
    """CMPI #imm,<ea> — compare immediate."""
    size = 1 << ((opword >> 6) & 0x3)
    if size == 1:
        imm = cpu.fetch_word() & 0xFF
    elif size == 2:
        imm = cpu.fetch_word()
    else:
        imm = cpu.fetch_long()

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
    dst = read_ea(cpu, ea_type, ea_val, size)
    result = dst - imm
    _set_cmp_flags(cpu, imm, dst, result, size)
    return 8 if ea_type == EA_DATA_REG else 8


def op_cmpm(cpu: MC68010, opword: int) -> int:
    """CMPM (Ay)+,(Ax)+ — compare memory with post-increment."""
    reg_x = (opword >> 9) & 0x7
    reg_y = opword & 0x7
    size = 1 << ((opword >> 6) & 0x3)

    inc_y = size if not (reg_y == 7 and size == 1) else 2
    if size == 1:
        src = cpu.read_byte(cpu.a[reg_y])
    elif size == 2:
        src = cpu.read_word(cpu.a[reg_y])
    else:
        src = cpu.read_long(cpu.a[reg_y])
    cpu.a[reg_y] = (cpu.a[reg_y] + inc_y) & 0xFFFFFFFF

    inc_x = size if not (reg_x == 7 and size == 1) else 2
    if size == 1:
        dst = cpu.read_byte(cpu.a[reg_x])
    elif size == 2:
        dst = cpu.read_word(cpu.a[reg_x])
    else:
        dst = cpu.read_long(cpu.a[reg_x])
    cpu.a[reg_x] = (cpu.a[reg_x] + inc_x) & 0xFFFFFFFF

    result = dst - src
    _set_cmp_flags(cpu, src, dst, result, size)
    return 12


# ═══════════════════════════════════════════════════════════════════
#  MULU / MULS / DIVU / DIVS
# ═══════════════════════════════════════════════════════════════════

def op_mulu(cpu: MC68010, opword: int) -> int:
    """MULU <ea>,Dn — unsigned word multiply → long result."""
    reg = (opword >> 9) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    src = read_ea(cpu, ea_type, ea_val, 2) & 0xFFFF
    dst = cpu.d[reg] & 0xFFFF
    result = src * dst
    cpu.d[reg] = result & 0xFFFFFFFF
    _set_logic_flags(cpu, result, 4)
    return 70


def op_muls(cpu: MC68010, opword: int) -> int:
    """MULS <ea>,Dn — signed word multiply → long result."""
    reg = (opword >> 9) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    src = _sign_extend(read_ea(cpu, ea_type, ea_val, 2), 2)
    dst = _sign_extend(cpu.d[reg] & 0xFFFF, 2)
    result = (src * dst) & 0xFFFFFFFF
    cpu.d[reg] = result
    _set_logic_flags(cpu, result, 4)
    return 70


def op_divu(cpu: MC68010, opword: int) -> int:
    """DIVU <ea>,Dn — unsigned 32/16 → 16q:16r."""
    reg = (opword >> 9) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    divisor = read_ea(cpu, ea_type, ea_val, 2) & 0xFFFF

    if divisor == 0:
        execute_exception(cpu, 5)  # zero divide
        return 38

    dividend = cpu.d[reg] & 0xFFFFFFFF
    quotient = dividend // divisor
    remainder = dividend % divisor

    if quotient > 0xFFFF:
        cpu.set_flag(SR_OVERFLOW, True)
        # Register unchanged on overflow
        return 140

    cpu.d[reg] = ((remainder & 0xFFFF) << 16) | (quotient & 0xFFFF)
    _set_logic_flags(cpu, quotient, 2)
    cpu.set_flag(SR_OVERFLOW, False)
    return 140


def op_divs(cpu: MC68010, opword: int) -> int:
    """DIVS <ea>,Dn — signed 32/16 → 16q:16r."""
    reg = (opword >> 9) & 0x7
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    divisor = _sign_extend(read_ea(cpu, ea_type, ea_val, 2), 2)

    if divisor == 0:
        execute_exception(cpu, 5)  # zero divide
        return 158

    dividend = _sign_extend(cpu.d[reg] & 0xFFFFFFFF, 4)
    quotient = int(dividend / divisor)  # truncate toward zero
    remainder = dividend - (quotient * divisor)

    if quotient > 32767 or quotient < -32768:
        cpu.set_flag(SR_OVERFLOW, True)
        return 158

    cpu.d[reg] = ((remainder & 0xFFFF) << 16) | (quotient & 0xFFFF)
    _set_logic_flags(cpu, quotient & 0xFFFF, 2)
    cpu.set_flag(SR_OVERFLOW, False)
    return 158


# ═══════════════════════════════════════════════════════════════════
#  Bit operations: BTST / BSET / BCLR / BCHG
# ═══════════════════════════════════════════════════════════════════

def _bit_op(cpu: MC68010, opword: int, op_type: int, dynamic: bool) -> int:
    """Common bit operation handler.
    op_type: 0=BTST, 1=BCHG, 2=BCLR, 3=BSET
    dynamic: True = bit# from Dn, False = bit# from immediate
    """
    if dynamic:
        bit_num_reg = (opword >> 9) & 0x7
        bit_num = cpu.d[bit_num_reg] & 0xFFFFFFFF
    else:
        bit_num = cpu.fetch_word() & 0xFF

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7

    if ea_mode == 0:
        # Data register — 32-bit, modulo 32
        bit_num &= 31
        val = cpu.d[ea_reg]
        cpu.set_flag(SR_ZERO, not bool(val & (1 << bit_num)))
        if op_type == 1:  # BCHG
            cpu.d[ea_reg] = val ^ (1 << bit_num)
        elif op_type == 2:  # BCLR
            cpu.d[ea_reg] = val & ~(1 << bit_num)
        elif op_type == 3:  # BSET
            cpu.d[ea_reg] = val | (1 << bit_num)
        return 6 if op_type == 0 else 8
    else:
        # Memory — byte, modulo 8
        bit_num &= 7
        ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 1)
        val = read_ea(cpu, ea_type, ea_val, 1)
        cpu.set_flag(SR_ZERO, not bool(val & (1 << bit_num)))
        if op_type == 1:  # BCHG
            write_ea(cpu, ea_type, ea_val, 1, val ^ (1 << bit_num))
        elif op_type == 2:  # BCLR
            write_ea(cpu, ea_type, ea_val, 1, val & ~(1 << bit_num))
        elif op_type == 3:  # BSET
            write_ea(cpu, ea_type, ea_val, 1, val | (1 << bit_num))
        return 8 if op_type == 0 else 12


def op_btst_dyn(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 0, True)

def op_bchg_dyn(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 1, True)

def op_bclr_dyn(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 2, True)

def op_bset_dyn(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 3, True)

def op_btst_imm(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 0, False)

def op_bchg_imm(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 1, False)

def op_bclr_imm(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 2, False)

def op_bset_imm(cpu: MC68010, opword: int) -> int:
    return _bit_op(cpu, opword, 3, False)


# ═══════════════════════════════════════════════════════════════════
#  Shift/Rotate: ASL / ASR / LSL / LSR / ROL / ROR / ROXL / ROXR
# ═══════════════════════════════════════════════════════════════════

def op_shift_reg(cpu: MC68010, opword: int) -> int:
    """Shift/rotate register by count (immediate or from register)."""
    reg = opword & 0x7
    count_or_reg = (opword >> 9) & 0x7
    size = 1 << ((opword >> 6) & 0x3)
    ir = (opword >> 5) & 0x1  # 0=immediate count, 1=register count
    dr = (opword >> 8) & 0x1  # 0=right, 1=left
    op_type = (opword >> 3) & 0x3  # 00=AS, 01=LS, 10=ROX, 11=RO

    if ir:
        count = cpu.d[count_or_reg] & 63
    else:
        count = count_or_reg if count_or_reg != 0 else 8

    mask = SIZE_MASK[size]
    msb = MSB_MASK[size]
    bits = size * 8
    val = cpu.get_d(reg, size)

    if count == 0:
        _set_logic_flags(cpu, val, size)
        cpu.set_flag(SR_CARRY, False)
        return 6

    match (op_type, dr):
        case (0, 1):  # ASL (arithmetic shift left)
            result = val
            last_out = False
            overflow = False
            for _ in range(count):
                last_out = bool(result & msb)
                old_msb = result & msb
                result = (result << 1) & mask
                if (result & msb) != old_msb:
                    overflow = True
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, overflow)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (0, 0):  # ASR (arithmetic shift right)
            result = val
            sign = val & msb
            last_out = False
            for _ in range(count):
                last_out = bool(result & 1)
                result = (result >> 1) | sign
                result &= mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (1, 1):  # LSL (logical shift left)
            result = val
            last_out = False
            for _ in range(count):
                last_out = bool(result & msb)
                result = (result << 1) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (1, 0):  # LSR (logical shift right)
            result = val
            last_out = False
            for _ in range(count):
                last_out = bool(result & 1)
                result = (result >> 1) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (3, 1):  # ROL (rotate left)
            result = val
            last_out = False
            for _ in range(count):
                last_out = bool(result & msb)
                result = ((result << 1) | (1 if last_out else 0)) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (3, 0):  # ROR (rotate right)
            result = val
            last_out = False
            for _ in range(count):
                last_out = bool(result & 1)
                result = (result >> 1) | (msb if last_out else 0)
                result &= mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (2, 1):  # ROXL (rotate left through extend)
            result = val
            x = 1 if cpu.get_flag(SR_EXTEND) else 0
            for _ in range(count):
                old_msb = 1 if (result & msb) else 0
                result = ((result << 1) | x) & mask
                x = old_msb
            cpu.set_flag(SR_CARRY, bool(x))
            cpu.set_flag(SR_EXTEND, bool(x))
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case (2, 0):  # ROXR (rotate right through extend)
            result = val
            x = 1 if cpu.get_flag(SR_EXTEND) else 0
            for _ in range(count):
                old_lsb = result & 1
                result = (result >> 1) | (msb if x else 0)
                result &= mask
                x = old_lsb
            cpu.set_flag(SR_CARRY, bool(x))
            cpu.set_flag(SR_EXTEND, bool(x))
            cpu.set_flag(SR_OVERFLOW, False)
            cpu.set_flag(SR_NEGATIVE, bool(result & msb))
            cpu.set_flag(SR_ZERO, result == 0)

        case _:
            result = val

    cpu.set_d(reg, size, result)
    return 6 + 2 * count


def op_shift_mem(cpu: MC68010, opword: int) -> int:
    """Memory shift/rotate — always word size, count=1."""
    dr = (opword >> 8) & 0x1
    op_type = (opword >> 9) & 0x3

    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    val = read_ea(cpu, ea_type, ea_val, 2)

    mask = 0xFFFF
    msb = 0x8000

    match (op_type, dr):
        case (0, 1):  # ASL
            last_out = bool(val & msb)
            old_msb = val & msb
            result = (val << 1) & mask
            overflow = (result & msb) != old_msb
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, overflow)

        case (0, 0):  # ASR
            last_out = bool(val & 1)
            sign = val & msb
            result = ((val >> 1) | sign) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, False)

        case (1, 1):  # LSL
            last_out = bool(val & msb)
            result = (val << 1) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, False)

        case (1, 0):  # LSR
            last_out = bool(val & 1)
            result = (val >> 1) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_EXTEND, last_out)
            cpu.set_flag(SR_OVERFLOW, False)

        case (3, 1):  # ROL
            last_out = bool(val & msb)
            result = ((val << 1) | (1 if last_out else 0)) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_OVERFLOW, False)

        case (3, 0):  # ROR
            last_out = bool(val & 1)
            result = ((val >> 1) | (msb if last_out else 0)) & mask
            cpu.set_flag(SR_CARRY, last_out)
            cpu.set_flag(SR_OVERFLOW, False)

        case (2, 1):  # ROXL
            x = 1 if cpu.get_flag(SR_EXTEND) else 0
            old_msb = 1 if (val & msb) else 0
            result = ((val << 1) | x) & mask
            cpu.set_flag(SR_CARRY, bool(old_msb))
            cpu.set_flag(SR_EXTEND, bool(old_msb))
            cpu.set_flag(SR_OVERFLOW, False)

        case (2, 0):  # ROXR
            x = 1 if cpu.get_flag(SR_EXTEND) else 0
            old_lsb = val & 1
            result = ((val >> 1) | (msb if x else 0)) & mask
            cpu.set_flag(SR_CARRY, bool(old_lsb))
            cpu.set_flag(SR_EXTEND, bool(old_lsb))
            cpu.set_flag(SR_OVERFLOW, False)

        case _:
            result = val

    cpu.set_flag(SR_NEGATIVE, bool(result & msb))
    cpu.set_flag(SR_ZERO, result == 0)
    write_ea(cpu, ea_type, ea_val, 2, result)
    return 8


# ═══════════════════════════════════════════════════════════════════
#  Branch: Bcc / BRA / BSR / DBcc
# ═══════════════════════════════════════════════════════════════════

def op_bcc(cpu: MC68010, opword: int) -> int:
    """Bcc / BRA / BSR — conditional/unconditional branch."""
    condition = (opword >> 8) & 0xF
    disp = opword & 0xFF

    # PC already advanced past opword; base PC for displacement
    base_pc = cpu.pc

    if disp == 0:
        # 16-bit displacement follows
        disp = _sign_extend(cpu.fetch_word(), 2)
    elif disp == 0xFF:
        # 32-bit displacement (68020+, but handle gracefully)
        disp = cpu.fetch_long()
        if disp & 0x80000000:
            disp -= 0x100000000
    else:
        # 8-bit displacement, sign-extend
        if disp & 0x80:
            disp -= 256

    if condition == 0:
        # BRA — always branch
        cpu.pc = (base_pc + disp) & 0xFFFFFF
        return 10
    elif condition == 1:
        # BSR — branch to subroutine
        cpu.push_long(cpu.pc)
        cpu.pc = (base_pc + disp) & 0xFFFFFF
        return 18
    else:
        # Bcc — conditional
        if cpu.test_condition(condition):
            cpu.pc = (base_pc + disp) & 0xFFFFFF
            return 10
        return 8  # branch not taken


def op_dbcc(cpu: MC68010, opword: int) -> int:
    """DBcc Dn,label — decrement and branch."""
    condition = (opword >> 8) & 0xF
    reg = opword & 0x7
    base_pc = cpu.pc
    disp = _sign_extend(cpu.fetch_word(), 2)

    if cpu.test_condition(condition):
        return 12  # condition true — don't decrement or branch

    # Decrement low word of Dn
    count = (cpu.d[reg] - 1) & 0xFFFF
    cpu.d[reg] = (cpu.d[reg] & 0xFFFF0000) | count

    if count != 0xFFFF:
        # Not expired — branch
        cpu.pc = (base_pc + disp) & 0xFFFFFF
        return 10
    else:
        # Counter expired — fall through
        return 14


def op_scc(cpu: MC68010, opword: int) -> int:
    """Scc <ea> — set byte to $FF if condition true, $00 if false."""
    condition = (opword >> 8) & 0xF
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 1)
    result = 0xFF if cpu.test_condition(condition) else 0x00
    write_ea(cpu, ea_type, ea_val, 1, result)
    return 4 if ea_type == EA_DATA_REG else 8


# ═══════════════════════════════════════════════════════════════════
#  JMP / JSR / RTS / BSR / LINK / UNLK
# ═══════════════════════════════════════════════════════════════════

def op_jmp(cpu: MC68010, opword: int) -> int:
    """JMP <ea> — jump to effective address."""
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 4)
    cpu.pc = ea_val & 0xFFFFFF
    return 8


def op_jsr(cpu: MC68010, opword: int) -> int:
    """JSR <ea> — jump to subroutine."""
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 4)
    cpu.push_long(cpu.pc)
    cpu.pc = ea_val & 0xFFFFFF
    return 16


def op_rts(cpu: MC68010, opword: int) -> int:
    """RTS — return from subroutine."""
    cpu.pc = cpu.pop_long() & 0xFFFFFF
    return 16


def op_link(cpu: MC68010, opword: int) -> int:
    """LINK An,#disp — create stack frame."""
    reg = opword & 0x7
    disp = _sign_extend(cpu.fetch_word(), 2)
    cpu.push_long(cpu.a[reg])
    cpu.a[reg] = cpu.a[7] & 0xFFFFFFFF
    cpu.a[7] = (cpu.a[7] + disp) & 0xFFFFFFFF
    return 16


def op_unlk(cpu: MC68010, opword: int) -> int:
    """UNLK An — destroy stack frame."""
    reg = opword & 0x7
    cpu.a[7] = cpu.a[reg] & 0xFFFFFFFF
    cpu.a[reg] = cpu.pop_long()
    return 12


# ═══════════════════════════════════════════════════════════════════
#  TRAP / RTE / STOP / NOP / RESET
# ═══════════════════════════════════════════════════════════════════

def op_trap(cpu: MC68010, opword: int) -> int:
    """TRAP #vector — software trap (vectors 32-47)."""
    vector = (opword & 0xF) + 32
    execute_exception(cpu, vector)
    return 34


def op_rte(cpu: MC68010, opword: int) -> int:
    """RTE — return from exception."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)  # privilege violation
        return 34
    return execute_rte(cpu)


def op_stop(cpu: MC68010, opword: int) -> int:
    """STOP #imm — load SR and stop."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    imm = cpu.fetch_word()
    cpu.set_sr(imm)
    cpu.stopped = True
    return 4


def op_nop(cpu: MC68010, opword: int) -> int:
    """NOP — no operation."""
    return 4


def op_reset_instr(cpu: MC68010, opword: int) -> int:
    """RESET instruction — assert reset line (supervisor only)."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    # Just a no-op in emulation — real hardware resets peripherals
    return 132


# ═══════════════════════════════════════════════════════════════════
#  MOVE to/from SR, CCR; ANDI/ORI/EORI to SR/CCR
# ═══════════════════════════════════════════════════════════════════

def op_move_to_sr(cpu: MC68010, opword: int) -> int:
    """MOVE <ea>,SR — privileged."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    data = read_ea(cpu, ea_type, ea_val, 2)
    cpu.set_sr(data)
    return 12


def op_move_from_sr(cpu: MC68010, opword: int) -> int:
    """MOVE SR,<ea> — privileged on 68010."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    write_ea(cpu, ea_type, ea_val, 2, cpu.sr)
    return 6


def op_move_to_ccr(cpu: MC68010, opword: int) -> int:
    """MOVE <ea>,CCR."""
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7
    ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, 2)
    data = read_ea(cpu, ea_type, ea_val, 2)
    cpu.set_ccr(data & 0xFF)
    return 12


def op_andi_sr(cpu: MC68010, opword: int) -> int:
    """ANDI #imm,SR — privileged."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    imm = cpu.fetch_word()
    cpu.set_sr(cpu.sr & imm)
    return 20


def op_ori_sr(cpu: MC68010, opword: int) -> int:
    """ORI #imm,SR — privileged."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    imm = cpu.fetch_word()
    cpu.set_sr(cpu.sr | imm)
    return 20


def op_eori_sr(cpu: MC68010, opword: int) -> int:
    """EORI #imm,SR — privileged."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    imm = cpu.fetch_word()
    cpu.set_sr(cpu.sr ^ imm)
    return 20


def op_andi_ccr(cpu: MC68010, opword: int) -> int:
    """ANDI #imm,CCR."""
    imm = cpu.fetch_word() & 0xFF
    cpu.set_ccr(cpu.get_ccr() & imm)
    return 20


def op_ori_ccr(cpu: MC68010, opword: int) -> int:
    """ORI #imm,CCR."""
    imm = cpu.fetch_word() & 0xFF
    cpu.set_ccr(cpu.get_ccr() | imm)
    return 20


def op_eori_ccr(cpu: MC68010, opword: int) -> int:
    """EORI #imm,CCR."""
    imm = cpu.fetch_word() & 0xFF
    cpu.set_ccr(cpu.get_ccr() ^ imm)
    return 20


# ═══════════════════════════════════════════════════════════════════
#  MOVEM — register list save/restore
# ═══════════════════════════════════════════════════════════════════

def op_movem(cpu: MC68010, opword: int) -> int:
    """MOVEM — move multiple registers to/from memory."""
    direction = (opword >> 10) & 0x1  # 0=reg-to-mem, 1=mem-to-reg
    size = 4 if (opword >> 6) & 0x1 else 2
    ea_mode = (opword >> 3) & 0x7
    ea_reg = opword & 0x7

    mask = cpu.fetch_word()

    if direction == 0:
        # Register to memory
        if ea_mode == 4:
            # Pre-decrement: register list is reversed
            addr = cpu.a[ea_reg] & 0xFFFFFFFF
            for i in range(15, -1, -1):
                if mask & (1 << (15 - i)):
                    addr = (addr - size) & 0xFFFFFFFF
                    if i < 8:
                        val = cpu.d[i]
                    else:
                        val = cpu.a[i - 8]
                    if size == 2:
                        cpu.write_word(addr, val & 0xFFFF)
                    else:
                        cpu.write_long(addr, val & 0xFFFFFFFF)
            cpu.a[ea_reg] = addr
        else:
            ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
            addr = ea_val
            for i in range(16):
                if mask & (1 << i):
                    if i < 8:
                        val = cpu.d[i]
                    else:
                        val = cpu.a[i - 8]
                    if size == 2:
                        cpu.write_word(addr, val & 0xFFFF)
                    else:
                        cpu.write_long(addr, val & 0xFFFFFFFF)
                    addr = (addr + size) & 0xFFFFFFFF
    else:
        # Memory to register
        if ea_mode == 3:
            # Post-increment
            addr = cpu.a[ea_reg] & 0xFFFFFFFF
            for i in range(16):
                if mask & (1 << i):
                    if size == 2:
                        val = cpu.read_word(addr)
                        val = _sign_extend(val, 2) & 0xFFFFFFFF
                    else:
                        val = cpu.read_long(addr)
                    if i < 8:
                        cpu.d[i] = val
                    else:
                        cpu.a[i - 8] = val
                    addr = (addr + size) & 0xFFFFFFFF
            cpu.a[ea_reg] = addr
        else:
            ea_type, ea_val = decode_ea(cpu, ea_mode, ea_reg, size)
            addr = ea_val
            for i in range(16):
                if mask & (1 << i):
                    if size == 2:
                        val = cpu.read_word(addr)
                        val = _sign_extend(val, 2) & 0xFFFFFFFF
                    else:
                        val = cpu.read_long(addr)
                    if i < 8:
                        cpu.d[i] = val
                    else:
                        cpu.a[i - 8] = val
                    addr = (addr + size) & 0xFFFFFFFF

    return 12  # base timing (varies with count)


# ═══════════════════════════════════════════════════════════════════
#  MOVEP — move peripheral data
# ═══════════════════════════════════════════════════════════════════

def op_movep(cpu: MC68010, opword: int) -> int:
    """MOVEP — move data to/from peripheral (alternate bytes)."""
    data_reg = (opword >> 9) & 0x7
    addr_reg = opword & 0x7
    opmode = (opword >> 6) & 0x7
    disp = _sign_extend(cpu.fetch_word(), 2)
    addr = (cpu.a[addr_reg] + disp) & 0xFFFFFFFF

    if opmode == 4:
        # MOVEP.W (d16,An),Dn — memory to register, word
        hi = cpu.read_byte(addr)
        lo = cpu.read_byte(addr + 2)
        cpu.d[data_reg] = (cpu.d[data_reg] & 0xFFFF0000) | (hi << 8) | lo
        return 16
    elif opmode == 5:
        # MOVEP.L (d16,An),Dn — memory to register, long
        b3 = cpu.read_byte(addr)
        b2 = cpu.read_byte(addr + 2)
        b1 = cpu.read_byte(addr + 4)
        b0 = cpu.read_byte(addr + 6)
        cpu.d[data_reg] = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
        return 24
    elif opmode == 6:
        # MOVEP.W Dn,(d16,An) — register to memory, word
        val = cpu.d[data_reg]
        cpu.write_byte(addr, (val >> 8) & 0xFF)
        cpu.write_byte(addr + 2, val & 0xFF)
        return 16
    elif opmode == 7:
        # MOVEP.L Dn,(d16,An) — register to memory, long
        val = cpu.d[data_reg]
        cpu.write_byte(addr, (val >> 24) & 0xFF)
        cpu.write_byte(addr + 2, (val >> 16) & 0xFF)
        cpu.write_byte(addr + 4, (val >> 8) & 0xFF)
        cpu.write_byte(addr + 6, val & 0xFF)
        return 24
    return 4


# ═══════════════════════════════════════════════════════════════════
#  EXG — exchange registers
# ═══════════════════════════════════════════════════════════════════

def op_exg(cpu: MC68010, opword: int) -> int:
    """EXG — exchange registers."""
    rx = (opword >> 9) & 0x7
    ry = opword & 0x7
    mode = (opword >> 3) & 0x1F

    if mode == 0x08:
        # Data registers
        cpu.d[rx], cpu.d[ry] = cpu.d[ry], cpu.d[rx]
    elif mode == 0x09:
        # Address registers
        cpu.a[rx], cpu.a[ry] = cpu.a[ry], cpu.a[rx]
    elif mode == 0x11:
        # Data and address
        cpu.d[rx], cpu.a[ry] = cpu.a[ry], cpu.d[rx]

    return 6


# ═══════════════════════════════════════════════════════════════════
#  MOVE USP
# ═══════════════════════════════════════════════════════════════════

def op_move_usp(cpu: MC68010, opword: int) -> int:
    """MOVE USP,An or MOVE An,USP — privileged."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    reg = opword & 0x7
    direction = (opword >> 3) & 0x1
    if direction:
        # USP → An
        cpu.a[reg] = cpu.usp
    else:
        # An → USP
        cpu.usp = cpu.a[reg]
    return 4


# ═══════════════════════════════════════════════════════════════════
#  MOVE VBR (68010)
# ═══════════════════════════════════════════════════════════════════

def op_movec(cpu: MC68010, opword: int) -> int:
    """MOVEC — move control register (68010+)."""
    if not cpu.supervisor:
        execute_exception(cpu, 8)
        return 34
    ext = cpu.fetch_word()
    reg_num = (ext >> 12) & 0xF
    cr = ext & 0xFFF
    direction = (opword >> 0) & 0x1  # bit 0: 0=Rc→Rn, 1=Rn→Rc

    # Get general register
    if reg_num < 8:
        is_addr = False
        rn = reg_num
    else:
        is_addr = True
        rn = reg_num - 8

    if direction == 0:
        # Control register → general register
        if cr == 0x800:  # USP
            val = cpu.usp
        elif cr == 0x801:  # VBR
            val = cpu.vbr
        elif cr == 0x000:  # SFC
            val = 0
        elif cr == 0x001:  # DFC
            val = 0
        else:
            val = 0
        if is_addr:
            cpu.a[rn] = val & 0xFFFFFFFF
        else:
            cpu.d[rn] = val & 0xFFFFFFFF
    else:
        # General register → control register
        if is_addr:
            val = cpu.a[rn]
        else:
            val = cpu.d[rn]
        if cr == 0x800:  # USP
            cpu.usp = val & 0xFFFFFFFF
        elif cr == 0x801:  # VBR
            cpu.vbr = val & 0xFFFFFFFF

    return 12


# ═══════════════════════════════════════════════════════════════════
#  Line-A / Line-F traps
# ═══════════════════════════════════════════════════════════════════

def op_line_a(cpu: MC68010, opword: int) -> int:
    """Line-A emulator trap (vector 10)."""
    cpu.pc = (cpu.pc - 2) & 0xFFFFFF
    execute_exception(cpu, 10)
    return 34


def op_line_f(cpu: MC68010, opword: int) -> int:
    """Line-F emulator trap (vector 11)."""
    cpu.pc = (cpu.pc - 2) & 0xFFFFFF
    execute_exception(cpu, 11)
    return 34
