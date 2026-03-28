#!/usr/bin/env python3
"""Patch SCZ.DVR into AMOSL.MON at $7AC2 on the disk image.

AMOSL.MON's DDT at $7038 has driver code pointer = $7AC2, but the disk
image has zeros there because MONGEN was never run. This script:
1. Extracts SCZ.DVR from the disk (blocks 1402-1405, 2040 bytes)
2. Creates a patched copy of the disk image with SCZ.DVR at $7AC2
3. Verifies the patch by loading it and checking RAM

AMOS block chain format:
- Each 512-byte block: 2-byte link word + 510 bytes data
- Link word = next block number (0 = end of file)
- LBA = block_number + 1
- AMOSL.MON starts at block 3257 (LBA 3258), 69 blocks

RAM mapping with link-word stripping:
- Block N of file: data bytes at disk positions [2..511]
- File byte offset = block_index * 510
- $7AC2 = byte 31426 = block 61, data position 316
"""
import sys
import shutil
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

def write_word_le(data, offset, value):
    data[offset] = value & 0xFF
    data[offset+1] = (value >> 8) & 0xFF

# ─── Step 1: Read SCZ.DVR from disk ───
print("Reading SCZ.DVR from disk...")
with open(src_path, "rb") as f:
    img = bytearray(f.read())

scz_data = bytearray()
block = 1402
block_count = 0
while block != 0 and block_count < 100:
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    scz_data.extend(img[offset+2:offset+512])  # Skip link word
    block_count += 1
    if link == 0:
        break
    block = link

print(f"  SCZ.DVR: {len(scz_data)} bytes ({block_count} blocks)")

# Verify it looks right (check for JMP opcode at offset $0008)
w0008 = (scz_data[0x09] << 8) | scz_data[0x08]
assert w0008 == 0x4EFA, f"Expected JMP opcode at +$0008, got ${w0008:04X}"
print(f"  Verified: JMP opcode at +$0008 = ${w0008:04X}")

# ─── Step 2: Calculate disk positions ───
print("\nCalculating patch positions...")

AMOSL_START_BLOCK = 3257
DRIVER_RAM_ADDR = 0x7AC2
BYTES_PER_BLOCK = 510  # After link word

# Which block of AMOSL.MON contains $7AC2?
file_byte_offset = DRIVER_RAM_ADDR  # RAM address = file byte offset (loaded at $0000)
block_index = file_byte_offset // BYTES_PER_BLOCK  # 61
data_offset_in_block = file_byte_offset % BYTES_PER_BLOCK  # 316

print(f"  Driver RAM address: ${DRIVER_RAM_ADDR:06X}")
print(f"  File byte offset: {file_byte_offset}")
print(f"  Block index in file: {block_index}")
print(f"  Data offset in block: {data_offset_in_block}")
print(f"  AMOS block number: {AMOSL_START_BLOCK + block_index}")
print(f"  Disk LBA: {AMOSL_START_BLOCK + block_index + 1}")

# ─── Step 3: Write SCZ.DVR to disk image ───
print("\nPatching disk image...")

# Copy original image
shutil.copy2(src_path, dst_path)
with open(dst_path, "r+b") as f:
    patched = bytearray(f.read())

    driver_pos = 0  # Position within SCZ.DVR data
    remaining = len(scz_data)

    current_block_index = block_index
    current_data_offset = data_offset_in_block

    blocks_modified = 0
    while remaining > 0:
        amos_block = AMOSL_START_BLOCK + current_block_index
        lba = amos_block + 1
        disk_offset = lba * 512

        # How many bytes can we write in this block?
        space_in_block = BYTES_PER_BLOCK - current_data_offset
        write_count = min(remaining, space_in_block)

        # Disk position: skip 2-byte link word + data offset
        disk_pos = disk_offset + 2 + current_data_offset

        print(f"  Block {current_block_index} (LBA {lba}): "
              f"writing {write_count} bytes at disk offset ${disk_pos:08X}")

        # Verify link word is intact
        link = read_word_le(patched, disk_offset)
        print(f"    Link word: ${link:04X} (preserving)")

        # Write driver data
        patched[disk_pos:disk_pos + write_count] = scz_data[driver_pos:driver_pos + write_count]

        driver_pos += write_count
        remaining -= write_count
        blocks_modified += 1

        # Next block starts at data offset 0
        current_block_index += 1
        current_data_offset = 0

    print(f"\n  Total: {len(scz_data)} bytes written across {blocks_modified} blocks")

    # Write patched image
    f.seek(0)
    f.write(patched)

print(f"\nPatched image saved to: {dst_path}")

# ─── Step 4: Verify the patch ───
print("\nVerifying patch...")

# Re-read the patched file
with open(dst_path, "rb") as f:
    verify = f.read()

# Read back the data at $7AC2 position
print(f"\nPatched disk content at driver position:")
for blk_idx in range(block_index, block_index + blocks_modified):
    amos_block = AMOSL_START_BLOCK + blk_idx
    lba = amos_block + 1
    offset = lba * 512
    link = read_word_le(verify, offset)

    if blk_idx == block_index:
        start = data_offset_in_block
    else:
        start = 0

    # Show first 16 bytes of data in this block
    data_start = offset + 2 + start
    words = []
    for w in range(0, min(16, 512 - 2 - start), 2):
        val = (verify[data_start + w + 1] << 8) | verify[data_start + w]
        words.append(f"{val:04X}")
    print(f"  LBA {lba} (data+{start}): link=${link:04X} [{' '.join(words)}]")

# ─── Step 5: Boot with patched image and check ───
print(f"\n{'='*60}")
print("STEP 5: BOOT WITH PATCHED IMAGE")
print(f"{'='*60}")

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
cpu.reset()

# Run to scheduler
count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x1250:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"Boot completed at instruction {count}")
print(f"CPU halted: {cpu.halted}, PC: ${cpu.pc:06X}")

# Check driver code at $7AC2
print(f"\nRAM at ${DRIVER_RAM_ADDR:06X} (driver code area):")
all_zero = True
for base in range(DRIVER_RAM_ADDR, DRIVER_RAM_ADDR + 128, 16):
    words = []
    for off in range(0, 16, 2):
        w = bus.read_word(base + off)
        if w != 0:
            all_zero = False
        words.append(f"{w:04X}")
    print(f"  ${base:06X}: {' '.join(words)}")

if all_zero:
    print("\n  *** STILL ALL ZEROS — patch didn't work ***")
else:
    print("\n  *** DRIVER CODE IS NOW PRESENT IN RAM! ***")

    # Verify it matches SCZ.DVR
    match = True
    for i in range(0, min(64, len(scz_data)), 2):
        ram_w = bus.read_word(DRIVER_RAM_ADDR + i)
        scz_w = (scz_data[i+1] << 8) | scz_data[i]
        if ram_w != scz_w:
            print(f"  Mismatch at +${i:04X}: RAM=${ram_w:04X} SCZ=${scz_w:04X}")
            match = False
            break
    if match:
        print(f"  First 64 bytes match SCZ.DVR — patch verified!")

# Check DDT
print(f"\nDDT at $7038:")
for off in [0x00, 0x04, 0x08, 0x0C, 0x34]:
    addr = 0x7038 + off
    val = bus.read_long(addr)
    labels = {0: "status", 4: "driver ptr", 8: "link", 0xC: "device data", 0x34: "name"}
    print(f"  +${off:02X}: ${val:08X} ({labels.get(off, '')})")

print(f"\nDone!")
