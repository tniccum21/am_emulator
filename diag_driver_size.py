#!/usr/bin/env python3
"""Find the exact driver reservation size in the original AMOSL.MON.

The DDT+$04 = $7AC2 is the driver code pointer. The original disk has
zeros there. We need to find how many zeros exist before the next
non-zero OS data, to know how much of SCZ.DVR we can safely inject.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

# Use the ORIGINAL (unpatched) image
img_path = Path("images/AMOS_1-3_Boot_OS.img")
with open(img_path, "rb") as f:
    img = f.read()

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# AMOSL.MON parameters
AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510
DRIVER_ADDR = 0x7AC2

# Read AMOSL.MON from disk, stripping link words
amosl_data = bytearray()
block = AMOSL_START_BLOCK
for i in range(69):
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    amosl_data.extend(img[offset+2:offset+512])
    if link == 0:
        break
    block = link

print(f"AMOSL.MON loaded: {len(amosl_data)} bytes")
print(f"RAM range: $000000-${len(amosl_data)-1:06X}")

# Find the boundary: where do zeros end after $7AC2?
print(f"\nSearching for first non-zero byte after ${DRIVER_ADDR:06X}...")

first_nz = None
for addr in range(DRIVER_ADDR, len(amosl_data)):
    if amosl_data[addr] != 0:
        first_nz = addr
        break

if first_nz:
    zero_count = first_nz - DRIVER_ADDR
    print(f"  First non-zero at ${first_nz:06X}")
    print(f"  Zero region: ${DRIVER_ADDR:06X}-${first_nz-1:06X} ({zero_count} bytes)")
    print(f"  Maximum driver size: {zero_count} bytes")

    # Show what's at the boundary
    print(f"\n  Data at boundary (${first_nz:06X}):")
    for base in range(first_nz, min(first_nz + 64, len(amosl_data)), 16):
        words = []
        for w in range(0, 16, 2):
            if base + w + 1 < len(amosl_data):
                val = (amosl_data[base+w+1] << 8) | amosl_data[base+w]
                words.append(f"{val:04X}")
        print(f"    ${base:06X}: {' '.join(words)}")

    # Check what this address is — is it an OS entry point?
    # Check the vector table at $000000
    print(f"\n  Checking vector table for references to ${first_nz:06X}...")
    for vec_addr in range(0, 0x400, 4):
        if vec_addr + 3 < len(amosl_data):
            # Read longword (two LE words)
            lo = (amosl_data[vec_addr+1] << 8) | amosl_data[vec_addr]
            hi = (amosl_data[vec_addr+3] << 8) | amosl_data[vec_addr+2]
            val = (hi << 16) | lo
            if val == first_nz:
                vec_num = vec_addr // 4
                print(f"    Vector {vec_num} at ${vec_addr:04X} → ${val:06X}")
else:
    print(f"  All zeros from ${DRIVER_ADDR:06X} to end of file!")

# Also check: what's in the area BEFORE $7AC2?
print(f"\nLast non-zero before ${DRIVER_ADDR:06X}:")
last_nz = None
for addr in range(DRIVER_ADDR - 1, max(0, DRIVER_ADDR - 0x200), -1):
    if amosl_data[addr] != 0:
        last_nz = addr
        break

if last_nz:
    print(f"  Last non-zero at ${last_nz:06X}")
    gap = DRIVER_ADDR - last_nz - 1
    print(f"  Gap to driver start: {gap} bytes")
    print(f"  Data around ${last_nz:06X}:")
    for base in range(max(0, last_nz - 16), last_nz + 32, 16):
        words = []
        for w in range(0, 16, 2):
            if base + w + 1 < len(amosl_data):
                val = (amosl_data[base+w+1] << 8) | amosl_data[base+w]
                words.append(f"{val:04X}")
        print(f"    ${base:06X}: {' '.join(words)}")

# Now examine the SCZ.DVR structure to understand what MONGEN copies
print(f"\n{'='*60}")
print(f"SCZ.DVR STRUCTURE ANALYSIS")
print(f"{'='*60}")

# Read SCZ.DVR
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

print(f"SCZ.DVR total: {len(scz_data)} bytes")

# Find where non-zero data ends in SCZ.DVR
last_scz_nz = 0
for i in range(len(scz_data) - 1, -1, -1):
    if scz_data[i] != 0:
        last_scz_nz = i
        break

print(f"Last non-zero byte in SCZ.DVR: offset ${last_scz_nz:04X} ({last_scz_nz})")
print(f"Effective driver data size: {last_scz_nz + 1} bytes")

# Find code boundaries (look for RTS instructions = $4E75)
print(f"\nRTS ($4E75) instructions in SCZ.DVR:")
for pos in range(0, len(scz_data) - 1, 2):
    w = (scz_data[pos+1] << 8) | scz_data[pos]
    if w == 0x4E75:
        print(f"  +${pos:04X} ({pos})")

# Check if SCZ.DVR header matches DDT format
print(f"\nSCZ.DVR header vs DDT structure:")
print(f"  SCZ[+$00]: ${(scz_data[1]<<8)|scz_data[0]:04X} ${(scz_data[3]<<8)|scz_data[2]:04X}  DDT+$00: status/capability")
print(f"  SCZ[+$04]: ${(scz_data[5]<<8)|scz_data[4]:04X} ${(scz_data[7]<<8)|scz_data[6]:04X}  DDT+$04: driver code ptr")
print(f"  SCZ[+$08]: ${(scz_data[9]<<8)|scz_data[8]:04X} ${(scz_data[11]<<8)|scz_data[10]:04X}  DDT+$08: JMP entry?")

# Check if the header is a complete DDT that should REPLACE the DDT at $7038
# rather than being placed at $7AC2
print(f"\n  SCZ.DVR header as DDT overlay:")
for off in range(0, 0x40, 4):
    lo = (scz_data[off+1] << 8) | scz_data[off]
    hi = (scz_data[off+3] << 8) | scz_data[off+2]
    val = (hi << 16) | lo
    if val != 0:
        print(f"    +${off:02X}: ${val:08X}")

# Look at what the DDT at $7038 contains in the unpatched boot
print(f"\n{'='*60}")
print(f"DDT FROM UNPATCHED BOOT")
print(f"{'='*60}")

# Boot with unpatched image
from alphasim.config import SystemConfig
from alphasim.main import build_system

config = SystemConfig(
    rom_even_path=Path("roms/AM-178-01-B05.BIN"),
    rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
    ram_size=0x400000,
    config_dip=0x0A,
    disk_image_path=img_path,
    trace_enabled=False,
    max_instructions=50_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None
cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x1250:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"Unpatched boot: scheduler at instruction {count}")

# Dump the full DDT
print(f"\nDDT at $7038 (from unpatched boot):")
for off in range(0, 0x90, 2):
    addr = 0x7038 + off
    w = bus.read_word(addr)
    if w != 0:
        print(f"  +${off:02X}: ${w:04X}")

# Check what's at $805A in the unpatched boot
print(f"\nCode at $805A (A-line handler, unpatched):")
for base in range(0x805A, 0x8090, 16):
    words = []
    for w in range(0, 16, 2):
        val = bus.read_word(base + w)
        words.append(f"{val:04X}")
    print(f"  ${base:06X}: {' '.join(words)}")

# Show full zero extent around $7AC2
print(f"\nZero extent scan from $7AC2:")
first_nz_ram = 0
for addr in range(0x7AC2, 0x8800, 2):
    w = bus.read_word(addr)
    if w != 0:
        first_nz_ram = addr
        break

if first_nz_ram:
    driver_space = first_nz_ram - 0x7AC2
    print(f"  First non-zero at ${first_nz_ram:06X}")
    print(f"  Available space: {driver_space} bytes (${driver_space:04X})")
    print(f"  Data at boundary:")
    for base in range(first_nz_ram, first_nz_ram + 32, 16):
        words = []
        for w in range(0, 16, 2):
            val = bus.read_word(base + w)
            words.append(f"{val:04X}")
        print(f"    ${base:06X}: {' '.join(words)}")
