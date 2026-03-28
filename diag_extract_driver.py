#!/usr/bin/env python3
"""Extract SCZ.DVR from disk image and analyze its structure.

Goal: Read the SCZ.DVR driver code from disk, understand its format,
and determine how to patch it into AMOSL.MON at $7AC2.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

img_path = Path("images/AMOS_1-3_Boot_OS.img")
with open(img_path, "rb") as f:
    img = bytearray(f.read())

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

def write_word_le(data, offset, value):
    data[offset] = value & 0xFF
    data[offset+1] = (value >> 8) & 0xFF

def rad50_decode(word):
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    c3 = word % 40; word //= 40
    c2 = word % 40; word //= 40
    c1 = word % 40
    return chars[c1] + chars[c2] + chars[c3]

SEP = "=" * 70

# ─── Step 1: Understand the block chain format ───
print(SEP)
print("STEP 1: VERIFY BLOCK CHAIN FORMAT WITH AMOSL.MON")
print(SEP)

# AMOSL.MON starts at block 3257, ROM reads from LBA 3258
# Each sector's word 0 is a link word
print("\nAMOSL.MON block chain (starts at block 3257):")
block = 3257
for i in range(5):
    lba = block + 1  # LBA = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    w1 = read_word_le(img, offset + 2)
    w2 = read_word_le(img, offset + 4)
    print(f"  Block {block} (LBA {lba}): link=${link:04X}={link} data: ${w1:04X} ${w2:04X}")
    if link == 0:
        print(f"  END OF CHAIN")
        break
    block = link

# Check last few blocks of AMOSL.MON
print("\nLast blocks of AMOSL.MON (69 blocks total, starting block 3257):")
for i in range(65, 72):
    block = 3257 + i
    lba = block + 1
    if lba * 512 + 16 > len(img):
        break
    offset = lba * 512
    link = read_word_le(img, offset)
    data_sum = sum(img[offset+2:offset+512])
    print(f"  Block {block} (LBA {lba}): link=${link:04X}={link} data_sum={data_sum}")
    if link == 0:
        print(f"  *** END OF FILE ***")
        break

# ─── Step 2: Find SCZ.DVR in the MFD properly ───
print(f"\n{SEP}")
print("STEP 2: FIND SCZ.DVR IN FILESYSTEM")
print(SEP)

# The MFD entry format (from what we now know):
# word 0-1: filename (RAD50, 6 chars)
# word 2: extension (RAD50, 3 chars)
# word 3: file size in blocks
# word 4: ??? (status/attributes)
# word 5: starting block number

# But wait — we need to verify this against AMOSL.MON:
# Entry at sector 1227:
#   word 3 = $0045 = 69 (matches file size in sectors read by ROM)
#   word 5 = $0CB9 = 3257 (matches starting block)

# Actually, let me look at it differently. There was a "second" AMOSL.MON entry
# at sector 3235 with word 5 = $1B6E = 7022 (different).
# That second entry might be within the loaded OS itself (sector 3235 is within LBAs 3258-3326).
# The entry at sector 1227 is the MFD entry.

# Let me look at the MFD more carefully.
# The ROM scans sector 2 for partition table, finds type $0102 at offset 8.
# From the partition table: type $0102, next word $004C = 76.
# This likely means partition descriptor at block 76 or similar.

print("\nPartition table at sector 2:")
offset = 2 * 512
for i in range(0, 32, 2):
    w = read_word_le(img, offset + i)
    print(f"  +${i:02X}: ${w:04X} = {w}")

# Sector 77 (from the ROM reads: LBA 77 was read)
print("\nSector 77 (LBA 77):")
offset = 77 * 512
for i in range(0, 64, 2):
    w = read_word_le(img, offset + i)
    print(f"  +${i:02X}: ${w:04X} = {w}")

# Sector 79 (also read by ROM)
print("\nSector 79 (LBA 79):")
offset = 79 * 512
for i in range(0, 64, 2):
    w = read_word_le(img, offset + i)
    print(f"  +${i:02X}: ${w:04X} = {w}")

# Sector 340 (also read by ROM — might be SCZ.LIT directory)
print("\nSector 340 (LBA 340):")
offset = 340 * 512
for i in range(0, 64, 2):
    w = read_word_le(img, offset + i)
    print(f"  +${i:02X}: ${w:04X} = {w}")

# Sector 634 (read by ROM)
print("\nSector 634 (LBA 634):")
offset = 634 * 512
for i in range(0, 64, 2):
    w = read_word_le(img, offset + i)
    print(f"  +${i:02X}: ${w:04X} = {w}")

# Sector 868 (read by ROM)
print("\nSector 868 (LBA 868):")
offset = 868 * 512
for i in range(0, 64, 2):
    w = read_word_le(img, offset + i)
    print(f"  +${i:02X}: ${w:04X} = {w}")

# Sector 1227 (read by ROM — MFD with AMOSL.MON entry)
print("\nSector 1227 (LBA 1227) — MFD sector containing AMOSL.MON:")
offset = 1227 * 512
for i in range(0, 128, 2):
    w = read_word_le(img, offset + i)
    if w != 0 or i < 16:
        print(f"  +${i:02X}: ${w:04X} = {w:5d}  {'(' + rad50_decode(w) + ')' if w < 64000 else ''}")

# ─── Step 3: Read SCZ.DVR from disk ───
print(f"\n{SEP}")
print("STEP 3: READ SCZ.DVR FROM DISK")
print(SEP)

# From the MFD search, SCZ.DVR had word 5 = $057A = 1402
# Let's try reading from block 1402

# But first, let me look at the directory structure more carefully
# The entry at sector 1272 offset 458:
#   word 0: $7752 = SCZ
#   word 1: $1C82 = DVR  (this is word 1 of filename, making it "SCZDVR"?)
#   word 2: $1C82 = DVR  (extension)
#   word 3: $0004 = 4     (file size in blocks?)
#   word 4: $0068 = 104   (status?)
#   word 5: $057A = 1402  (starting block)

# File size = 4 blocks? That's only 4 * 510 = 2040 bytes.
# Let's check — read from block 1402

start_block = 1402
print(f"\nReading from block {start_block} (LBA {start_block + 1}):")
block = start_block
driver_data = bytearray()
block_count = 0

while block != 0 and block_count < 100:
    lba = block + 1
    offset = lba * 512
    if offset + 512 > len(img):
        print(f"  Block {block}: PAST END OF IMAGE")
        break

    link = read_word_le(img, offset)
    data = img[offset+2:offset+512]  # 510 bytes of actual data
    driver_data.extend(data)

    # Check for SASI register references
    has_sasi = False
    for p in range(offset, offset + 508):
        if (img[p] == 0xFF and img[p+1] == 0xFF and
            img[p+2] >= 0xE0 and img[p+2] <= 0xE7 and img[p+3] == 0xFF):
            has_sasi = True
            break

    nz_count = sum(1 for b in data if b != 0)
    print(f"  Block {block} (LBA {lba}): link=${link:04X} "
          f"nonzero={nz_count}/510 {'SASI-REF' if has_sasi else ''}")

    block_count += 1
    if link == 0:
        print(f"  END OF CHAIN after {block_count} blocks")
        break
    block = link

print(f"\nTotal SCZ.DVR data: {len(driver_data)} bytes ({len(driver_data)/1024:.1f} KB)")

# Check if any of this looks like 68000 code
if driver_data:
    print(f"\nFirst 128 bytes of SCZ.DVR (as 68000 words):")
    for base in range(0, min(128, len(driver_data)), 16):
        words = []
        for w in range(0, 16, 2):
            if base + w + 1 < len(driver_data):
                val = (driver_data[base+w+1] << 8) | driver_data[base+w]
                words.append(f"{val:04X}")
        print(f"  +${base:03X}: {' '.join(words)}")

# Also try the second SCZ entry at sector 1272 offset 482:
# word 5 = $0582 = 1410
print(f"\n--- Trying second SCZ entry (block 1410) ---")
block = 1410
for i in range(5):
    lba = block + 1
    offset = lba * 512
    if offset + 512 > len(img):
        break
    link = read_word_le(img, offset)
    nz = sum(1 for b in img[offset+2:offset+512] if b != 0)
    has_sasi = False
    for p in range(offset, offset + 508):
        if (img[p] == 0xFF and img[p+1] == 0xFF and
            img[p+2] >= 0xE0 and img[p+2] <= 0xE7 and img[p+3] == 0xFF):
            has_sasi = True
    print(f"  Block {block} (LBA {lba}): link=${link:04X} nz={nz}/510 {'SASI-REF' if has_sasi else ''}")
    if link == 0:
        break
    block = link

# ─── Step 4: Search wider for SCZ.DVR with proper MFD format ───
print(f"\n{SEP}")
print("STEP 4: LOCATE SASI DRIVER CODE DIRECTLY")
print(SEP)

# The earlier analysis found SASI references at sectors 1366 and 1471
# Let's read those sectors and find what file they belong to
for target_sector in [1366, 1471]:
    offset = target_sector * 512
    link = read_word_le(img, offset)
    print(f"\nSector {target_sector} (LBA {target_sector}):")
    print(f"  Link word: ${link:04X} = {link}")

    # Walk backwards in the chain to find the start
    # If blocks are sequential, we can just go back
    # Check if blocks leading up to this are sequential
    prev_block = target_sector - 2  # LBA - 1 = block, block - 1
    if prev_block > 0:
        prev_offset = (prev_block + 1) * 512
        prev_link = read_word_le(img, prev_offset)
        print(f"  Previous block {prev_block} link: ${prev_link:04X}")

    # Show the SASI reference context
    for p in range(offset, offset + 508):
        if (img[p] == 0xFF and img[p+1] == 0xFF and
            img[p+2] >= 0xE0 and img[p+2] <= 0xE7 and img[p+3] == 0xFF):
            ctx_start = max(offset, p - 16)
            ctx_end = min(offset + 512, p + 16)
            words = []
            for w in range(ctx_start, ctx_end, 2):
                words.append(f"${read_word_le(img, w):04X}")
            reg = img[p+2] - 0xE0
            print(f"  SASI ref $FFFFE{reg} at offset +{p - offset}: {' '.join(words)}")

    # Dump first 64 bytes as 68000 words (skip link word)
    print(f"  First 64 data bytes:")
    for base in range(2, 66, 16):
        words = []
        for w in range(0, 16, 2):
            val = read_word_le(img, offset + base + w)
            words.append(f"{val:04X}")
        print(f"    +${base:03X}: {' '.join(words)}")

# ─── Step 5: Try to find the correct file containing sectors 1366/1471 ───
print(f"\n{SEP}")
print("STEP 5: TRACE BLOCK CHAIN TO FIND FILE OWNING SECTORS 1366/1471")
print(SEP)

# Walk backwards from sector 1366 to find file start
# In AMOS, sequential allocation means blocks N, N+1, N+2...
# So trace back: sector 1366 = LBA 1366, block = 1365
# If sequentially allocated, the file starts at block 1365 - N for some N

# Actually, let me trace the chain FORWARD from various starting points
# SCZ.DVR at block 1402 (from MFD entry) - but sectors 1366 and 1471 are BEFORE and AFTER
# Let's check if maybe block numbers work differently

# Perhaps LBA doesn't simply equal block + 1
# Let me check: AMOSL.MON block 3257, ROM reads LBA 3258
# So LBA = block + 1. Let's verify with the link words:
# Block 3257, link = $0CBA = 3258 = next block
# LBA 3258, that's block 3257 with link to block 3258 at LBA 3259

print("\nVerifying LBA/block mapping with AMOSL.MON:")
for blk in range(3257, 3262):
    lba = blk + 1
    off = lba * 512
    link = read_word_le(img, off)
    print(f"  Block {blk} at LBA {lba}: link=${link:04X} (next block {link})")

# So for SCZ.DVR starting at block 1402:
# Block 1402 at LBA 1403
print(f"\nSCZ.DVR chain from block 1402:")
block = 1402
scz_blocks = []
while block != 0 and len(scz_blocks) < 200:
    lba = block + 1
    off = lba * 512
    if off + 512 > len(img):
        break
    link = read_word_le(img, off)
    scz_blocks.append(block)
    has_sasi = any(
        img[p] == 0xFF and img[p+1] == 0xFF and
        img[p+2] >= 0xE0 and img[p+2] <= 0xE7 and img[p+3] == 0xFF
        for p in range(off, off + 508)
    )
    if has_sasi or len(scz_blocks) <= 5 or link == 0:
        print(f"  Block {block} (LBA {lba}): link=${link:04X} {'SASI-REF!' if has_sasi else ''}")
    if link == 0:
        break
    block = link

print(f"  Total blocks: {len(scz_blocks)}")
print(f"  Block range: {min(scz_blocks)}-{max(scz_blocks)}")
print(f"  Contains sector 1366? Block 1365 in chain? {1365 in scz_blocks}")
print(f"  Contains sector 1471? Block 1470 in chain? {1470 in scz_blocks}")

# Check if sectors 1366/1471 (LBAs) correspond to blocks in this chain
# LBA 1366 = block 1365
# LBA 1471 = block 1470
for target_lba in [1366, 1471]:
    target_block = target_lba - 1
    if target_block in scz_blocks:
        idx = scz_blocks.index(target_block)
        print(f"  YES: LBA {target_lba} = block {target_block} is entry #{idx} in SCZ.DVR")
    else:
        print(f"  NO: LBA {target_lba} = block {target_block} is NOT in SCZ.DVR chain")

# ─── Step 6: Read the full SCZ.DVR content ───
print(f"\n{SEP}")
print("STEP 6: FULL SCZ.DVR CONTENT")
print(SEP)

# Re-read following the chain
scz_data = bytearray()
block = 1402
while block != 0:
    lba = block + 1
    off = lba * 512
    if off + 512 > len(img):
        break
    link = read_word_le(img, off)
    scz_data.extend(img[off+2:off+512])
    if link == 0:
        break
    block = link

print(f"SCZ.DVR total data: {len(scz_data)} bytes ({len(scz_data)/1024:.1f} KB)")

# Search for SASI register references within the extracted file
print(f"\nSASI register references in SCZ.DVR:")
for pos in range(0, len(scz_data) - 3):
    if (scz_data[pos] == 0xFF and scz_data[pos+1] == 0xFF and
        scz_data[pos+2] >= 0xE0 and scz_data[pos+2] <= 0xE7 and scz_data[pos+3] == 0xFF):
        reg = scz_data[pos+2] - 0xE0
        w_val = (scz_data[pos+1] << 8) | scz_data[pos]
        w_next = (scz_data[pos+3] << 8) | scz_data[pos+2]
        # Show context
        ctx_start = max(0, pos - 8)
        ctx_end = min(len(scz_data), pos + 12)
        words = []
        for w in range(ctx_start, ctx_end, 2):
            words.append(f"${(scz_data[w+1] << 8) | scz_data[w]:04X}")
        print(f"  +${pos:04X}: $FFFFE{reg} ({' '.join(words)})")

# Also search for absolute short $FFEx references
print(f"\nAbsolute short $FFEx references in SCZ.DVR:")
for pos in range(0, len(scz_data) - 1, 2):
    w = (scz_data[pos+1] << 8) | scz_data[pos]
    if 0xFFE0 <= w <= 0xFFE7:
        if pos >= 2:
            prev = (scz_data[pos-1] << 8) | scz_data[pos-2]
            if prev != 0xFFFF:  # Not part of absolute long
                print(f"  +${pos:04X}: ${w:04X} (prev: ${prev:04X})")

# Dump the entire SCZ.DVR as hex
print(f"\nFull SCZ.DVR hex dump:")
for base in range(0, len(scz_data), 16):
    words = []
    any_nz = False
    for w in range(0, 16, 2):
        if base + w + 1 < len(scz_data):
            val = (scz_data[base+w+1] << 8) | scz_data[base+w]
            if val != 0:
                any_nz = True
            words.append(f"{val:04X}")
    if any_nz or base < 64:
        ascii_str = ""
        for b in range(min(16, len(scz_data) - base)):
            ch = scz_data[base + b]
            ascii_str += chr(ch) if 32 <= ch < 127 else "."
        print(f"  ${base:04X}: {' '.join(words)}  {ascii_str}")

print(f"\n{SEP}")
print("ANALYSIS COMPLETE")
print(SEP)
