#!/usr/bin/env python3
"""Comprehensive AMOS filesystem analysis to locate AMOSL.MON and SCZ.DVR.

Key questions:
1. What is AMOSL.MON's actual file size in blocks?
2. Does the ROM bootstrap load ALL of AMOSL.MON, or only part?
3. Is SCZ.DVR code embedded within AMOSL.MON on disk?
4. Where exactly on disk is the data that maps to RAM address $7AC2?
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

img_path = Path("images/AMOS_1-3_Boot_OS.img")
with open(img_path, "rb") as f:
    img = f.read()

def rad50_decode(word):
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    c3 = word % 40; word //= 40
    c2 = word % 40; word //= 40
    c1 = word % 40
    return chars[c1] + chars[c2] + chars[c3]

def read_word_le(data, offset):
    """Read a little-endian 16-bit word (as stored on disk)."""
    return (data[offset+1] << 8) | data[offset]

def read_long_le(data, offset):
    """Read two LE words as a 32-bit longword (word0 = low, word1 = high)."""
    lo = read_word_le(data, offset)
    hi = read_word_le(data, offset + 2)
    return (hi << 16) | lo

print(f"{'='*70}")
print(f"AMOS FILESYSTEM ANALYSIS — {img_path.name}")
print(f"{'='*70}")
print(f"Image size: {len(img)} bytes ({len(img)//512} sectors)")

# ─── Step 1: Find the MFD (Master File Directory) ───
# The ROM scans for partition type 258 ($0102) and 260 ($0104)
# MFD is typically at sector 2 or near the beginning
# Let's scan for MFD entries by looking for RAD50-encoded filenames

print(f"\n{'='*70}")
print(f"STEP 1: SCAN FOR MFD/UFD ENTRIES")
print(f"{'='*70}")

# AMOS MFD entry format (approximate, varies by version):
# Word 0-1: filename (2 RAD50 words = 6 chars)
# Word 2: extension (1 RAD50 word = 3 chars)
# Word 3-N: attributes, starting block, size, etc.

# Search for AMOSL.MON entries
amo_r50 = 0x0857  # "AMO"
sl_r50 = 0x78A0   # "SL "
mon_r50 = 0x53A6  # "MON"
scz_r50 = 0x7752  # "SCZ"
dvr_r50 = 0x1C82  # "DVR"

print(f"\nSearching for AMOSL.MON (RAD50: ${amo_r50:04X} ${sl_r50:04X} .${mon_r50:04X})...")
amosl_entries = []
for pos in range(0, len(img) - 12, 2):
    w0 = read_word_le(img, pos)
    if w0 == amo_r50:
        w1 = read_word_le(img, pos + 2)
        if w1 == sl_r50:
            w2 = read_word_le(img, pos + 4)
            if w2 == mon_r50:
                sector = pos // 512
                off_in_sector = pos % 512
                # Read surrounding words for MFD entry context
                entry = []
                for w in range(0, 20, 2):
                    if pos + w + 1 < len(img):
                        entry.append(read_word_le(img, pos + w))
                amosl_entries.append((pos, sector, off_in_sector, entry))
                print(f"\n  Found at offset ${pos:06X} (sector {sector}, +{off_in_sector}):")
                for i, w in enumerate(entry):
                    decoded = rad50_decode(w) if w < 64000 else "---"
                    print(f"    word {i}: ${w:04X} = {w:5d}  (RAD50: '{decoded}')")

print(f"\nSearching for SCZ.DVR (RAD50: ${scz_r50:04X} .${dvr_r50:04X})...")
scz_entries = []
for pos in range(0, len(img) - 8, 2):
    w0 = read_word_le(img, pos)
    if w0 == scz_r50:
        w2 = read_word_le(img, pos + 4)
        if w2 == dvr_r50:
            w1 = read_word_le(img, pos + 2)
            sector = pos // 512
            off_in_sector = pos % 512
            entry = []
            for w in range(0, 20, 2):
                if pos + w + 1 < len(img):
                    entry.append(read_word_le(img, pos + w))
            scz_entries.append((pos, sector, off_in_sector, entry))
            print(f"\n  Found at offset ${pos:06X} (sector {sector}, +{off_in_sector}):")
            for i, w in enumerate(entry):
                decoded = rad50_decode(w) if w < 64000 else "---"
                print(f"    word {i}: ${w:04X} = {w:5d}  (RAD50: '{decoded}')")

# ─── Step 2: Determine AMOSL.MON file boundaries on disk ───
print(f"\n{'='*70}")
print(f"STEP 2: AMOS PARTITION AND FILE LAYOUT")
print(f"{'='*70}")

# The ROM's partition scan looks for type 258 ($0102) at specific offsets
# Let's find the disk header / volume label structure
# Scan for the partition table marker $0102 (type 258)
print(f"\nScanning for partition type markers...")
for pos in range(0, min(len(img), 0x10000), 2):
    w = read_word_le(img, pos)
    if w == 0x0102 or w == 0x0104:
        sector = pos // 512
        off = pos % 512
        # Show surrounding context
        ctx = []
        for c in range(-8, 16, 2):
            if 0 <= pos + c < len(img) - 1:
                ctx.append(f"${read_word_le(img, pos + c):04X}")
        print(f"  Type ${w:04X} at offset ${pos:06X} (sector {sector}, +{off}): {' '.join(ctx)}")

# ─── Step 3: Trace where ROM bootstrap reads from ───
print(f"\n{'='*70}")
print(f"STEP 3: TRACE ROM BOOTSTRAP — FULL SECTOR MAP")
print(f"{'='*70}")

from alphasim.config import SystemConfig
from alphasim.main import build_system
import types

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

# Find SASI device
sasi = None
for start, end, dev in bus._devices:
    if start == 0xFFFFE0:
        sasi = dev
        break

# Track sector reads AND where data gets written in RAM
reads = []
instruction_count = 0

orig_do_read = sasi._do_read_sector

def tracked_read(self):
    global instruction_count
    track = self._sno
    sector = self._sct
    head = (self._sdh >> 4) & 1
    spt = 10
    physical = track * spt + (max(sector, 1) - 1)
    lba = physical * 2 + head + 1
    reads.append({
        'track': track, 'sector': sector, 'head': head, 'lba': lba,
        'instruction': instruction_count,
        'disk_offset': lba * 512,
    })
    return orig_do_read()

sasi._do_read_sector = types.MethodType(tracked_read, sasi)

cpu.reset()
instruction_count = 0

# Run to scheduler
while not cpu.halted and instruction_count < config.max_instructions:
    if cpu.pc == 0x1250:
        break
    cpu.step()
    bus.tick(1)
    instruction_count += 1

print(f"Boot complete at instruction {instruction_count}")
print(f"Total sector reads: {len(reads)}")

if reads:
    lbas = [r['lba'] for r in reads]
    unique_lbas = sorted(set(lbas))
    print(f"Unique LBAs: {len(unique_lbas)}")
    print(f"LBA range: {min(lbas)} to {max(lbas)}")

    # Group reads into contiguous ranges
    ranges = []
    start = unique_lbas[0]
    end = unique_lbas[0]
    for lba in unique_lbas[1:]:
        if lba == end + 1:
            end = lba
        else:
            ranges.append((start, end))
            start = lba
            end = lba
    ranges.append((start, end))

    print(f"\nContiguous read ranges:")
    for s, e in ranges:
        count = e - s + 1
        disk_start = s * 512
        disk_end = (e + 1) * 512
        print(f"  LBA {s:4d}-{e:4d} ({count:3d} sectors, {count*512:6d} bytes) "
              f"disk ${disk_start:08X}-${disk_end:08X}")

    # Total data loaded
    total_bytes = len(unique_lbas) * 512
    print(f"\nTotal unique data loaded: {total_bytes} bytes ({total_bytes/1024:.1f} KB)")

# ─── Step 4: Check AMOSL.MON's extent on disk ───
print(f"\n{'='*70}")
print(f"STEP 4: AMOSL.MON CONTENT ANALYSIS")
print(f"{'='*70}")

# From the boot trace, AMOSL.MON is loaded from certain LBAs
# Let's figure out which sectors contain OS code and where
# The ROM loads OS to RAM starting at $000000

# Show where the data ends up in RAM
# The ROM copies sector data to RAM in sequential order
# Starting from the first OS sector, each sector maps to the next 512 bytes of RAM

# Let's find what read range corresponds to AMOSL.MON
# Typically: first few reads are boot block/partition scan
# Then a contiguous block of reads is the AMOSL.MON load

if len(ranges) > 1:
    # The largest contiguous range is likely AMOSL.MON
    largest = max(ranges, key=lambda r: r[1] - r[0])
    os_start_lba, os_end_lba = largest
    os_sectors = os_end_lba - os_start_lba + 1
    print(f"\nLargest contiguous read range (likely AMOSL.MON):")
    print(f"  LBAs {os_start_lba} to {os_end_lba} = {os_sectors} sectors ({os_sectors*512} bytes)")

    # If OS loads at RAM $000000, then:
    # Sector 0 of AMOSL.MON → $000000-$0001FF
    # Sector 1 → $000200-$0003FF
    # ...
    # $7AC2 falls at offset $7AC2 within the file → sector $7AC2/512 = 61 (sector offset within file)
    file_sector_for_7AC2 = 0x7AC2 // 512
    byte_offset_for_7AC2 = 0x7AC2 % 512
    print(f"\n  $7AC2 falls in file sector {file_sector_for_7AC2} (byte offset {byte_offset_for_7AC2} within sector)")
    print(f"  That's disk LBA {os_start_lba + file_sector_for_7AC2}")

    target_lba = os_start_lba + file_sector_for_7AC2
    if target_lba <= os_end_lba:
        print(f"  *** LBA {target_lba} IS within loaded range (loaded up to LBA {os_end_lba}) ***")
        # Show what's on disk at that location
        disk_offset = target_lba * 512
        print(f"\n  Disk content at LBA {target_lba} (offset ${disk_offset:08X}):")
        for base in range(disk_offset, disk_offset + 512, 16):
            words = []
            any_nz = False
            for w in range(0, 16, 2):
                val = read_word_le(img, base + w)
                if val != 0:
                    any_nz = True
                words.append(f"{val:04X}")
            if any_nz:
                print(f"    ${base:08X}: {' '.join(words)}")

        # Check around $7AC2 specifically
        target_disk_offset = disk_offset + byte_offset_for_7AC2
        print(f"\n  Disk bytes at $7AC2 equivalent (disk offset ${target_disk_offset:08X}):")
        for off in range(0, 64, 16):
            if target_disk_offset + off + 15 < len(img):
                words = []
                for w in range(0, 16, 2):
                    val = read_word_le(img, target_disk_offset + off + w)
                    words.append(f"{val:04X}")
                print(f"    +${off:02X}: {' '.join(words)}")
    else:
        print(f"  *** LBA {target_lba} is PAST loaded range (max LBA {os_end_lba}) ***")
        print(f"  ROM only loads {os_sectors} sectors but file offset needs sector {file_sector_for_7AC2}")
        # Check if there's data on disk past the loaded range
        for check_lba in range(os_end_lba + 1, os_end_lba + 20):
            off = check_lba * 512
            if off + 512 <= len(img):
                is_zero = all(img[off + i] == 0 for i in range(512))
                if not is_zero:
                    w0 = read_word_le(img, off)
                    w1 = read_word_le(img, off + 2)
                    print(f"    LBA {check_lba}: first words ${w0:04X} ${w1:04X} (NON-ZERO)")
                else:
                    print(f"    LBA {check_lba}: all zeros")

# ─── Step 5: Check what's on disk at every AMOSL.MON sector ───
print(f"\n{'='*70}")
print(f"STEP 5: AMOSL.MON — SEARCHING FOR SASI REFERENCES WITHIN LOADED RANGE")
print(f"{'='*70}")

# Search within the AMOSL.MON disk range for SASI register references
if len(ranges) > 1:
    os_start_lba, os_end_lba = largest
    print(f"\nSearching LBAs {os_start_lba}-{os_end_lba} for $FFFFE0-$FFFFE7 references...")

    for lba in range(os_start_lba, os_end_lba + 1):
        offset = lba * 512
        for pos in range(offset, offset + 508):
            # Look for absolute long $FFFFFFEx pattern
            # In LE words: $FFFF $FFE0-$FFE7
            # Bytes: FF FF E0-E7 FF
            if (img[pos] == 0xFF and img[pos+1] == 0xFF and
                img[pos+2] >= 0xE0 and img[pos+2] <= 0xE7 and img[pos+3] == 0xFF):
                file_offset = lba - os_start_lba  # Sector within AMOSL.MON
                ram_addr = file_offset * 512 + (pos - offset)
                reg = img[pos+2] - 0xE0
                print(f"  LBA {lba} (file sector {file_offset}): "
                      f"$FFFFE{reg} at disk+${pos:06X} → RAM ${ram_addr:06X}")
                # Show context (4 words before and after)
                ctx_start = max(offset, pos - 8)
                ctx_end = min(offset + 512, pos + 12)
                words = []
                for w in range(ctx_start, ctx_end, 2):
                    words.append(f"${read_word_le(img, w):04X}")
                print(f"    Context: {' '.join(words)}")

    # Also search for absolute short form ($FFE0-$FFE7 as sign-extended word)
    # In 68000, absolute short .W addressing: word $FFE0 sign-extends to $FFFFFFFFE0
    print(f"\n  Also searching for absolute short $FFEx references in AMOSL.MON range...")
    short_refs = []
    for lba in range(os_start_lba, os_end_lba + 1):
        offset = lba * 512
        for pos in range(offset, offset + 510, 2):
            w = read_word_le(img, pos)
            if 0xFFE0 <= w <= 0xFFE7:
                # Check preceding word for likely 68000 opcode
                if pos >= offset + 2:
                    prev_w = read_word_le(img, pos - 2)
                    # Skip if previous is also $FFxx (likely part of absolute long)
                    if prev_w != 0xFFFF:
                        file_sector = lba - os_start_lba
                        ram_addr = file_sector * 512 + (pos - offset)
                        reg = w - 0xFFE0
                        short_refs.append((lba, file_sector, ram_addr, reg, prev_w))

    if short_refs:
        print(f"  Found {len(short_refs)} absolute short $FFEx references:")
        for lba, fsec, ram, reg, prev in short_refs[:30]:
            print(f"    LBA {lba} (sector {fsec}): $FFE{reg} at RAM ${ram:06X} (prev opcode ${prev:04X})")
    else:
        print(f"  No absolute short $FFEx references found in loaded AMOSL.MON range")

# ─── Step 6: DDT structure in RAM ───
print(f"\n{'='*70}")
print(f"STEP 6: DDT AT $7038 — EXAMINING DRIVER POINTER CHAIN")
print(f"{'='*70}")

# Read DDT structure from RAM
print(f"DDT dump:")
for off in range(0, 0x90, 2):
    addr = 0x7038 + off
    w = bus.read_word(addr)
    if w != 0 or off < 0x10 or off == 0x34 or off == 0x36:
        labels = {
            0x00: "status/capability (word 0)", 0x02: "status/capability (word 1)",
            0x04: "driver code ptr (hi)", 0x06: "driver code ptr (lo)",
            0x08: "link (hi)", 0x0A: "link (lo)",
            0x0C: "device data (hi)", 0x0E: "device data (lo)",
            0x34: "RAD50 name word 1", 0x36: "RAD50 name word 2",
        }
        label = labels.get(off, "")
        extra = ""
        if off == 0x34 or off == 0x36:
            if w < 64000:
                extra = f" = '{rad50_decode(w)}'"
        print(f"  +${off:02X} (${addr:06X}): ${w:04X}{' ' + label if label else ''}{extra}")

driver_ptr = bus.read_long(0x703C)
print(f"\nDriver code pointer (longword at $703C): ${driver_ptr:08X}")

# Check what's on disk at the equivalent file offset for $7AC2
if driver_ptr > 0 and driver_ptr < 0x100000 and len(ranges) > 1:
    file_sector = driver_ptr // 512
    byte_in_sector = driver_ptr % 512
    disk_lba = os_start_lba + file_sector
    disk_offset = disk_lba * 512 + byte_in_sector

    print(f"\n  File sector for ${driver_ptr:06X}: {file_sector}")
    print(f"  Disk LBA: {disk_lba}")
    print(f"  Disk offset: ${disk_offset:08X}")

    if disk_offset + 128 < len(img):
        print(f"\n  ON-DISK content at driver pointer equivalent:")
        all_zero = True
        for base in range(0, 128, 16):
            words = []
            for w in range(0, 16, 2):
                val = read_word_le(img, disk_offset + base + w)
                if val != 0:
                    all_zero = False
                words.append(f"{val:04X}")
            print(f"    ${driver_ptr + base:06X}: {' '.join(words)}")

        if all_zero:
            print(f"\n  *** DISK DATA IS ALSO ALL ZEROS AT ${driver_ptr:06X} ***")
            print(f"  *** This confirms MONGEN was NOT run — driver not embedded ***")
        else:
            print(f"\n  *** NON-ZERO DATA ON DISK — driver MAY exist but isn't reaching RAM ***")

# ─── Step 7: Wider search for any non-zero data past loaded OS ───
print(f"\n{'='*70}")
print(f"STEP 7: AMOSL.MON FILE SIZE FROM MFD ENTRIES")
print(f"{'='*70}")

# Try to interpret MFD entries to find file size
# AMOS MFD format varies, but typically:
# Words 0-1: filename (RAD50)
# Word 2: extension (RAD50)
# Word 3: file status/protection bits
# Word 4: starting block number
# Word 5: file size in blocks
# (This is approximate - exact format depends on AMOS version)

for pos, sector, off_in_sector, entry in amosl_entries:
    if len(entry) >= 8:
        print(f"\nAMOSL.MON entry at sector {sector}:")
        print(f"  Filename: {rad50_decode(entry[0])}{rad50_decode(entry[1])}.{rad50_decode(entry[2])}")
        print(f"  Word 3 (status?): ${entry[3]:04X} = {entry[3]}")
        print(f"  Word 4 (start block?): ${entry[4]:04X} = {entry[4]}")
        print(f"  Word 5 (size blocks?): ${entry[5]:04X} = {entry[5]}")
        print(f"  Word 6: ${entry[6]:04X} = {entry[6]}")
        print(f"  Word 7: ${entry[7]:04X} = {entry[7]}")
        if len(entry) >= 10:
            print(f"  Word 8: ${entry[8]:04X} = {entry[8]}")
            print(f"  Word 9: ${entry[9]:04X} = {entry[9]}")

        # Assume word 5 = size in 512-byte blocks
        if entry[5] > 0 and entry[5] < 10000:
            size_bytes = entry[5] * 512
            print(f"\n  If word 5 = file size in blocks:")
            print(f"    {entry[5]} blocks × 512 = {size_bytes} bytes ({size_bytes/1024:.1f} KB)")
            print(f"    Would reach RAM address ${size_bytes:06X}")
            if size_bytes > driver_ptr:
                print(f"    *** File extends past driver ptr ${driver_ptr:06X} ***")
            else:
                print(f"    *** File DOES NOT reach driver ptr ${driver_ptr:06X} ***")

        # Also try word 4 as start block, word 5 as word count
        if entry[4] > 0:
            print(f"\n  If word 4 = starting block {entry[4]}:")
            print(f"    Disk offset of file start: ${entry[4] * 512:08X}")

# ─── Step 8: RAM comparison with disk ───
print(f"\n{'='*70}")
print(f"STEP 8: VERIFY RAM MATCHES DISK DATA")
print(f"{'='*70}")

# Verify that what's in RAM actually matches what's on disk
# This tells us if the ROM loaded the data correctly
if len(ranges) > 1:
    os_start_lba, os_end_lba = largest
    print(f"\nComparing RAM content with disk data (first 40 sectors of AMOSL.MON)...")
    mismatches = 0
    for sec in range(min(40, os_end_lba - os_start_lba + 1)):
        disk_offset = (os_start_lba + sec) * 512
        ram_addr = sec * 512
        match = True
        for w_off in range(0, 512, 2):
            disk_word = read_word_le(img, disk_offset + w_off)
            ram_word = bus.read_word(ram_addr + w_off)
            if disk_word != ram_word:
                if match:  # First mismatch in this sector
                    print(f"  Sector {sec} (LBA {os_start_lba + sec}): "
                          f"MISMATCH at offset +${w_off:03X}: "
                          f"disk=${disk_word:04X} ram=${ram_word:04X}")
                    match = False
                    mismatches += 1
        if match and sec < 5:
            print(f"  Sector {sec} (LBA {os_start_lba + sec}): OK")

    if mismatches == 0:
        print(f"  All 40 sectors match — ROM loaded correctly")
    else:
        print(f"  {mismatches} sectors have mismatches")

    # Now specifically check the sector containing $7AC2
    target_sec = driver_ptr // 512
    if target_sec < os_end_lba - os_start_lba + 1:
        disk_offset = (os_start_lba + target_sec) * 512
        print(f"\n  Sector containing driver ptr ${driver_ptr:06X} (file sector {target_sec}):")
        match = True
        for w_off in range(0, 512, 2):
            disk_word = read_word_le(img, disk_offset + w_off)
            ram_word = bus.read_word(target_sec * 512 + w_off)
            if disk_word != ram_word:
                print(f"    MISMATCH at +${w_off:03X}: disk=${disk_word:04X} ram=${ram_word:04X}")
                match = False
        if match:
            print(f"    Sector matches disk — disk also has zeros here")

# ─── Step 9: Find ALL file entries in MFD ───
print(f"\n{'='*70}")
print(f"STEP 9: FULL MFD DIRECTORY LISTING")
print(f"{'='*70}")

# Scan the sectors that look like MFD entries (containing RAD50 filenames)
# MFD entries are typically 32 bytes each, 16 per sector
# Let's look for sectors with multiple valid RAD50 entries

# First find a sector with an AMOSL entry and scan that area
if amosl_entries:
    mfd_sector = amosl_entries[0][1]
    print(f"\nMFD area around sector {mfd_sector}:")

    # Scan nearby sectors for directory entries
    for scan_sector in range(max(0, mfd_sector - 5), mfd_sector + 20):
        offset = scan_sector * 512
        if offset + 512 > len(img):
            break

        # Try reading as 32-byte MFD entries (16 entries per sector)
        valid_entries = 0
        entries_text = []
        for entry_idx in range(16):
            entry_off = offset + entry_idx * 32
            w0 = read_word_le(img, entry_off)
            w1 = read_word_le(img, entry_off + 2)
            w2 = read_word_le(img, entry_off + 4)

            # Check if this looks like a valid filename (all words < 64000)
            if w0 > 0 and w0 < 64000 and w1 < 64000 and w2 < 64000:
                name = rad50_decode(w0) + rad50_decode(w1)
                ext = rad50_decode(w2)
                if name.strip() and ext.strip():
                    valid_entries += 1
                    # Read remaining words
                    w3 = read_word_le(img, entry_off + 6)
                    w4 = read_word_le(img, entry_off + 8)
                    w5 = read_word_le(img, entry_off + 10)
                    entries_text.append(
                        f"    {name}.{ext}  status=${w3:04X} "
                        f"start={w4:5d} size={w5:5d}")

        if valid_entries >= 2:
            print(f"\n  Sector {scan_sector} ({valid_entries} entries):")
            for e in entries_text:
                print(e)

# ─── Step 10: Check $0 area in RAM for OS header ───
print(f"\n{'='*70}")
print(f"STEP 10: AMOS OS HEADER AT $000000")
print(f"{'='*70}")

print(f"First 128 bytes of loaded OS:")
for base in range(0, 128, 16):
    words = []
    for off in range(0, 16, 2):
        w = bus.read_word(base + off)
        words.append(f"{w:04X}")
    ascii_str = ""
    for off in range(0, 16):
        b = bus.read_byte(base + off)
        ascii_str += chr(b) if 32 <= b < 127 else "."
    print(f"  ${base:06X}: {' '.join(words)}  {ascii_str}")

# Entry point at $000030
print(f"\nEntry point code at $000030:")
for base in range(0x30, 0x60, 16):
    words = []
    for off in range(0, 16, 2):
        w = bus.read_word(base + off)
        words.append(f"{w:04X}")
    print(f"  ${base:06X}: {' '.join(words)}")

print(f"\n{'='*70}")
print(f"ANALYSIS COMPLETE")
print(f"{'='*70}")
