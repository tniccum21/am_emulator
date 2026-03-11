#!/usr/bin/env python3
"""Patch SCZ.DVR into AMOSL.MON v4 — fit in 1432-byte boundary.

Strategy:
  - Copy first 1432 bytes of SCZ.DVR to $7AC2
  - Sub2 (18 bytes, called from 7 places) → fits in gap at +$0586
  - Sub1 (80 bytes, called once from +$0204) → NOP out the call
  - Patch JSR(PC) displacements for sub2 calls
"""
import sys
import shutil
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

DRIVER_RAM = 0x7AC2
MAX_COPY = 1432  # $805A - $7AC2
AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

def write_word_le(data, offset, value):
    data[offset] = value & 0xFF
    data[offset+1] = (value >> 8) & 0xFF

# ─── Read SCZ.DVR ───
print("Reading SCZ.DVR...")
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

print(f"  SCZ.DVR: {len(scz_data)} bytes")

# ─── Build patched driver (1432 bytes) ───
driver = bytearray(scz_data[:MAX_COPY])

# 1. Place sub2 (18 bytes) at +$0586 (gap after last RTS at +$0584)
SUB2_SRC = 0x0618  # original offset in SCZ.DVR
SUB2_SIZE = 18     # +$0618 to +$0629 inclusive
SUB2_NEW = 0x0586  # new offset within driver

print(f"\n  Placing sub2 ({SUB2_SIZE} bytes) at +${SUB2_NEW:04X}")
driver[SUB2_NEW:SUB2_NEW + SUB2_SIZE] = scz_data[SUB2_SRC:SUB2_SRC + SUB2_SIZE]

# Verify: sub2 starts with LEA $062A(A0),A1 = $43E8 $062A
w = (driver[SUB2_NEW+1] << 8) | driver[SUB2_NEW]
assert w == 0x43E8, f"Sub2 verify failed: expected $43E8 got ${w:04X}"
print(f"    Verified: first word ${w:04X} (LEA)")

# 2. Patch 7 JSR(PC) calls to sub2: redirect from +$0618 to +$0586
sub2_calls = [
    (0x03B0, 0x03B2, 0x0266),  # (jsr_offset, disp_offset, old_disp)
    (0x040A, 0x040C, 0x020C),
    (0x043C, 0x043E, 0x01DA),
    (0x046C, 0x046E, 0x01AA),
    (0x0490, 0x0492, 0x0186),
    (0x057C, 0x057E, 0x009A),
    (0x0592, 0x0594, 0x0084),
]

print(f"\n  Patching 7 JSR(PC) calls to sub2:")
for jsr_off, disp_off, old_disp in sub2_calls:
    # Verify current displacement
    cur = read_word_le(driver, disp_off)
    assert cur == old_disp, f"Disp verify at +${disp_off:04X}: expected ${old_disp:04X} got ${cur:04X}"

    # New displacement: target +$0586 from displacement word at disp_off
    new_disp = SUB2_NEW - disp_off
    if new_disp < 0:
        new_disp += 0x10000  # Signed 16-bit wraparound

    write_word_le(driver, disp_off, new_disp)
    print(f"    +${disp_off:04X}: ${old_disp:04X} → ${new_disp:04X} (target +${SUB2_NEW:04X})")

# 3. NOP out sub1 call at +$0204 (called once, 80 bytes, can't fit)
print(f"\n  NOP-ing sub1 call at +$0204:")
# Verify: $4EBA at +$0204
w = read_word_le(driver, 0x0204)
assert w == 0x4EBA, f"JSR verify at +$0204: expected $4EBA got ${w:04X}"
# Replace JSR(PC) $03C2 (4 bytes) with NOP NOP
write_word_le(driver, 0x0204, 0x4E71)  # NOP
write_word_le(driver, 0x0206, 0x4E71)  # NOP
print(f"    +$0204: $4EBA $03C2 → $4E71 $4E71 (NOP NOP)")

