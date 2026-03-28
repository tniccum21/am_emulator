#!/usr/bin/env python3
"""Final dispatch diagnostic — check DD.HWA and trace I/O processing code at $1380+.

Root cause identified: DDT+$80 (saved SP) points to boot code, not I/O dispatch code.
MONGEN would set this up, but we're patching manually.

This script:
1. Check DD.HWA in the disk driver descriptor
2. Dump the I/O processing code at $1380-$1500
3. Examine the DDB format
4. Determine what's needed to fix I/O dispatch
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

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# Disk patch + boot
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

print(f"Boot: PC=${cpu.pc:06X}")

# ─── 1. Check DD.HWA in driver descriptor ───
print(f"\n{'='*60}")
print("1. DISK DRIVER DESCRIPTOR ($7AC2)")
print(f"{'='*60}")

for off in range(0, 0x30, 2):
    addr = DRIVER_RAM + off
    w = bus.read_word(addr)
    l = read_long(addr) if off + 3 < 0x100 else 0
    name = ""
    if off == 0x00: name = "DD.BSZ (ORI.B #0,D0)"
    elif off == 0x04: name = "SN.SIZ (ORI.B #120,D1)"
    elif off == 0x08: name = "DD.XFR (JMP XFER)"
    elif off == 0x0C: name = "(cont)"
    elif off == 0x18: name = "DD.HWA? (hw addr)"
    elif off == 0x28: name = "DD.MNT (JMP INIT)"
    elif off == 0x2C: name = "(cont)"
    print(f"  +${off:02X} (${addr:06X}): W=${w:04X} L=${l:08X}  {name}")

# Check DD.HWA specifically
dd_hwa = read_long(DRIVER_RAM + 0x18)
print(f"\n  DD.HWA (long at +$18): ${dd_hwa:08X}")
print(f"  As 24-bit address: ${dd_hwa & 0xFFFFFF:06X}")

# Also check nearby offsets for the HWA
for off in [0x16, 0x18, 0x1A, 0x1C, 0x1E, 0x20]:
    l = read_long(DRIVER_RAM + off)
    if (l & 0xFFFF0000) == 0xFFFF0000 or (l & 0xFF000000) == 0xFF000000:
        print(f"  Possible HWA at +${off:02X}: ${l:08X} → ${l & 0xFFFFFF:06X}")

# Compare with terminal driver's DD.HWA
term_hwa = read_long(0x69D0 + 0x18)
print(f"\n  Terminal DD.HWA (+$18): ${term_hwa:08X} → ACIA at ${term_hwa & 0xFFFFFF:06X}")

# ─── 2. I/O processing code at $1380-$1500 ───
print(f"\n{'='*60}")
print("2. I/O PROCESSING CODE ($1380-$1500)")
print(f"{'='*60}")

for addr in range(0x1380, 0x1500, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── 3. DDB chain and format ───
print(f"\n{'='*60}")
print("3. DDB CHAIN (from $0408)")
print(f"{'='*60}")

ddbchn = read_long(0x0408)
print(f"  DDBCHN ($0408): ${ddbchn:08X}")

if ddbchn != 0 and ddbchn < 0x400000:
    ddb = ddbchn
    ddb_count = 0
    while ddb != 0 and ddb < 0x400000 and ddb_count < 10:
        ddb_count += 1
        print(f"\n  DDB #{ddb_count} at ${ddb:06X}:")
        for off in range(0, 0x30, 2):
            addr = ddb + off
            w = bus.read_word(addr)
            l = read_long(addr) if off + 3 < 0x100 else 0
            name = ""
            if off == 0x00: name = "link"
            elif off == 0x04: name = "device ID?"
            elif off == 0x08: name = "DDT pointer?"
            elif off == 0x0E: name = "block/cmd?"
            elif off == 0x12: name = "buffer addr?"
            elif off == 0x16: name = "size?"
            if w != 0 or off in (0x00, 0x04, 0x08, 0x0E, 0x12):
                print(f"    +${off:02X}: W=${w:04X} L=${l:08X}  {name}")
        ddb = read_long(ddb)

# ─── 4. DEVTBL ($0404) ───
print(f"\n{'='*60}")
print("4. DEVTBL ($0404)")
print(f"{'='*60}")

devtbl = read_long(0x0404)
print(f"  DEVTBL: ${devtbl:08X}")
if devtbl != 0 and devtbl < 0x400000:
    for off in range(0, 0x40, 2):
        addr = devtbl + off
        w = bus.read_word(addr)
        if w != 0:
            l = read_long(addr)
            print(f"  +${off:02X} (${addr:06X}): W=${w:04X} L=${l:08X}")

# ─── 5. Check what reads from SASI address $FFFFE0-$FFFFE7 return ───
print(f"\n{'='*60}")
print("5. SASI CONTROLLER AT $FFFFE0-$FFFFE7")
print(f"{'='*60}")

for addr in range(0xFFFFE0, 0xFFFFE8):
    try:
        val = bus.read_byte(addr)
        print(f"  ${addr:06X}: ${val:02X}")
    except Exception as e:
        print(f"  ${addr:06X}: EXCEPTION: {e}")

# ─── 6. Check ($0462) function pointer (used by I/O dispatch at $5148) ───
print(f"\n{'='*60}")
print("6. ($0462) I/O FUNCTION POINTER")
print(f"{'='*60}")

fn_0462 = read_long(0x0462)
print(f"  ($0462).L = ${fn_0462:08X}")
if fn_0462 != 0 and fn_0462 < 0x400000:
    print(f"  Code at ${fn_0462:06X}:")
    for addr in range(fn_0462, fn_0462 + 20, 2):
        w = bus.read_word(addr)
        print(f"    ${addr:06X}: ${w:04X}")

# ─── 7. What is the DD.HWA offset? Check SCZ.DVR disassembly ───
# The XFER entry at +$005C uses: MOVEA.L DD.HWA(A0),A5
# A0 points to driver base ($7AC2 = START)
# DD.HWA is the offset from START to the hardware address longword
# Let me find it by checking what MOVEA.L instructions are in the XFER code

print(f"\n{'='*60}")
print("7. XFER CODE — Find DD.HWA offset")
print(f"{'='*60}")

xfer_addr = DRIVER_RAM + 0x005C  # XFER entry
print(f"  XFER at ${xfer_addr:06X}:")
for addr in range(xfer_addr, xfer_addr + 40, 2):
    w = bus.read_word(addr)
    print(f"    ${addr:06X}: ${w:04X}")

# Search for MOVEA.L (d16,A0),A5 near XFER
# Encoding: MOVEA.L (d16,A0),A5 = $2A68 followed by displacement word
print(f"\n  Searching for MOVEA.L (d16,A0),A5 ($2A68) in driver:")
for addr in range(DRIVER_RAM, DRIVER_RAM + MAX_COPY - 2, 2):
    w = bus.read_word(addr)
    if w == 0x2A68:
        w2 = bus.read_word(addr + 2)
        off = addr - DRIVER_RAM
        print(f"    +${off:04X} (${addr:06X}): MOVEA.L ${w2:04X}(A0),A5")
        # The actual hardware address
        target = read_long(DRIVER_RAM + w2)
        print(f"      Value at $7AC2+${w2:04X}: ${target:08X}")

print("\nDone!")
