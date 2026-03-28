#!/usr/bin/env python3
"""Examine the AMOS filesystem to find AMOSL.MON and check for driver code.

The AMOS disk format uses:
- Sector 0: boot block
- MFD (Master File Directory) at a known location
- Files stored as linked sector chains

We need to find:
1. How many sectors the ROM reads during bootstrap
2. Where AMOSL.MON is on disk
3. Whether driver code exists in the file but isn't being loaded
"""
import sys
import struct
sys.path.insert(0, ".")
from pathlib import Path
from alphasim.config import SystemConfig
from alphasim.main import build_system

# First, let's trace the ROM bootstrap to count SASI reads
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

# Get the SASI controller to track reads
sasi = None
for start, end, dev in bus._devices:
    if start == 0xFFFFE0:
        sasi = dev
        break

# Track SASI sector reads
sector_reads = []
orig_do_read = sasi._do_read_sector.__func__ if hasattr(sasi._do_read_sector, '__func__') else None

class SASITracker:
    def __init__(self, sasi):
        self.sasi = sasi
        self.reads = []
        self._orig_do_read = sasi._do_read_sector

    def install(self):
        tracker = self
        orig = self._orig_do_read
        def tracked_read(self_sasi):
            # Record the CHS before the read
            track = ((self_sasi._cylinder_high << 8) | self_sasi._cylinder_low)
            sector = self_sasi._sector_number
            head = (self_sasi._sdh >> 4) & 1
            # Calculate LBA
            physical = track * 10 + (sector - 1)
            lba = physical * 2 + head + 1
            tracker.reads.append({
                'track': track, 'sector': sector, 'head': head, 'lba': lba
            })
            return orig()

        import types
        sasi._do_read_sector = types.MethodType(tracked_read, sasi)

tracker = SASITracker(sasi)
tracker.install()

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
print(f"Total SASI sector reads: {len(tracker.reads)}")

# Analyze sector reads
if tracker.reads:
    lbas = [r['lba'] for r in tracker.reads]
    print(f"LBA range: {min(lbas)} to {max(lbas)}")
    print(f"Unique LBAs: {len(set(lbas))}")

    # Show first and last few reads
    print(f"\nFirst 10 reads:")
    for r in tracker.reads[:10]:
        print(f"  T={r['track']} S={r['sector']} H={r['head']} → LBA={r['lba']}")

    print(f"\nLast 10 reads:")
    for r in tracker.reads[-10:]:
        print(f"  T={r['track']} S={r['sector']} H={r['head']} → LBA={r['lba']}")

    # How much data was loaded?
    total_bytes = len(tracker.reads) * 512
    print(f"\nTotal data loaded: {total_bytes} bytes ({total_bytes/1024:.1f} KB)")

    # What's the destination address range?
    # During boot, ROM loads sectors into RAM starting at some base address
    # Let's check by looking at what addresses got written

# Now examine the raw disk image
print(f"\n{'='*60}")
print(f"RAW DISK IMAGE ANALYSIS")
print(f"{'='*60}")

img_path = Path("images/AMOS_1-3_Boot_OS.img")
with open(img_path, "rb") as f:
    img_data = f.read()

print(f"Image size: {len(img_data)} bytes ({len(img_data)/1024/1024:.1f} MB)")
print(f"Image sectors (512-byte): {len(img_data) // 512}")

# Read boot block (sector 0)
print(f"\n=== Boot block (sector 0, first 64 bytes) ===")
for off in range(0, 64, 16):
    hexbytes = ' '.join(f'{img_data[off+i]:02X}' for i in range(16))
    ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in img_data[off:off+16])
    print(f"  {off:04X}: {hexbytes}  {ascii_str}")

# The MFD should be near the beginning of the partition
# AMOS MFD entries are 32 bytes each, containing filename in RAD50
# Let's search for "AMOSL" or "MON" in the filesystem

def rad50_decode(word):
    """Decode a RAD50-encoded word to 3 characters."""
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    c3 = word % 40
    word //= 40
    c2 = word % 40
    word //= 40
    c1 = word % 40
    return chars[c1] + chars[c2] + chars[c3]

# Search for MFD entries — look for RAD50-encoded filenames
# AMOS MFD entry: 2 words (filename) + 1 word (extension) + more fields
print(f"\n=== Searching for AMOSL.MON in disk image ===")

# RAD50 encoding for "AMOSL" = "AMO" + "SL "
# "AMO" = A=1, M=13, O=15 → 1*40*40 + 13*40 + 15 = 1600 + 520 + 15 = 2135 = $0857
# "SL " = S=19, L=12, ' '=0 → 19*40*40 + 12*40 + 0 = 30400 + 480 = 30880 = $78A0
# "MON" = M=13, O=15, N=14 → 13*40*40 + 15*40 + 14 = 20800 + 600 + 14 = 21414 = $53A6

# But with byte-swap, the raw bytes would be different
# Word $0857 → phys bytes: $57, $08 (low byte first)
# Word $78A0 → phys bytes: $A0, $78

# Search for the pattern in the disk image
target_w1 = 0x0857  # "AMO"
target_w2 = 0x78A0  # "SL "
target_w3 = 0x53A6  # "MON"

# In raw disk bytes (with byte-swap), search for these words
# Word stored as: low byte at even addr, high byte at odd addr
# So $0857 → bytes $57, $08
# And $78A0 → bytes $A0, $78

