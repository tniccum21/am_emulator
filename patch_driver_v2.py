#!/usr/bin/env python3
"""Patch SCZ.DVR into AMOSL.MON — v2: respects $805A boundary.

The zero region at $7AC2 is exactly 1432 bytes ($7AC2-$8059).
SCZ.DVR is 2040 bytes total, 1631 effective. We can only copy 1432 bytes.

This script:
1. Extracts SCZ.DVR from disk
2. Scans for cross-boundary references (BSR/JSR/JMP beyond +$0598)
3. Copies only the first 1432 bytes to $7AC2
4. Boots and traces driver execution
"""
import sys
import shutil
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

MAX_DRIVER_SIZE = 1432  # $805A - $7AC2 = $0598

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# ─── Step 1: Read SCZ.DVR from disk ───
print("Step 1: Reading SCZ.DVR...")
with open(src_path, "rb") as f:
    img = bytearray(f.read())

scz_data = bytearray()
block = 1402
block_count = 0
while block != 0 and block_count < 100:
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    scz_data.extend(img[offset+2:offset+512])
    block_count += 1
    if link == 0:
        break
    block = link

print(f"  SCZ.DVR: {len(scz_data)} bytes, max copy: {MAX_DRIVER_SIZE} bytes")

# ─── Step 2: Scan for cross-boundary references ───
print(f"\nStep 2: Scanning for references beyond +${MAX_DRIVER_SIZE:04X}...")

# Scan for BSR.W ($6100), BSR.S ($61xx), JMP/JSR (d16,PC) ($4EFA/$4EBA)
cross_refs = []
for pos in range(0, min(MAX_DRIVER_SIZE, len(scz_data)) - 1, 2):
    w = (scz_data[pos+1] << 8) | scz_data[pos]

    # BSR.S ($61xx where xx != 0)
    if (w & 0xFF00) == 0x6100 and (w & 0xFF) != 0:
        disp = w & 0xFF
        if disp >= 0x80:
            disp -= 256
        target = pos + 2 + disp
        if target >= MAX_DRIVER_SIZE:
            cross_refs.append(('BSR.S', pos, target))

    # BSR.W ($6100 $xxxx)
    elif w == 0x6100 and pos + 3 < len(scz_data):
        disp_w = (scz_data[pos+3] << 8) | scz_data[pos+2]
        if disp_w >= 0x8000:
            disp_w -= 0x10000
        target = pos + 2 + disp_w
        if target >= MAX_DRIVER_SIZE:
            cross_refs.append(('BSR.W', pos, target))

    # JMP (d16,PC) = $4EFA $xxxx
    elif w == 0x4EFA and pos + 3 < len(scz_data):
        disp_w = (scz_data[pos+3] << 8) | scz_data[pos+2]
        if disp_w >= 0x8000:
            disp_w -= 0x10000
        target = pos + 2 + disp_w
        if target >= MAX_DRIVER_SIZE:
            cross_refs.append(('JMP(PC)', pos, target))

    # JSR (d16,PC) = $4EBA $xxxx
    elif w == 0x4EBA and pos + 3 < len(scz_data):
        disp_w = (scz_data[pos+3] << 8) | scz_data[pos+2]
        if disp_w >= 0x8000:
            disp_w -= 0x10000
        target = pos + 2 + disp_w
        if target >= MAX_DRIVER_SIZE:
            cross_refs.append(('JSR(PC)', pos, target))

if cross_refs:
    print(f"  WARNING: {len(cross_refs)} cross-boundary references found!")
    for kind, src, tgt in cross_refs:
        print(f"    {kind} at +${src:04X} → +${tgt:04X} (${tgt + 0x7AC2:06X})")
else:
    print(f"  No cross-boundary references found — safe to truncate!")

# Show RTS locations for context
print(f"\n  RTS locations in SCZ.DVR:")
for pos in range(0, len(scz_data) - 1, 2):
    w = (scz_data[pos+1] << 8) | scz_data[pos]
    if w == 0x4E75:
        marker = " *** BEYOND BOUNDARY ***" if pos >= MAX_DRIVER_SIZE else ""
        print(f"    +${pos:04X} ({pos}){marker}")

# ─── Step 3: Create patched disk image ───
print(f"\nStep 3: Patching disk image (max {MAX_DRIVER_SIZE} bytes)...")

AMOSL_START_BLOCK = 3257
DRIVER_RAM_ADDR = 0x7AC2
BYTES_PER_BLOCK = 510

# Truncate driver data to fit
driver_data = scz_data[:MAX_DRIVER_SIZE]
print(f"  Copying {len(driver_data)} bytes of SCZ.DVR to ${DRIVER_RAM_ADDR:06X}")

file_byte_offset = DRIVER_RAM_ADDR
block_index = file_byte_offset // BYTES_PER_BLOCK
data_offset_in_block = file_byte_offset % BYTES_PER_BLOCK

# Follow the ACTUAL block chain to find correct disk positions
print(f"\n  Following AMOSL.MON block chain to find sector positions...")
chain = []
blk = AMOSL_START_BLOCK
for i in range(69):
    lba = blk + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    chain.append((blk, lba, offset))
    if link == 0:
        break
    blk = link

