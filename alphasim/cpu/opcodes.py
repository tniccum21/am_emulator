"""Opcode dispatch table builder for 68010 instruction set.

Builds a 65536-entry table indexed by opcode word.
Each entry is either a handler function or None (illegal instruction).
"""

from __future__ import annotations

from . import instructions as I


def build_opcode_table() -> list:
    """Build and return the 65536-entry opcode dispatch table."""
    table: list = [None] * 65536

    def _fill(pattern: int, mask: int, handler) -> None:
        """Fill all opcodes matching pattern & mask with handler."""
        # For each possible opcode, check if it matches
        inv_mask = (~mask) & 0xFFFF
        # Iterate over all possible values of the unmasked bits
        bits = []
        for i in range(16):
            if not (mask & (1 << i)):
                bits.append(i)

        for combo in range(1 << len(bits)):
            opcode = pattern
            for j, bit_pos in enumerate(bits):
                if combo & (1 << j):
                    opcode |= (1 << bit_pos)
            if table[opcode] is None:
                table[opcode] = handler

    def _set(opcode: int, handler) -> None:
        """Set a single opcode entry."""
        table[opcode] = handler

    # ── MOVE (sizes: byte=$1xxx, long=$2xxx, word=$3xxx) ─────────
    # MOVE/MOVEA: top 2 bits = size, bits 11-6 = dst, bits 5-0 = src
    for opcode in range(0x1000, 0x4000):
        size_code = (opcode >> 12) & 0x3
        dst_mode = (opcode >> 6) & 0x7
        dst_reg = (opcode >> 9) & 0x7
        src_mode = (opcode >> 3) & 0x7
        src_reg = opcode & 0x7

        # Skip invalid source EA modes
        if src_mode == 7 and src_reg > 4:
            continue

        if dst_mode == 1:
            # MOVEA (destination is An) — word and long only
            if size_code == 1:  # byte — invalid for MOVEA
                continue
            table[opcode] = I.op_movea
        else:
            # Regular MOVE — check valid destination modes
            if dst_mode == 7 and dst_reg > 1:
                continue  # invalid dst EA
            table[opcode] = I.op_move

    # ── MOVEQ ($7xxx) ────────────────────────────────────────────
    for opcode in range(0x7000, 0x8000):
        if not (opcode & 0x0100):  # bit 8 must be 0
            table[opcode] = I.op_moveq

    # ── $0xxx: Immediate ops, bit ops, MOVEP ─────────────────────

    # ORI to CCR: $003C
    _set(0x003C, I.op_ori_ccr)
    # ORI to SR: $007C
    _set(0x007C, I.op_ori_sr)
    # ANDI to CCR: $023C
    _set(0x023C, I.op_andi_ccr)
    # ANDI to SR: $027C
    _set(0x027C, I.op_andi_sr)
    # EORI to CCR: $0A3C
    _set(0x0A3C, I.op_eori_ccr)
    # EORI to SR: $0A7C
    _set(0x0A7C, I.op_eori_sr)

    # ORI #imm,<ea>: $00xx
    for size_bits in range(3):  # 00=byte, 01=word, 10=long
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x0000 | (size_bits << 6) | ea
            if table[op] is None:
                table[op] = I.op_ori

    # ANDI #imm,<ea>: $02xx
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x0200 | (size_bits << 6) | ea
            if table[op] is None:
                table[op] = I.op_andi

    # SUBI #imm,<ea>: $04xx
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x0400 | (size_bits << 6) | ea
            if table[op] is None:
                table[op] = I.op_subi

    # ADDI #imm,<ea>: $06xx
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x0600 | (size_bits << 6) | ea
            if table[op] is None:
                table[op] = I.op_addi

    # CMPI #imm,<ea>: $0Cxx
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x0C00 | (size_bits << 6) | ea
            if table[op] is None:
                table[op] = I.op_cmpi

    # EORI #imm,<ea>: $0Axx
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x0A00 | (size_bits << 6) | ea
            if table[op] is None:
                table[op] = I.op_eori

    # BTST/BCHG/BCLR/BSET dynamic (Dn,<ea>): $0xxx bit 8 set
    for dn in range(8):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            # BTST: bits 8-6 = 100
            op = 0x0100 | (dn << 9) | ea
            if table[op] is None:
                table[op] = I.op_btst_dyn
            # BCHG: bits 8-6 = 101
            op = 0x0140 | (dn << 9) | ea
            if ea_mode != 1 and not (ea_mode == 7 and ea_reg > 1):
                if table[op] is None:
                    table[op] = I.op_bchg_dyn
            # BCLR: bits 8-6 = 110
            op = 0x0180 | (dn << 9) | ea
            if ea_mode != 1 and not (ea_mode == 7 and ea_reg > 1):
                if table[op] is None:
                    table[op] = I.op_bclr_dyn
            # BSET: bits 8-6 = 111
            op = 0x01C0 | (dn << 9) | ea
            if ea_mode != 1 and not (ea_mode == 7 and ea_reg > 1):
                if table[op] is None:
                    table[op] = I.op_bset_dyn

    # BTST/BCHG/BCLR/BSET static (immediate bit#): $08xx
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        op = 0x0800 | ea  # BTST
        if table[op] is None:
            table[op] = I.op_btst_imm
        if ea_mode != 1 and not (ea_mode == 7 and ea_reg > 1):
            op = 0x0840 | ea  # BCHG
            if table[op] is None:
                table[op] = I.op_bchg_imm
            op = 0x0880 | ea  # BCLR
            if table[op] is None:
                table[op] = I.op_bclr_imm
            op = 0x08C0 | ea  # BSET
            if table[op] is None:
                table[op] = I.op_bset_imm

    # MOVEP: $0xxx with ea_mode=001 and specific opmodes
    for dn in range(8):
        for an in range(8):
            for opmode in (4, 5, 6, 7):
                op = (dn << 9) | (opmode << 6) | (1 << 3) | an
                table[op] = I.op_movep

    # ── $4xxx: Miscellaneous ─────────────────────────────────────

    # CLR: $4200-$42FF
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x4200 | (size_bits << 6) | ea
            table[op] = I.op_clr

    # NEG: $4400
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x4400 | (size_bits << 6) | ea
            table[op] = I.op_neg

    # NOT: $4600
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            op = 0x4600 | (size_bits << 6) | ea
            table[op] = I.op_not

    # TST: $4A00
    for size_bits in range(3):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode == 1:
                continue
            op = 0x4A00 | (size_bits << 6) | ea
            table[op] = I.op_tst

    # EXT: $4880 (word) $48C0 (long)
    for reg in range(8):
        table[0x4880 | reg] = I.op_ext  # EXT.W
        table[0x48C0 | reg] = I.op_ext  # EXT.L

    # SWAP: $4840
    for reg in range(8):
        table[0x4840 | reg] = I.op_swap

    # PEA: $4840 with ea modes
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        if ea_mode in (2, 5, 6):
            table[0x4840 | ea] = I.op_pea
        elif ea_mode == 7 and ea_reg in (0, 1, 2, 3):
            table[0x4840 | ea] = I.op_pea

    # LEA: $41C0
    for an in range(8):
        for ea in range(0x40):
            ea_mode = (ea >> 3) & 0x7
            ea_reg = ea & 0x7
            if ea_mode in (2, 5, 6):
                pass  # valid
            elif ea_mode == 7 and ea_reg in (0, 1, 2, 3):
                pass  # valid
            else:
                continue
            op = 0x41C0 | (an << 9) | ea
            table[op] = I.op_lea

    # MOVE from SR: $40C0
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        if ea_mode == 1:
            continue
        if ea_mode == 7 and ea_reg > 1:
            continue
        table[0x40C0 | ea] = I.op_move_from_sr

    # MOVE to CCR: $44C0
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        if ea_mode == 1:
            continue
        table[0x44C0 | ea] = I.op_move_to_ccr

    # MOVE to SR: $46C0
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        if ea_mode == 1:
            continue
        table[0x46C0 | ea] = I.op_move_to_sr

    # NOP: $4E71
    _set(0x4E71, I.op_nop)

    # STOP: $4E72
    _set(0x4E72, I.op_stop)

    # RTE: $4E73
    _set(0x4E73, I.op_rte)

    # RTS: $4E75
    _set(0x4E75, I.op_rts)

    # RESET: $4E70
    _set(0x4E70, I.op_reset_instr)

    # TRAP: $4E40-$4E4F
    for v in range(16):
        table[0x4E40 | v] = I.op_trap

    # LINK: $4E50-$4E57
    for reg in range(8):
        table[0x4E50 | reg] = I.op_link

    # UNLK: $4E58-$4E5F
    for reg in range(8):
        table[0x4E58 | reg] = I.op_unlk

    # MOVE USP: $4E60-$4E6F
    for reg in range(8):
        table[0x4E60 | reg] = I.op_move_usp  # An → USP
        table[0x4E68 | reg] = I.op_move_usp  # USP → An

    # MOVEC: $4E7A, $4E7B
    _set(0x4E7A, I.op_movec)
    _set(0x4E7B, I.op_movec)

    # JSR: $4E80
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        if ea_mode in (2, 5, 6):
            pass
        elif ea_mode == 7 and ea_reg in (0, 1, 2, 3):
            pass
        else:
            continue
        table[0x4E80 | ea] = I.op_jsr

    # JMP: $4EC0
    for ea in range(0x40):
        ea_mode = (ea >> 3) & 0x7
        ea_reg = ea & 0x7
        if ea_mode in (2, 5, 6):
            pass
        elif ea_mode == 7 and ea_reg in (0, 1, 2, 3):
            pass
        else:
            continue
        table[0x4EC0 | ea] = I.op_jmp

    # MOVEM: $4880-$48FF (reg-to-mem), $4C80-$4CFF (mem-to-reg)
    for opcode in range(0x4880, 0x4D00):
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7
        direction = (opcode >> 10) & 0x1
        sz = (opcode >> 6) & 0x1

        if direction == 0:
            # Reg to mem: modes 2,4,5,6,7/0,7/1
            if ea_mode in (2, 4, 5, 6):
                pass
            elif ea_mode == 7 and ea_reg in (0, 1):
                pass
            else:
                continue
        else:
            # Mem to reg: modes 2,3,5,6,7/0,7/1,7/2,7/3
            if ea_mode in (2, 3, 5, 6):
                pass
            elif ea_mode == 7 and ea_reg in (0, 1, 2, 3):
                pass
            else:
                continue

        if table[opcode] is None:
            table[opcode] = I.op_movem

    # ── $5xxx: ADDQ / SUBQ / Scc / DBcc ─────────────────────────

    for opcode in range(0x5000, 0x6000):
        size_bits = (opcode >> 6) & 0x3
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7

        if size_bits == 3:
            # Scc or DBcc
            if ea_mode == 1:
                # DBcc
                table[opcode] = I.op_dbcc
            else:
                # Scc
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_scc
        else:
            # ADDQ or SUBQ
            if (opcode >> 8) & 0x1:
                # SUBQ
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_subq
            else:
                # ADDQ
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_addq

    # ── $6xxx: Bcc / BRA / BSR ───────────────────────────────────

    for opcode in range(0x6000, 0x7000):
        table[opcode] = I.op_bcc

    # ── $8xxx: OR / DIVU / DIVS ─────────────────────────────────

    for opcode in range(0x8000, 0x9000):
        opmode = (opcode >> 6) & 0x7
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7

        if opmode == 3:
            # DIVU
            table[opcode] = I.op_divu
        elif opmode == 7:
            # DIVS
            table[opcode] = I.op_divs
        elif opmode <= 2:
            # OR <ea>,Dn
            table[opcode] = I.op_or
        elif opmode in (4, 5, 6):
            # OR Dn,<ea>
            if ea_mode == 1:
                continue
            if ea_mode == 7 and ea_reg > 1:
                continue
            table[opcode] = I.op_or

    # ── $9xxx: SUB / SUBA / SUBX ────────────────────────────────

    for opcode in range(0x9000, 0xA000):
        opmode = (opcode >> 6) & 0x7
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7

        if opmode in (3, 7):
            # SUBA
            table[opcode] = I.op_suba
        elif opmode <= 2:
            # SUB <ea>,Dn
            table[opcode] = I.op_sub
        elif opmode in (4, 5, 6):
            if ea_mode in (0, 1):
                # SUBX (reg or mem mode)
                if ea_mode == 0:
                    table[opcode] = I.op_subx
                elif ea_mode == 1:
                    # -(Ay),-(Ax) encoded as ea_mode=1 in the SUBX context
                    # Actually SUBX memory mode uses bit 3 = 1
                    pass
            else:
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_sub

    # Fix SUBX encoding: $9xx0 and $9xx8 patterns
    for rx in range(8):
        for ry in range(8):
            for size_bits in range(3):
                # SUBX Dy,Dx (register)
                op = 0x9100 | (rx << 9) | (size_bits << 6) | ry
                table[op] = I.op_subx
                # SUBX -(Ay),-(Ax) (memory)
                op = 0x9108 | (rx << 9) | (size_bits << 6) | ry
                table[op] = I.op_subx

    # ── $Axxx: Line-A ───────────────────────────────────────────

    for opcode in range(0xA000, 0xB000):
        table[opcode] = I.op_line_a

    # ── $Bxxx: CMP / CMPA / CMPM / EOR ──────────────────────────

    for opcode in range(0xB000, 0xC000):
        opmode = (opcode >> 6) & 0x7
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7

        if opmode in (3, 7):
            # CMPA
            table[opcode] = I.op_cmpa
        elif opmode <= 2:
            # CMP <ea>,Dn
            table[opcode] = I.op_cmp
        elif opmode in (4, 5, 6):
            if ea_mode == 1:
                # CMPM (Ay)+,(Ax)+
                table[opcode] = I.op_cmpm
            else:
                # EOR Dn,<ea>
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_eor

    # ── $Cxxx: AND / MULU / MULS / EXG ──────────────────────────

    for opcode in range(0xC000, 0xD000):
        opmode = (opcode >> 6) & 0x7
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7

        if opmode == 3:
            # MULU
            table[opcode] = I.op_mulu
        elif opmode == 7:
            # MULS
            table[opcode] = I.op_muls
        elif opmode <= 2:
            # AND <ea>,Dn
            table[opcode] = I.op_and
        elif opmode in (4, 5, 6):
            # Check for EXG patterns
            mode_bits = (opcode >> 3) & 0x1F
            if opmode == 5 and mode_bits in (0x08, 0x09, 0x11):
                table[opcode] = I.op_exg
            else:
                if ea_mode == 1:
                    continue
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_and

    # ── $Dxxx: ADD / ADDA / ADDX ────────────────────────────────

    for opcode in range(0xD000, 0xE000):
        opmode = (opcode >> 6) & 0x7
        ea_mode = (opcode >> 3) & 0x7
        ea_reg = opcode & 0x7

        if opmode in (3, 7):
            # ADDA
            table[opcode] = I.op_adda
        elif opmode <= 2:
            # ADD <ea>,Dn
            table[opcode] = I.op_add
        elif opmode in (4, 5, 6):
            if ea_mode == 0 or ea_mode == 1:
                pass  # handled below for ADDX
            else:
                if ea_mode == 7 and ea_reg > 1:
                    continue
                table[opcode] = I.op_add

    # Fix ADDX encoding
    for rx in range(8):
        for ry in range(8):
            for size_bits in range(3):
                op = 0xD100 | (rx << 9) | (size_bits << 6) | ry
                table[op] = I.op_addx
                op = 0xD108 | (rx << 9) | (size_bits << 6) | ry
                table[op] = I.op_addx

    # ── $Exxx: Shift/Rotate ─────────────────────────────────────

    # Register shifts: all $Exxx where bits 7-6 != 11 (not memory shift)
    # Bits 7-6 = 00 (byte), 01 (word), 10 (long)
    for opcode in range(0xE000, 0xF000):
        size_bits = (opcode >> 6) & 0x3
        if size_bits != 3:
            # Register shift/rotate
            table[opcode] = I.op_shift_reg
        else:
            # Memory shift/rotate (word size, count=1)
            ea_mode = (opcode >> 3) & 0x7
            ea_reg = opcode & 0x7
            if ea_mode in (2, 3, 4, 5, 6):
                table[opcode] = I.op_shift_mem
            elif ea_mode == 7 and ea_reg in (0, 1):
                table[opcode] = I.op_shift_mem

    # ── $Fxxx: Line-F ───────────────────────────────────────────

    for opcode in range(0xF000, 0x10000):
        table[opcode] = I.op_line_f

    return table