pattern_bytes_1 = bytes([target_w1 & 0xFF, (target_w1 >> 8) & 0xFF])  # $57, $08
pattern_bytes_2 = bytes([target_w2 & 0xFF, (target_w2 >> 8) & 0xFF])  # $A0, $78

print(f"  RAD50 'AMO' = ${target_w1:04X}, bytes: {pattern_bytes_1.hex()}")
print(f"  RAD50 'SL ' = ${target_w2:04X}, bytes: {pattern_bytes_2.hex()}")
print(f"  RAD50 'MON' = ${target_w3:04X}, bytes: {(target_w3 & 0xFF):02X}{((target_w3 >> 8) & 0xFF):02X}")

# Search for "AMO" followed by "SL " within 2-4 bytes
for pos in range(0, len(img_data) - 6):
    if img_data[pos:pos+2] == pattern_bytes_1:
        # Check if next word is "SL "
        if img_data[pos+2:pos+4] == pattern_bytes_2:
            # Check extension
            ext_lo = img_data[pos+4]
            ext_hi = img_data[pos+5]
            ext_word = (ext_hi << 8) | ext_lo
            ext_str = rad50_decode(ext_word)

            # Read surrounding context
            sector = pos // 512
            offset_in_sector = pos % 512

            print(f"\n  FOUND 'AMOSL' at image offset {pos} (${pos:06X})")
            print(f"  Sector: {sector}, offset in sector: {offset_in_sector}")
            print(f"  Extension word: ${ext_word:04X} = '{ext_str}'")

            # Dump the MFD entry (32 bytes around this location)
            entry_start = pos
            print(f"  MFD entry dump (32 bytes):")
            for off in range(0, 32, 2):
                if entry_start + off + 1 < len(img_data):
                    lo = img_data[entry_start + off]
                    hi = img_data[entry_start + off + 1]
                    word = (hi << 8) | lo
                    print(f"    +${off:02X}: ${word:04X} ({rad50_decode(word) if word < 64000 else '---'})")

# Also search for any reference to the bytes at $7AC2
# In the raw disk image, what sector contains the data that would be loaded to $7AC2?
# We need to know the loading base address

# Let's figure out where the ROM loads data
# The ROM bootstrap typically loads starting at the "MEMBAS" address
# From boot: MEMBAS at $040C
membas = bus.read_long(0x040C)
print(f"\n=== Load address analysis ===")
print(f"MEMBAS ($040C) = ${membas:08X}")
print(f"MEMEND ($0410) = ${bus.read_long(0x0410):08X}")

# The OS is loaded at some base address, and $7AC2 is the driver code location
# If the load base is known, the file offset = $7AC2 - base
# Common AMOS bases: $0000, $0400, $0500, etc.

# Let's check what's around $7AC2 in RAM — wider scan
print(f"\n=== Wide scan around $7AC2 in RAM ===")
# Find where non-zero data starts and ends around $7AC2
for addr in range(0x7000, 0x8000, 16):
    words = []
    any_nonzero = False
    for off in range(0, 16, 2):
        w = bus.read_word(addr + off)
        if w != 0:
            any_nonzero = True
        words.append(f"{w:04X}")
    if any_nonzero:
        print(f"  ${addr:06X}: {' '.join(words)}")

# Last non-zero before $7AC2
print(f"\n=== Last non-zero before $7AC2 ===")
last_nonzero_addr = 0
for addr in range(0x7000, 0x7AC2, 2):
    w = bus.read_word(addr)
    if w != 0:
        last_nonzero_addr = addr
if last_nonzero_addr:
    print(f"  Last non-zero at ${last_nonzero_addr:06X}: ${bus.read_word(last_nonzero_addr):04X}")
    # Show context
    for addr in range(last_nonzero_addr - 16, last_nonzero_addr + 32, 16):
        words = []
        for off in range(0, 16, 2):
            w = bus.read_word(addr + off)
            words.append(f"{w:04X}")
        print(f"  ${addr:06X}: {' '.join(words)}")

# First non-zero after $7AC2
print(f"\n=== First non-zero after $7AC2 ===")
first_nonzero_addr = 0
for addr in range(0x7AC2, 0x10000, 2):
    w = bus.read_word(addr)
    if w != 0:
        first_nonzero_addr = addr
        break
if first_nonzero_addr:
    print(f"  First non-zero at ${first_nonzero_addr:06X}: ${bus.read_word(first_nonzero_addr):04X}")

# Check how big AMOSL.MON is based on loaded RAM content
# Find the boundary where loaded OS data ends
print(f"\n=== OS load boundary ===")
for addr in range(0x0500, 0x20000, 2):
    w = bus.read_word(addr)
    if w != 0:
        last_os_addr = addr
print(f"  Last non-zero in OS area: ${last_os_addr:06X}")

# Let's also check if maybe the DDT structure is different than what I assumed
# Maybe DDT+$04 is NOT the driver code pointer
# Let's look at what code actually calls the driver
print(f"\n=== Examining what calls the driver ===")
# The error path goes through $004E24-$004E4A (device search)
# Let's dump that code
print(f"  Device dispatch code at $004E24:")
for addr in range(0x4E24, 0x4E60, 2):
    w = bus.read_word(addr)
    print(f"    ${addr:06X}: ${w:04X}")

# And the DDT dispatch at $0012F4
print(f"\n  DDT dispatch at $0012F4:")
for addr in range(0x12F4, 0x1340, 2):
    w = bus.read_word(addr)
    print(f"    ${addr:06X}: ${w:04X}")
