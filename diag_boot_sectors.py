#!/usr/bin/env python3
"""Trace exactly which sectors the ROM bootstrap reads and where data goes in RAM.

Goal: determine if AMOSL.MON is partially loaded, leaving driver code on disk.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from alphasim.config import SystemConfig
from alphasim.main import build_system

config = SystemConfig(
    rom_even_path=Path("roms/AM-178-01-B05.BIN"),
    rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
    ram_size=0x400000,
    config_dip=0x0A,
    disk_image_path=Path("images/AMOS_1-3_Boot_OS.img"),
    trace_enabled=False,
    max_instructions=50_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None

# Instrument SASI to track all sector reads with RAM destination
sasi = None
for start, end, dev in bus._devices:
    if start == 0xFFFFE0:
        sasi = dev
        break

# Track PIO read destinations by watching CPU writes after sector reads
reads = []
import types

orig_do_read = sasi._do_read_sector

def tracked_read(self):
    track = self._sno
    sector = self._sct
    head = (self._sdh >> 4) & 1
    spt = 10
    physical = track * spt + (max(sector, 1) - 1)
    lba = physical * 2 + head + 1
    reads.append({
        'track': track, 'sector': sector, 'head': head, 'lba': lba,
        'instruction': instruction_count
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

# Analyze reads
if reads:
    lbas = sorted(set(r['lba'] for r in reads))
    print(f"Unique LBAs read: {len(lbas)}")
    print(f"LBA range: {min(lbas)} to {max(lbas)}")

    # Show sectors in order of first access
    print(f"\n=== Sector reads in order ===")
    for i, r in enumerate(reads):
        if i < 20 or i > len(reads) - 10:
            print(f"  [{i:3d}] LBA={r['lba']:4d} T={r['track']:3d} S={r['sector']:2d} H={r['head']} "
                  f"@ inst {r['instruction']}")
        elif i == 20:
            print(f"  ... ({len(reads) - 30} more reads) ...")

    # Total bytes read from disk
    total = len(reads) * 512
    print(f"\nTotal data read: {total} bytes ({total//1024} KB)")
    print(f"That maps to RAM addresses $0000-${total-1:04X} (approx)")

    # Check if $7AC2 falls within loaded range
    if total > 0x7AC2:
        print(f"\n*** $7AC2 IS within loaded range — driver SHOULD be present ***")
        print(f"*** Something may be overwriting it after load ***")
    else:
        print(f"\n*** $7AC2 is OUTSIDE loaded range ({total} < {0x7AC2}) ***")
        print(f"*** Bootstrap doesn't load enough data! ***")
        print(f"*** Need to load {0x7AC2 - total} more bytes ***")

# Check the raw disk image at the LBAs that were read
print(f"\n{'='*60}")
print(f"=== Raw disk data around $7AC2 equivalent sectors ===")
img_path = Path("images/AMOS_1-3_Boot_OS.img")
with open(img_path, "rb") as f:
    img = f.read()

# The AMOSL.MON file on disk starts at some sector
# Find the first few AMOSL.MON sectors
# From MFD: AMOSL.MON at offset $099656 (sector 1227)
# The MFD entry at sector 1227 should tell us the starting block
# AMOS MFD entry format:
#   word 0-1: filename (2 RAD50 words)
#   word 2: extension (1 RAD50 word)
#   word 3: file attributes
#   word 4: starting block number
#   word 5: file size in blocks
# (This is approximate — AMOS filesystem varies by version)

mfd_offset = 0x099656
print(f"\nAMOSL.MON MFD entry at disk offset ${mfd_offset:06X} (sector {mfd_offset//512}):")
for w in range(0, 16, 2):
    lo = img[mfd_offset + w]
    hi = img[mfd_offset + w + 1]
    word = (hi << 8) | lo
    print(f"  word {w//2}: ${word:04X} ({word})")

# Let's look at what the ROM bootstrap loads
# The ROM reads sectors starting from the boot block
# Show what sectors correspond to what LBAs
print(f"\n=== Mapping LBAs to disk image offsets ===")
for lba in sorted(lbas)[:20]:
    offset = lba * 512
    # Show first 8 bytes of each sector (byte-swapped to CPU view)
    words = []
    for w in range(0, 16, 2):
        lo = img[offset + w]
        hi = img[offset + w + 1]
        word = (hi << 8) | lo
        words.append(f"${word:04X}")
    print(f"  LBA {lba:4d} (offset ${offset:08X}): {' '.join(words)}")

# Now: what's on disk just PAST the last loaded LBA?
max_lba = max(lbas)
print(f"\n=== Disk content past last loaded sector (LBA {max_lba}) ===")
for lba in range(max_lba + 1, max_lba + 20):
    offset = lba * 512
    if offset + 16 > len(img):
        break
    # Check if sector has data
    is_zero = all(img[offset + i] == 0 for i in range(512))
    if not is_zero:
        words = []
        for w in range(0, 16, 2):
            lo = img[offset + w]
            hi = img[offset + w + 1]
            word = (hi << 8) | lo
            words.append(f"${word:04X}")
        print(f"  LBA {lba:4d}: {' '.join(words)} (NON-ZERO)")
    else:
        print(f"  LBA {lba:4d}: (all zeros)")

# Finally: check which disk sectors contain SASI references
print(f"\n=== Sectors with SASI register references ===")
sasi_sectors = set()
for pos in range(0, len(img) - 3):
    if img[pos] == 0xFF and img[pos+1] == 0xFF and img[pos+2] >= 0xE0 and img[pos+2] <= 0xE7 and img[pos+3] == 0xFF:
        sasi_sectors.add(pos // 512)
for s in sorted(sasi_sectors):
    loaded = "LOADED" if any(r['lba'] == s for r in reads) else "NOT LOADED"
    lba_equivalent = s  # Assuming 1:1 mapping (may not be accurate due to CHS mapping)
    print(f"  Sector {s} — {loaded}")
    # Show the context
    offset = s * 512
    for pos in range(offset, offset + 512 - 3):
        if img[pos] == 0xFF and img[pos+1] == 0xFF and img[pos+2] >= 0xE0 and img[pos+2] <= 0xE7 and img[pos+3] == 0xFF:
            ctx_start = max(offset, pos - 16)
            ctx_end = min(offset + 512, pos + 16)
            # Show as byte-swapped words
            words = []
            for w in range(ctx_start, ctx_end, 2):
                lo = img[w]
                hi = img[w + 1]
                words.append(f"${(hi << 8)|lo:04X}")
            in_sector_off = pos - offset
            print(f"    at +{in_sector_off}: {' '.join(words)}")
