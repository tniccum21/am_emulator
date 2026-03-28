#!/usr/bin/env python3
"""Examine the overflow subroutines in SCZ.DVR that don't fit in the
1432-byte reserved space at $7AC2.

Missing subroutines:
  +$05C8 - +$0616 (RTS) = 78 bytes, called from +$0204
  +$0618 - +$0628 (RTS) = 16 bytes, called from 7 places
  +$0650 - +$065E = data table (14 bytes)
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# Read SCZ.DVR
with open(src_path, "rb") as f:
    img = bytearray(f.read())

scz_data = bytearray()
block = 1402
while block != 0:
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    scz_data.extend(img[offset+2:offset+512])
    if link == 0:
        break
    block = link

print(f"SCZ.DVR: {len(scz_data)} bytes")

def disasm_word(data, pos):
    """Simple 68000 word disassembly for common instructions."""
    if pos + 1 >= len(data):
        return None, 2
    w = (data[pos+1] << 8) | data[pos]

    if w == 0x4E75:
        return "RTS", 2
    if w == 0x4E74:
        return "RTR", 2
    if w == 0x4E73:
        return "RTE", 2
    if w == 0x4E71:
        return "NOP", 2

    # MOVEM
    if (w & 0xFFC0) == 0x4880:
        return f"MOVEM.W ...", 4
    if (w & 0xFFC0) == 0x48C0:
        return f"MOVEM.L ...", 4

    # MOVE.B
    if (w & 0xF000) == 0x1000:
        return f"MOVE.B ${w:04X}", 2
    # MOVE.W
    if (w & 0xF000) == 0x3000:
        return f"MOVE.W ${w:04X}", 2
    # MOVE.L
    if (w & 0xF000) == 0x2000:
        return f"MOVE.L ${w:04X}", 2

    # BSR.S
    if (w & 0xFF00) == 0x6100 and (w & 0xFF) != 0:
        disp = w & 0xFF
        if disp >= 0x80:
            disp -= 256
        target = pos + 2 + disp
        return f"BSR.S +${target:04X}", 2

    # BSR.W
    if w == 0x6100:
        if pos + 3 < len(data):
            d = (data[pos+3] << 8) | data[pos+2]
            if d >= 0x8000:
                d -= 0x10000
            target = pos + 2 + d
            return f"BSR.W +${target:04X}", 4
        return "BSR.W ???", 4

    # BRA.S
    if (w & 0xFF00) == 0x6000 and (w & 0xFF) != 0:
        disp = w & 0xFF
        if disp >= 0x80:
            disp -= 256
        target = pos + 2 + disp
        return f"BRA.S +${target:04X}", 2

    # Bcc.S
    if (w & 0xF000) == 0x6000 and (w & 0xFF) != 0:
        cc = (w >> 8) & 0xF
        cc_names = {0:'T',1:'F',2:'HI',3:'LS',4:'CC',5:'CS',6:'NE',7:'EQ',
                    8:'VC',9:'VS',10:'PL',11:'MI',12:'GE',13:'LT',14:'GT',15:'LE'}
        disp = w & 0xFF
        if disp >= 0x80:
            disp -= 256
        target = pos + 2 + disp
        return f"B{cc_names.get(cc,'??')}.S +${target:04X}", 2

    # JMP (d16,PC)
    if w == 0x4EFA:
        if pos + 3 < len(data):
            d = (data[pos+3] << 8) | data[pos+2]
            if d >= 0x8000:
                d -= 0x10000
            target = pos + 2 + d
            return f"JMP +${target:04X} (PC)", 4

    # JSR (d16,PC)
    if w == 0x4EBA:
        if pos + 3 < len(data):
            d = (data[pos+3] << 8) | data[pos+2]
            if d >= 0x8000:
                d -= 0x10000
            target = pos + 2 + d
            return f"JSR +${target:04X} (PC)", 4

    # JSR (abs.L)
    if w == 0x4EB9:
        if pos + 5 < len(data):
            hi = (data[pos+3] << 8) | data[pos+2]
            lo = (data[pos+5] << 8) | data[pos+4]
            target = (hi << 16) | lo
            return f"JSR ${target:08X}", 6

    # LINE-A
    if (w & 0xF000) == 0xA000:
        return f"LINE-A ${w & 0xFFF:03X}", 2

    # ANDI/ORI/CMPI etc
    if (w & 0xFF00) == 0x0200:
        return f"ANDI.B #${(data[pos+3] << 8) | data[pos+2]:04X},..." if pos+3 < len(data) else f"ANDI.B ...", 4
    if (w & 0xFF00) == 0x0000:
        return f"ORI.B ...", 4

    # TST
    if (w & 0xFFC0) == 0x4A00:
        return f"TST.B ${w:04X}", 2
    if (w & 0xFFC0) == 0x4A40:
        return f"TST.W ${w:04X}", 2
    if (w & 0xFFC0) == 0x4A80:
        return f"TST.L ${w:04X}", 2

    # CLR
    if (w & 0xFFC0) == 0x4200:
        return f"CLR.B ${w:04X}", 2

    return f"${w:04X}", 2

# ─── Dump the overflow subroutines ───
print(f"\n{'='*60}")
print(f"SUBROUTINE 1: +$05C8 to +$0616 (78 bytes)")
print(f"Called from: +$0204")
print(f"{'='*60}")

pos = 0x05C8
while pos <= 0x0616:
    inst, size = disasm_word(scz_data, pos)
    w = (scz_data[pos+1] << 8) | scz_data[pos]
    extra = ""
    if size == 4 and pos + 3 < len(scz_data):
        w2 = (scz_data[pos+3] << 8) | scz_data[pos+2]
        extra = f" {w2:04X}"
    print(f"  +${pos:04X}: {w:04X}{extra:>5s}  {inst}")
    pos += size

print(f"\n{'='*60}")
print(f"SUBROUTINE 2: +$0618 to +$0628 (16 bytes)")
print(f"Called from: +$03B0, +$040A, +$043C, +$046C, +$0490, +$057C, +$0592")
print(f"{'='*60}")

pos = 0x0618
while pos <= 0x0628:
    inst, size = disasm_word(scz_data, pos)
    w = (scz_data[pos+1] << 8) | scz_data[pos]
    extra = ""
    if size == 4 and pos + 3 < len(scz_data):
        w2 = (scz_data[pos+3] << 8) | scz_data[pos+2]
        extra = f" {w2:04X}"
    print(f"  +${pos:04X}: {w:04X}{extra:>5s}  {inst}")
    pos += size

# ─── Also dump the data table ───
print(f"\n{'='*60}")
print(f"DATA TABLE: +$0650 to +$065E")
print(f"{'='*60}")
for pos in range(0x0650, 0x0660, 2):
    if pos + 1 < len(scz_data):
        w = (scz_data[pos+1] << 8) | scz_data[pos]
        print(f"  +${pos:04X}: ${w:04X}")

# ─── Check if data table is referenced ───
print(f"\n{'='*60}")
print(f"REFERENCES TO DATA TABLE (+$0650)")
print(f"{'='*60}")
for pos in range(0, len(scz_data) - 3, 2):
    w = (scz_data[pos+1] << 8) | scz_data[pos]
    # LEA (d16,PC) = $41FA
    if w == 0x41FA:
        d = (scz_data[pos+3] << 8) | scz_data[pos+2]
        if d >= 0x8000:
            d -= 0x10000
        target = pos + 2 + d
        if 0x0640 <= target <= 0x0670:
            print(f"  LEA +${target:04X}(PC),A0 at +${pos:04X}")
    # Also check other LEA forms: $43FA, $45FA, etc. (LEA (d16,PC),An)
    if (w & 0xF1FF) == 0x41FA:
        d = (scz_data[pos+3] << 8) | scz_data[pos+2]
        if d >= 0x8000:
            d -= 0x10000
        target = pos + 2 + d
        if 0x0640 <= target <= 0x0670 and target != pos + 2 + d:
            an = (w >> 9) & 7
            print(f"  LEA +${target:04X}(PC),A{an} at +${pos:04X}")

# ─── Find free space in AMOSL.MON for relocation ───
print(f"\n{'='*60}")
print(f"SEARCHING FOR FREE SPACE IN AMOSL.MON")
print(f"{'='*60}")

# Read AMOSL.MON
amosl_data = bytearray()
block = 3257
for i in range(69):
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    amosl_data.extend(img[offset+2:offset+512])
    if link == 0:
        break
    block = link

# Find zero runs > 100 bytes outside the driver area
print(f"Looking for zero runs > 100 bytes (excluding $7AC2-$805A)...")
run_start = None
run_len = 0
for addr in range(0, len(amosl_data)):
    if amosl_data[addr] == 0:
        if run_start is None:
            run_start = addr
        run_len += 1
    else:
        if run_len >= 100:
            if not (0x7AC2 <= run_start < 0x805A):
                print(f"  ${run_start:06X}-${run_start+run_len-1:06X}: {run_len} bytes of zeros")
        run_start = None
        run_len = 0

# Final run
if run_len >= 100 and run_start is not None:
    if not (0x7AC2 <= run_start < 0x805A):
        print(f"  ${run_start:06X}-${run_start+run_len-1:06X}: {run_len} bytes of zeros")

# ─── Check what the callers do around the JSR calls ───
print(f"\n{'='*60}")
print(f"CONTEXT AROUND CROSS-BOUNDARY CALLS")
print(f"{'='*60}")

call_sites = [0x0204, 0x03B0, 0x040A, 0x043C, 0x046C, 0x0490, 0x057C, 0x0592]
for site in call_sites:
    print(f"\n  --- Call at +${site:04X} ---")
    for p in range(max(0, site - 8), min(len(scz_data) - 1, site + 8), 2):
        inst, size = disasm_word(scz_data, p)
        w = (scz_data[p+1] << 8) | scz_data[p]
        marker = " <-- CALL" if p == site else ""
        print(f"    +${p:04X}: {w:04X}  {inst}{marker}")