print(f"  Chain has {len(chain)} blocks")
print(f"  Need blocks {block_index} through {block_index + 4}")

shutil.copy2(src_path, dst_path)
with open(dst_path, "r+b") as f:
    patched = bytearray(f.read())

    driver_pos = 0
    remaining = len(driver_data)
    cur_block_idx = block_index
    cur_data_off = data_offset_in_block
    blocks_modified = 0

    while remaining > 0 and cur_block_idx < len(chain):
        blk_num, lba, disk_offset = chain[cur_block_idx]

        space_in_block = BYTES_PER_BLOCK - cur_data_off
        write_count = min(remaining, space_in_block)
        disk_pos = disk_offset + 2 + cur_data_off

        # Verify link word
        link = read_word_le(patched, disk_offset)
        print(f"  Block {cur_block_idx} (AMOS {blk_num}, LBA {lba}): "
              f"{write_count} bytes at disk ${disk_pos:08X}, link=${link:04X}")

        patched[disk_pos:disk_pos + write_count] = driver_data[driver_pos:driver_pos + write_count]

        driver_pos += write_count
        remaining -= write_count
        blocks_modified += 1
        cur_block_idx += 1
        cur_data_off = 0

    print(f"\n  Total: {len(driver_data)} bytes across {blocks_modified} blocks")

    f.seek(0)
    f.write(patched)

print(f"  Saved: {dst_path}")

# ─── Step 4: Boot and verify ───
print(f"\n{'='*60}")
print("Step 4: BOOT WITH CORRECTED PATCH")
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

# Capture ACIA output
acia_output = []
acia.tx_callback = lambda port, val: acia_output.append(chr(val) if 0x20 <= val < 0x7F or val in (0x0D, 0x0A) else f'<{val:02X}>')

cpu.reset()

# Track driver execution
DRIVER_BASE = 0x7AC2
DRIVER_END = DRIVER_BASE + MAX_DRIVER_SIZE
driver_entered = False
driver_entry_count = 0
driver_stuck_pc = None
last_pc = None
pc_repeat = 0

count = 0
while not cpu.halted and count < 10_000_000:
    pc = cpu.pc

    if DRIVER_BASE <= pc < DRIVER_END:
        if not driver_entered:
            driver_entered = True
            driver_entry_count += 1
            if driver_entry_count <= 3:
                print(f"\n  Driver entry #{driver_entry_count} at instr {count}, PC=${pc:06X} (+${pc-DRIVER_BASE:04X})")
                print(f"    D0=${cpu.d[0]:08X} A0=${cpu.a[0]:08X} A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X}")

        if pc == last_pc:
            pc_repeat += 1
            if pc_repeat == 200:
                driver_stuck_pc = pc
                print(f"\n  STUCK at PC=${pc:06X} (+${pc-DRIVER_BASE:04X}) at instr {count}")
                print(f"    D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D7=${cpu.d[7]:08X}")
                print(f"    A0=${cpu.a[0]:08X} A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X}")
                # Show the loop
                for addr in range(max(DRIVER_BASE, pc - 12), min(DRIVER_END, pc + 12), 2):
                    w = bus.read_word(addr)
                    m = " <--" if addr == pc else ""
                    print(f"    ${addr:06X}: ${w:04X}{m}")
                break
        else:
            pc_repeat = 0
        last_pc = pc
    else:
        if driver_entered:
            driver_entered = False

    if pc == 0x1250:
        print(f"\n  Scheduler reached at instruction {count}")
        break

    cpu.step()
    bus.tick(1)
    count += 1

if not cpu.halted and driver_stuck_pc is None and cpu.pc != 0x1250:
    print(f"\n  Stopped at instruction limit, PC=${cpu.pc:06X}")

# Verify driver in RAM
print(f"\nRAM verification:")
driver_present = False
for i in range(0, 32, 2):
    w = bus.read_word(DRIVER_BASE + i)
    if w != 0:
        driver_present = True
        break

if driver_present:
    print(f"  Driver code present at ${DRIVER_BASE:06X} ✓")
else:
    print(f"  Driver code MISSING at ${DRIVER_BASE:06X} ✗")

# Check $805A is intact (A-line handler)
w805a = bus.read_word(0x805A)
print(f"  Code at $805A: ${w805a:04X} (should NOT be driver data)")

# DDT state
print(f"\nDDT at $7038:")
for off in [0x00, 0x04, 0x06, 0x0C, 0x34]:
    addr = 0x7038 + off
    w = bus.read_word(addr)
    labels = {0: "status", 4: "drv ptr hi", 6: "drv ptr lo", 0xC: "dev data", 0x34: "name"}
    print(f"  +${off:02X}: ${w:04X} ({labels.get(off, '')})")

# Show ACIA output
if acia_output:
    text = ''.join(acia_output)
    print(f"\nACIA output ({len(acia_output)} chars):")
    for line in text.split('\n')[:20]:
        print(f"  {line}")

print(f"\nDone!")
