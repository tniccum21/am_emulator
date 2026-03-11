#!/usr/bin/env python3
"""Trace I/O dispatch — find where disk driver DD.XFR is actually called.

Key findings so far:
- DD.XFR at $5150 is terminal init, NOT disk I/O dispatch
- $A03C queues DDT into $03A4-based queue
- Need to find: what processes the $03A4 queue and calls driver via DDT+$06

Approach: Search for code patterns that reference $03A4, DDT+$06, or
          load driver pointers. Also trace ALL JSR/JMP (An) calls during
          runtime to find any call into the $7AC2-$805A range.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
import shutil

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

DRIVER_RAM = 0x7AC2
MAX_COPY = 1432
AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510
CANTMR_DST = 0x9000
CLRCDB_DST = 0x9050

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# Disk patch + boot (minimal)
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

driver = scz_data[:MAX_COPY]
shutil.copy2(src_path, dst_path)
with open(dst_path, "r+b") as f:
    patched = bytearray(f.read())
    file_off = DRIVER_RAM
    blk_idx = file_off // BYTES_PER_BLOCK
    data_off = file_off % BYTES_PER_BLOCK
    drv_pos = 0
    remaining = len(driver)
    while remaining > 0:
        lba = AMOSL_START_BLOCK + blk_idx + 1
        disk_offset = lba * 512
        space = BYTES_PER_BLOCK - data_off
        count = min(remaining, space)
        disk_pos = disk_offset + 2 + data_off
        patched[disk_pos:disk_pos + count] = driver[drv_pos:drv_pos + count]
        drv_pos += count
        remaining -= count
        blk_idx += 1
        data_off = 0
    f.seek(0)
    f.write(patched)

from alphasim.config import SystemConfig
from alphasim.main import build_system

config = SystemConfig(
    rom_even_path=Path("roms/AM-178-01-B05.BIN"),
    rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
    ram_size=0x400000,
    config_dip=0x0A,
    disk_image_path=dst_path,
    trace_enabled=False,
    max_instructions=50_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None

def read_long(addr):
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)

orig_write_byte = bus._write_byte_physical
skip_hook = False

def raw_write_word(addr, val):
    addr &= ~1
    orig_write_byte(addr, val & 0xFF)
    orig_write_byte(addr + 1, (val >> 8) & 0xFF)

def raw_write_long(addr, val):
    addr &= ~1
    raw_write_word(addr, (val >> 16) & 0xFFFF)
    raw_write_word(addr + 2, val & 0xFFFF)

CORRECT_MEMBAS = 0x8800
mem_patch_done = False
write_043B_count = 0

def patching_write(address, value):
    global mem_patch_done, write_043B_count, skip_hook
    addr = address & 0xFFFFFF
    if skip_hook:
        orig_write_byte(address, value)
        return
    orig_write_byte(address, value)
    if not mem_patch_done and addr == 0x043B and count > 10000:
        write_043B_count += 1
        if write_043B_count >= 2:
            mem_patch_done = True
            skip_hook = True
            raw_write_long(0x0430, CORRECT_MEMBAS)
            raw_write_long(0x0438, 0x3F0000)
            raw_write_word(0x0426, 4)
            raw_write_long(CORRECT_MEMBAS, 0)
            raw_write_long(CORRECT_MEMBAS + 4, 0x3F0000 - CORRECT_MEMBAS)
            skip_hook = False

bus._write_byte_physical = patching_write
cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x006C0E and count > 100000:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"Boot: PC=${cpu.pc:06X} at instr {count}")

# ─── Static Analysis: Search for patterns ───
print(f"\n{'='*60}")
print("STATIC ANALYSIS — Code Pattern Search")
print(f"{'='*60}")

# 1. Search for references to $03A4 (I/O queue base)
print("\n1. References to $03A4 (I/O queue base):")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    if w == 0x03A4:
        # Show context
        prev = bus.read_word(addr - 2) if addr >= 2 else 0
        next_w = bus.read_word(addr + 2) if addr + 2 < 0x8000 else 0
        print(f"  ${addr:06X}: ${prev:04X} [${w:04X}] ${next_w:04X}")

# 2. Search for MOVEA.L $0006(An),Ax — loading driver pointer from DDT+$06
print("\n2. MOVEA.L $0006(An),Ax patterns (loading DDT+$06 driver ptr):")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    # MOVEA.L (d16,An),Ax: $2x6y where x=dest_reg<<1|size, y=mode+src_reg
    # MOVEA.L = $2_68-$2_6F for source (d16,A0)-(d16,A7)
    # Full encoding: $2x68-$2x6F where x = dest reg * 2 + 0 (long)
    # dest regs: A0=$2068, A1=$2268, A2=$2468, etc.
    if (w & 0xF1F8) == 0x2068:  # MOVEA.L (d16,An),Ax
        w2 = bus.read_word(addr + 2) if addr + 2 < 0x8000 else 0
        if w2 == 0x0006:
            dest_reg = (w >> 9) & 7
            src_reg = w & 7
            print(f"  ${addr:06X}: MOVEA.L $0006(A{src_reg}),A{dest_reg}")
            # Show wider context
            for a in range(max(0, addr - 8), addr + 12, 2):
                ww = bus.read_word(a)
                m = " <--" if a == addr else ""
                print(f"    ${a:06X}: ${ww:04X}{m}")

# 3. Search for LEA/MOVEA $03A4 — I/O queue access
print("\n3. LEA ($03A4).W patterns:")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    # LEA (xxx).W,An = $41F8 xxxx (A0), $43F8 (A1), etc.
    if (w & 0xF1FF) == 0x41F8:
        w2 = bus.read_word(addr + 2) if addr + 2 < 0x8000 else 0
        if w2 == 0x03A4:
            dest_reg = (w >> 9) & 7
            print(f"  ${addr:06X}: LEA ($03A4).W,A{dest_reg}")

# 4. Check what $69D0 is — the terminal device descriptor
print(f"\n4. Terminal device descriptor at $69D0:")
for off in range(0, 0x30, 2):
    addr = 0x69D0 + off
    w = bus.read_word(addr)
    l = read_long(addr) if off + 3 < 0x100 else 0
    print(f"  +${off:02X} (${addr:06X}): W=${w:04X} L=${l:08X}")

# 5. Check scheduler dispatch code more carefully
# At $129C, the dispatch path has: $12AE: MOVEA.L $0080(A0),A7
# then at $12B6: $4E66 = MOVE A6,USP
# then $12B8: BTST #4,($0403).W
# then $12C0: JSR $14F8
# then $12C4: ANDI.W #$F7FF,$0002(A0)
# then calls ($0514)+$04
# Then RTE
print(f"\n5. Scheduler dispatch path $129C-$12F2:")
addr = 0x129C
while addr <= 0x12F2:
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")
    addr += 2

# 6. Check the I/O queue at $03A4 after boot
print(f"\n6. I/O queue at $03A4:")
q_base = 0x03A4
for off in [0x00, 0x02, 0x04, 0x06, 0x78, 0x7A, 0x7C, 0x80, 0x84]:
    addr = q_base + off
    w = bus.read_word(addr)
    l = read_long(addr)
    print(f"  $03A4+${off:02X} (${addr:06X}): W=${w:04X} L=${l:08X}")

# Also check what's at $03A4+$78 = $041C (JOBCUR!)
print(f"\n  NOTE: $03A4+$78 = $041C (JOBCUR) = ${read_long(0x041C):08X}")

# 7. The function at $14D8 — JSR $0008(A6) where A6 = ($0514)
# What's this function doing?
print(f"\n7. Code at $14D0-$14F0 (scheduler helper that calls ($0514)+$0008):")
for addr in range(0x14C0, 0x1500, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# 8. Check the $A03C exit path — what happens after return?
# The caller was at $004E46. Let's see that code path.
print(f"\n8. Code at $004E00-$004E80 (disk mount caller):")
for addr in range(0x4E00, 0x4E80, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# 9. More context around DD.MNT call at $5104
print(f"\n9. Wider context $50A0-$5108 (mount function):")
for addr in range(0x50A0, 0x5108, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# 10. Check what DDT+$78 (queue link) and DDT+$84 look like
# for the TERMINAL DDT at $69D0
print(f"\n10. Terminal DDT ($69D0) queue fields:")
term_ddt = 0x69D0
for off in [0x78, 0x7C, 0x80, 0x84]:
    addr = term_ddt + off
    l = read_long(addr)
    print(f"  $69D0+${off:02X} (${addr:06X}): ${l:08X}")

# 11. Search for MOVEA.L $0006(A0),Ax or MOVE.L $0006(A0),Dx
# More broadly: any instruction reading offset $06 from A0
print(f"\n11. Any read from +$06(A0) [DDT's driver ptr]:")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    # MOVE.L $0006(A0),Dn = $2028 $0006
    # MOVEA.L $0006(A0),An = $2068 $0006
    if w in (0x2028, 0x2068, 0x2228, 0x2268, 0x2428, 0x2468,
             0x2628, 0x2668, 0x2828, 0x2868, 0x2A28, 0x2A68,
             0x2C28, 0x2C68, 0x2E28, 0x2E68):
        w2 = bus.read_word(addr + 2)
        if w2 == 0x0006:
            # Determine operation
            if (w & 0x01C0) == 0x0040:
                dest = f"A{(w >> 9) & 7}"
            else:
                dest = f"D{(w >> 9) & 7}"
            print(f"  ${addr:06X}: MOVE.L $0006(A0),{dest}")
            for a in range(max(0, addr - 6), addr + 10, 2):
                ww = bus.read_word(a)
                m = " <--" if a == addr else ""
                print(f"    ${a:06X}: ${ww:04X}{m}")

print("\nDone!")
