#!/usr/bin/env python3
"""Search disk image for SASI driver code and examine AMOSL.MON loading.

Approach:
1. Search raw disk image for references to SASI registers ($FFFFE0-$FFFFE7)
2. Find SCZ.DVR file in AMOS filesystem
3. Check what exactly the ROM bootstrap loads and where
4. Run AMOS_1-3 further into boot to see device init
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

# --- Part 1: Search raw disk image for SASI register references ---

img_path = Path("images/AMOS_1-3_Boot_OS.img")
with open(img_path, "rb") as f:
    img = f.read()

print(f"=== Searching AMOS_1-3_Boot_OS.img for SASI references ===")
print(f"Image size: {len(img)} bytes ({len(img)//512} sectors)")

# SASI registers are at $FFFFE0-$FFFFE7
# In the AM-1200 byte-swap world, $FFFFE0 in a word operand would appear as:
# High word $FFFF → bytes: $FF, $FF (byte-swapped: $FF, $FF — same!)
# Low word $FFE0 → bytes: $E0, $FF (byte-swapped)
# So searching for $FFE0 as LE bytes: $E0, $FF

# Search for the byte pattern that represents absolute addressing to $FFFFEx
# In 68000, absolute long addressing uses the extension words:
# $FFFF $FFE0 → raw bytes: $FF $FF $E0 $FF (with byte-swap in each word)
matches = []
for pos in range(0, len(img) - 3):
    # Look for word-swapped $FFFF followed by word-swapped $FFEx
    # $FFFF as LE word → FF FF (no change)
    # $FFE0 as LE word → E0 FF
    if img[pos] == 0xFF and img[pos+1] == 0xFF and img[pos+2] >= 0xE0 and img[pos+2] <= 0xE7 and img[pos+3] == 0xFF:
        sector = pos // 512
        off = pos % 512
        # Show context
        ctx_start = max(0, pos - 8)
        ctx_end = min(len(img), pos + 12)
        ctx_hex = ' '.join(f'{img[i]:02X}' for i in range(ctx_start, ctx_end))
        matches.append((pos, sector, off, ctx_hex))

print(f"Found {len(matches)} references to $FFFFEx (SASI regs) in disk image:")
for pos, sector, off, ctx in matches[:30]:
    print(f"  offset ${pos:08X} (sector {sector}, +{off}): ...{ctx}...")

# Also search for the absolute short form: $FE00-$FE07 (short address $FFE0-$FFE7)
# Absolute short: .W operand, sign-extended to 24/32 bits
# $FFE0 as word → LE bytes: $E0, $FF
print(f"\n=== Search for absolute short $FFEx references ===")
short_matches = []
for pos in range(0, len(img) - 1):
    if img[pos] >= 0xE0 and img[pos] <= 0xE7 and img[pos+1] == 0xFF:
        sector = pos // 512
        off = pos % 512
        # Check if this is likely an operand (preceded by an opcode-like word)
        if pos >= 2:
            prev_lo = img[pos-2]
            prev_hi = img[pos-1]
            prev_word = (prev_hi << 8) | prev_lo
            # Filter for likely 68000 opcodes
            if prev_word != 0xFFFF and prev_word != 0x0000:
                short_matches.append((pos, sector, off, prev_word))

print(f"Found {len(short_matches)} potential absolute short $FFEx references")
for pos, sector, off, prev in short_matches[:30]:
    ctx_start = max(0, pos - 4)
    ctx_end = min(len(img), pos + 4)
    ctx_hex = ' '.join(f'{img[i]:02X}' for i in range(ctx_start, ctx_end))
    print(f"  offset ${pos:08X} (sector {sector}): prev=${prev:04X} ...{ctx_hex}...")

# --- Part 2: Search for SCZ.DVR in AMOS filesystem ---
print(f"\n{'='*60}")
print(f"=== Searching for SCZ.DVR in AMOS filesystem ===")

def rad50_encode(s):
    """Encode a 3-char string to RAD50."""
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    result = 0
    for i, ch in enumerate(s.upper()[:3]):
        idx = chars.index(ch) if ch in chars else 0
        result = result * 40 + idx
    return result

def rad50_decode(word):
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    c3 = word % 40; word //= 40
    c2 = word % 40; word //= 40
    c1 = word % 40
    return chars[c1] + chars[c2] + chars[c3]

# SCZ.DVR: "SCZ" = S=19,C=3,Z=26 → 19*1600+3*40+26 = 30400+120+26 = 30546 = $7752
# " " padding → "SCZ   " → word1="SCZ"=$7752, word2="   "=$0000
# "DVR" = D=4,V=22,R=18 → 4*1600+22*40+18 = 6400+880+18 = 7298 = $1C82
scz_word = rad50_encode("SCZ")
dvr_word = rad50_encode("DVR")
print(f"  RAD50 'SCZ' = ${scz_word:04X}")
print(f"  RAD50 'DVR' = ${dvr_word:04X}")

# Search for SCZ in the image (LE byte order)
scz_lo = scz_word & 0xFF
scz_hi = (scz_word >> 8) & 0xFF
for pos in range(0, len(img) - 5):
    if img[pos] == scz_lo and img[pos+1] == scz_hi:
        sector = pos // 512
        # Check surrounding context for MFD entry
        ctx_words = []
        for w in range(0, 12, 2):
            if pos + w + 1 < len(img):
                word = (img[pos+w+1] << 8) | img[pos+w]
                ctx_words.append(f"${word:04X}")
        ctx_decoded = []
        for w in range(0, 12, 2):
            if pos + w + 1 < len(img):
                word = (img[pos+w+1] << 8) | img[pos+w]
                if word < 64000:
                    ctx_decoded.append(rad50_decode(word))
        print(f"  Found 'SCZ' at offset ${pos:06X} (sector {sector})")
        print(f"    Words: {' '.join(ctx_words)}")
        print(f"    RAD50: {' '.join(ctx_decoded)}")

# --- Part 3: Search for AMOSL.MON ---
print(f"\n=== Searching for AMOSL.MON in filesystem ===")
amosl_w1 = rad50_encode("AMO")
amosl_w2 = rad50_encode("SL ")
mon_w = rad50_encode("MON")
print(f"  RAD50 'AMO' = ${amosl_w1:04X}, 'SL ' = ${amosl_w2:04X}, 'MON' = ${mon_w:04X}")

a1_lo, a1_hi = amosl_w1 & 0xFF, (amosl_w1 >> 8) & 0xFF
a2_lo, a2_hi = amosl_w2 & 0xFF, (amosl_w2 >> 8) & 0xFF

for pos in range(0, len(img) - 7):
    if img[pos] == a1_lo and img[pos+1] == a1_hi and img[pos+2] == a2_lo and img[pos+3] == a2_hi:
        sector = pos // 512
        ext_word = (img[pos+5] << 8) | img[pos+4]
        ext_str = rad50_decode(ext_word)
        if ext_str.strip() in ("MON", ""):
            print(f"  Found 'AMOSL' at offset ${pos:06X} (sector {sector})")
            # Dump 32 bytes of MFD entry context
            entry_start = pos
            print(f"    MFD entry words:")
            for w in range(0, 32, 2):
                if entry_start + w + 1 < len(img):
                    word = (img[entry_start+w+1] << 8) | img[entry_start+w]
                    decoded = rad50_decode(word) if word < 64000 else "---"
                    print(f"      +${w:02X}: ${word:04X}  ({decoded})")

# --- Part 4: Also try searching in loaded RAM for driver code ---
print(f"\n{'='*60}")
print(f"=== Checking loaded RAM after boot ===")
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
cpu.reset()

# Run further — past device init, to where the error messages appear
count = 0
comint_count = 0
while not cpu.halted and count < config.max_instructions:
    pc = cpu.pc
    try:
        opword = bus.read_word(pc)
    except:
        opword = 0

    if (opword & 0xF000) == 0xA000:
        svca_num = (opword - 0xA000) // 2
        if svca_num == 0o166:  # COMINT
            comint_count += 1
            if comint_count == 1:
                print(f"First COMINT call at instruction {count}, PC=${pc:06X}")
            break

    cpu.step()
    bus.tick(1)
    count += 1

print(f"Stopped at instruction {count}")

# Now check the DDT and DDB structures
print(f"\n=== DDT at $7038 (after device init) ===")
for offset in range(0, 0x90, 4):
    addr = 0x7038 + offset
    val = bus.read_long(addr)
    if val != 0 or offset in (0, 4, 8, 0xC, 0x34, 0x84):
        label = {0: "flags", 4: "driver ptr?", 8: "link", 0xC: "dev data",
                 0x34: "RAD50 name", 0x84: "result"}.get(offset, "")
        print(f"  +${offset:02X}: ${val:08X}  {label}")

# Check DDB chain
print(f"\n=== DDB chain from DDBCHN ($0408) ===")
ddbchn = bus.read_long(0x0408)
print(f"  DDBCHN = ${ddbchn:08X}")
addr = ddbchn
for i in range(10):
    if addr == 0 or addr >= 0x400000:
        break
    print(f"\n  DDB #{i} at ${addr:06X}:")
    for off in range(0, 0x20, 4):
        val = bus.read_long(addr + off)
        if val != 0:
            print(f"    +${off:02X}: ${val:08X}")
    # Get the DDT/driver pointer - try different offsets
    for try_off in [4, 8, 0xC]:
        ptr = bus.read_long(addr + try_off)
        if ptr > 0x1000 and ptr < 0x100000:
            print(f"    → ptr at +${try_off:02X}=${ptr:08X}: first words = ", end="")
            for w in range(4):
                print(f"${bus.read_word(ptr + w*2):04X} ", end="")
            print()
    link = bus.read_long(addr + 8)
    addr = link

# Search loaded RAM for SASI register references
print(f"\n=== Searching loaded RAM for SASI register references ===")
for addr in range(0x0500, 0x20000, 2):
    w = bus.read_word(addr)
    # Look for $FFE0-$FFE7 (absolute short SASI addresses)
    if 0xFFE0 <= w <= 0xFFE7:
        # Check context - previous word might be an opcode
        if addr >= 2:
            prev = bus.read_word(addr - 2)
            # Filter for likely operands (following MOVE, CMP, etc)
            if prev != 0xFFFF:
                print(f"  ${addr:06X}: ${w:04X} (prev: ${prev:04X})")
