"""Basic 68010 disassembler for debug trace output."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..bus.memory_bus import MemoryBus

SIZE_NAMES = {1: ".B", 2: ".W", 4: ".L"}
CONDITION_CODES = [
    "T", "F", "HI", "LS", "CC", "CS", "NE", "EQ",
    "VC", "VS", "PL", "MI", "GE", "LT", "GT", "LE",
]


def _read_word(bus: MemoryBus, addr: int) -> int:
    return bus.read_word(addr & 0xFFFFFF)


def _read_long(bus: MemoryBus, addr: int) -> int:
    return bus.read_long(addr & 0xFFFFFF)


def _sign_extend_byte(v: int) -> int:
    return v - 256 if v & 0x80 else v


def _sign_extend_word(v: int) -> int:
    return v - 65536 if v & 0x8000 else v


def _format_ea(bus: MemoryBus, pc: int, mode: int, reg: int,
               size: int) -> tuple[str, int]:
    """Format an effective address, return (text, bytes consumed)."""
    match mode:
        case 0:
            return f"D{reg}", 0
        case 1:
            return f"A{reg}", 0
        case 2:
            return f"(A{reg})", 0
        case 3:
            return f"(A{reg})+", 0
        case 4:
            return f"-(A{reg})", 0
        case 5:
            disp = _sign_extend_word(_read_word(bus, pc))
            return f"{disp}(A{reg})", 2
        case 6:
            ext = _read_word(bus, pc)
            disp = _sign_extend_byte(ext & 0xFF)
            xn = (ext >> 12) & 0xF
            xn_name = f"A{xn & 7}" if xn & 8 else f"D{xn & 7}"
            xn_size = ".L" if ext & 0x0800 else ".W"
            return f"{disp}(A{reg},{xn_name}{xn_size})", 2
        case 7:
            match reg:
                case 0:
                    addr = _sign_extend_word(_read_word(bus, pc))
                    return f"(${addr & 0xFFFF:04X}).W", 2
                case 1:
                    addr = _read_long(bus, pc)
                    return f"(${addr:08X}).L", 4
                case 2:
                    disp = _sign_extend_word(_read_word(bus, pc))
                    return f"{disp}(PC)", 2
                case 3:
                    ext = _read_word(bus, pc)
                    disp = _sign_extend_byte(ext & 0xFF)
                    xn = (ext >> 12) & 0xF
                    xn_name = f"A{xn & 7}" if xn & 8 else f"D{xn & 7}"
                    xn_size = ".L" if ext & 0x0800 else ".W"
                    return f"{disp}(PC,{xn_name}{xn_size})", 2
                case 4:
                    if size == 1:
                        val = _read_word(bus, pc) & 0xFF
                        return f"#${val:02X}", 2
                    elif size == 2:
                        val = _read_word(bus, pc)
                        return f"#${val:04X}", 2
                    else:
                        val = _read_long(bus, pc)
                        return f"#${val:08X}", 4
    return "???", 0


def disassemble_one(bus: MemoryBus, pc: int) -> tuple[str, int]:
    """Disassemble one instruction at the given PC.

    Returns (text, instruction_length_in_bytes).
    """
    opword = _read_word(bus, pc)
    pos = pc + 2

    top4 = (opword >> 12) & 0xF

    # Quick check for common instructions
    if top4 in (1, 2, 3):
        # MOVE
        size_map = {1: 1, 2: 4, 3: 2}
        size = size_map[top4]
        src_mode = (opword >> 3) & 0x7
        src_reg = opword & 0x7
        dst_reg = (opword >> 9) & 0x7
        dst_mode = (opword >> 6) & 0x7

        if dst_mode == 1:
            mnem = f"MOVEA{SIZE_NAMES[size]}"
        else:
            mnem = f"MOVE{SIZE_NAMES[size]}"

        src_str, src_bytes = _format_ea(bus, pos, src_mode, src_reg, size)
        pos += src_bytes
        dst_str, dst_bytes = _format_ea(bus, pos, dst_mode, dst_reg, size)
        pos += dst_bytes
        return f"{mnem:10s} {src_str},{dst_str}", pos - pc

    if top4 == 7 and not (opword & 0x0100):
        # MOVEQ
        data = opword & 0xFF
        reg = (opword >> 9) & 0x7
        return f"MOVEQ      #{_sign_extend_byte(data)},D{reg}", 2

    if top4 == 6:
        # Bcc / BRA / BSR
        cond = (opword >> 8) & 0xF
        disp8 = opword & 0xFF
        if disp8 == 0:
            disp = _sign_extend_word(_read_word(bus, pos))
            pos += 2
        elif disp8 == 0xFF:
            disp = _read_long(bus, pos)
            pos += 4
        else:
            disp = _sign_extend_byte(disp8)

        target = (pc + 2 + disp) & 0xFFFFFF
        if cond == 0:
            mnem = "BRA"
        elif cond == 1:
            mnem = "BSR"
        else:
            mnem = f"B{CONDITION_CODES[cond]}"
        return f"{mnem:10s} ${target:06X}", pos - pc

    # Fallback: just show the hex opcode
    return f"DC.W       ${opword:04X}", 2
