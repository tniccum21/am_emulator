#!/usr/bin/env python3
"""Quick check: what does the emulator return when reading SASI address $FFFE11?
And what does the full ($0514)+$04 handler do?
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

# Minimal boot
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

cpu.reset()

# Quick check: what does the bus return from SASI addresses?
print("SASI Controller Address Range ($FFFE10-$FFFE1F):")
for addr in range(0xFFFE10, 0xFFFE20):
    try:
        val = bus.read_byte(addr)
        print(f"  ${addr:06X}: ${val:02X}")
    except Exception as e:
        print(f"  ${addr:06X}: EXCEPTION: {e}")

# Also check via word reads
print("\nWord reads:")
for addr in range(0xFFFE10, 0xFFFE20, 2):
    try:
        val = bus.read_word(addr)
        print(f"  ${addr:06X}: ${val:04X}")
    except Exception as e:
        print(f"  ${addr:06X}: EXCEPTION: {e}")

# Check MOVEP-style reads (alternate bytes)
# MOVEP.W $0004(A5),D6 where A5=$FFFE11
# Reads bytes at $FFFE15 and $FFFE17
print(f"\nMOVEP-style reads (A5=$FFFE11, $0004(A5)):")
a5 = 0xFFFE11
for off in [0, 1, 2, 3, 4, 5, 6, 7]:
    addr = a5 + off
    try:
        val = bus.read_byte(addr)
        print(f"  ${addr:06X}: ${val:02X}")
    except Exception as e:
        print(f"  ${addr:06X}: EXCEPTION: {e}")

# Boot to $006C0E and then check the ($0514)+$04 handler fully
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

print(f"\nBoot: PC=${cpu.pc:06X}")

# Full ($0514)+$04 handler disassembly
print(f"\n($0514)+$04 handler at $1732 — extended disassembly:")
for addr in range(0x1732, 0x1760, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# Also ($0514)+$00 and +$02
print(f"\n($0514)+$00 handler at $1724:")
for addr in range(0x1724, 0x1732, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

print(f"\n($0514)+$02 handler at $172C:")
for addr in range(0x172C, 0x1732, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# Check what DD.HWA the DDT has
print(f"\nDDT $7038 — extended dump looking for DD.HWA:")
for off in range(0, 0x30, 2):
    addr = 0x7038 + off
    w = bus.read_word(addr)
    l = read_long(addr)
    if w != 0:
        print(f"  +${off:02X}: W=${w:04X} L=${l:08X}")

# Check what devices the bus has registered
print(f"\nBus device map (checking what's at $FFFE00-$FFFFFF):")
for base in range(0xFFFE00, 0x1000000, 0x10):
    for off in range(0, 0x10, 2):
        addr = base + off
        try:
            val = bus.read_word(addr)
            if val != 0:
                print(f"  ${addr:06X}: ${val:04X}")
                break
        except:
            pass

print("\nDone!")