# ─── Write to disk image ───
print(f"\nWriting patched image...")
shutil.copy2(src_path, dst_path)
with open(dst_path, "r+b") as f:
    patched = bytearray(f.read())

    file_off = DRIVER_RAM
    blk_idx = file_off // BYTES_PER_BLOCK
    data_off = file_off % BYTES_PER_BLOCK

    drv_pos = 0
    remaining = len(driver)
    blocks = 0

    while remaining > 0:
        amos_block = AMOSL_START_BLOCK + blk_idx
        lba = amos_block + 1
        disk_offset = lba * 512

        space = BYTES_PER_BLOCK - data_off
        count = min(remaining, space)
        disk_pos = disk_offset + 2 + data_off

        # Verify link word
        link = read_word_le(patched, disk_offset)
        patched[disk_pos:disk_pos + count] = driver[drv_pos:drv_pos + count]

        drv_pos += count
        remaining -= count
        blocks += 1
        blk_idx += 1
        data_off = 0

    print(f"  Written: {len(driver)} bytes across {blocks} blocks")

    f.seek(0)
    f.write(patched)

# ─── Verify disk data ───
print(f"\nVerifying disk data...")
with open(dst_path, "rb") as f:
    verify = f.read()

# Read back first 16 bytes at driver position
file_off = DRIVER_RAM
blk_idx = file_off // BYTES_PER_BLOCK
data_off = file_off % BYTES_PER_BLOCK
lba = AMOSL_START_BLOCK + blk_idx + 1
disk_pos = lba * 512 + 2 + data_off
verify_words = [read_word_le(verify, disk_pos + i*2) for i in range(8)]
orig_words = [read_word_le(img, disk_pos + i*2) for i in range(8)]
print(f"  Original at driver pos: {' '.join(f'${w:04X}' for w in orig_words)}")
print(f"  Patched  at driver pos: {' '.join(f'${w:04X}' for w in verify_words)}")
scz_words = [read_word_le(scz_data, i*2) for i in range(8)]
print(f"  SCZ.DVR  first 8 words: {' '.join(f'${w:04X}' for w in scz_words)}")

match = verify_words == scz_words
print(f"  Match: {'✓' if match else '✗'}")

# ─── Boot and test ───
print(f"\n{'='*60}")
print("BOOT TEST")
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
acia_output = []
acia.tx_callback = lambda port, val: acia_output.append(chr(val) if 0x20 <= val < 0x7F or val in (0x0D, 0x0A) else f'<{val:02X}>')

cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x1250:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"  Boot result: PC=${cpu.pc:06X} at instr {count}")

# Verify driver in RAM (check multiple words)
print(f"\n  RAM at ${DRIVER_RAM:06X}:")
driver_present = False
for base in range(DRIVER_RAM, DRIVER_RAM + 32, 16):
    words = []
    for off in range(0, 16, 2):
        w = bus.read_word(base + off)
        if w != 0:
            driver_present = True
        words.append(f"{w:04X}")
    print(f"    ${base:06X}: {' '.join(words)}")
print(f"  Driver: {'PRESENT ✓' if driver_present else 'MISSING ✗'}")

# Check sub2 at +$0586
sub2_addr = DRIVER_RAM + SUB2_NEW
w = bus.read_word(sub2_addr)
print(f"  Sub2 at ${sub2_addr:06X}: ${w:04X} ({'✓' if w == 0x43E8 else '✗'})")

# Check $805A preserved
w = bus.read_word(0x805A)
print(f"  $805A: ${w:04X} (preserved: {'✓' if w != 0 else '✗'})")

# DDT
print(f"\n  DDT at $7038:")
for off in [0x00, 0x06, 0x0C, 0x14, 0x34]:
    addr = 0x7038 + off
    w = bus.read_word(addr)
    print(f"    +${off:02X}: ${w:04X}")

# ACIA output
if acia_output:
    text = ''.join(acia_output)
    print(f"\n  ACIA output: {repr(text[:300])}")

print(f"\nDone!")
