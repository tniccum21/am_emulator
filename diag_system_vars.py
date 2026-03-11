#!/usr/bin/env python3
"""Find AMOS system variables (MEMBAS, ZSYDSK, JOBATT, etc.) in AMOSL.MON.

From MONGEN disassembly:
  - ZSYDSK(A4) = offset to driver slot
  - JOBATT(A4) = end of driver area (null-JCB field)
  - MEMBAS(A4) = memory base (updated after driver install)
  - SYSTEM(A4) = configuration flags
  - DIAG03(A4) = boot diagnostic driver name offset

The code at +$05C8 reads ($042A).W — this may be MEMBAS.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# Read AMOSL.MON data (strip link words)
with open(src_path, "rb") as f:
    img = bytearray(f.read())

amosl = bytearray()
block = 3257
for i in range(69):
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    amosl.extend(img[offset+2:offset+512])
    if link == 0:
        break
    block = link

print(f"AMOSL.MON: {len(amosl)} bytes")

# Read as words (LE → CPU word order)
def word(addr):
    return (amosl[addr+1] << 8) | amosl[addr]

def long(addr):
    return (word(addr+2) << 16) | word(addr)

# ─── Search for MEMBAS, ZSYDSK, JOBATT ───
# MEMBAS should contain $7AC2 (no driver = driver slot start)
# ZSYDSK should contain $7AC2 (driver slot offset)
# JOBATT should contain the end-of-driver-area offset

print(f"\nSearching for longwords = $00007AC2 (MEMBAS/ZSYDSK candidates):")
for addr in range(0, min(0x1000, len(amosl) - 3), 2):
    val = long(addr)
    if val == 0x7AC2:
        print(f"  ${addr:04X}: ${val:08X}")

print(f"\nSearching for words = $7AC2:")
for addr in range(0, min(0x1000, len(amosl) - 1), 2):
    val = word(addr)
    if val == 0x7AC2:
        # Also show surrounding context
        prev = word(addr - 2) if addr >= 2 else 0
        next_w = word(addr + 2) if addr + 2 < len(amosl) else 0
        print(f"  ${addr:04X}: ${val:04X} (prev=${prev:04X} next=${next_w:04X})")

# ─── Check key candidates ───
print(f"\nKey address candidates:")
for addr in [0x0004, 0x0008, 0x000C, 0x0010, 0x0014, 0x0018, 0x001C,
             0x0020, 0x0024, 0x0028, 0x002C, 0x0030,
             0x0400, 0x0404, 0x0408, 0x040C, 0x0410, 0x0414,
             0x0418, 0x041C, 0x0420, 0x0424, 0x0428, 0x042A,
             0x042C, 0x0430, 0x0434, 0x0438, 0x043C, 0x0440]:
    if addr + 3 < len(amosl):
        val = long(addr)
        val_w = word(addr)
        note = ""
        if val == 0x7AC2:
            note = " ← $7AC2 (ZSYDSK? MEMBAS?)"
        elif val_w == 0x7AC2:
            note = " ← word=$7AC2"
        elif 0x7000 <= val <= 0xC000:
            note = f" ← possible system offset"
        print(f"  ${addr:04X}: L=${val:08X} W=${val_w:04X}{note}")

# ─── Check what's at $042A specifically ───
print(f"\nData around $042A (suspected MEMBAS):")
for addr in range(0x0420, 0x0440, 2):
    if addr + 1 < len(amosl):
        w = word(addr)
        l = long(addr) if addr + 3 < len(amosl) else 0
        print(f"  ${addr:04X}: W=${w:04X} L=${l:08X}")

# ─── Boot and check runtime values ───
print(f"\n{'='*60}")
print(f"RUNTIME SYSTEM VARIABLE CHECK")
print(f"{'='*60}")

from alphasim.config import SystemConfig
from alphasim.main import build_system

config = SystemConfig(
    rom_even_path=Path("roms/AM-178-01-B05.BIN"),
    rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
    ram_size=0x400000,
    config_dip=0x0A,
    disk_image_path=src_path,
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

print(f"Boot to scheduler: {count} instructions")

# Check runtime values at suspected system variable offsets
print(f"\nRuntime system variables:")
for addr, name in [(0x042A, "MEMBAS?"), (0x042C, "MEMBAS+2?"),
                    (0x042E, "ZSYDSK?"), (0x0430, "ZSYDSK+2?"),
                    (0x04E0, "ref'd by sub1"), (0x04E2, ""),
                    (0x0028, "A-line vec?"), (0x002C, ""),
                    (0x0030, ""), (0x0034, "")]:
    w = bus.read_word(addr)
    l = bus.read_long(addr)
    print(f"  ${addr:04X}: W=${w:04X} L=${l:08X}  {name}")

# Also search runtime RAM for $7AC2
print(f"\nRuntime longwords = $00007AC2:")
for addr in range(0, 0x1000, 2):
    l = bus.read_long(addr)
    if l == 0x7AC2:
        print(f"  ${addr:04X}: ${l:08X}")

print(f"\nRuntime words = $7AC2:")
for addr in range(0, 0x1000, 2):
    w = bus.read_word(addr)
    if w == 0x7AC2:
        l = bus.read_long(addr)
        print(f"  ${addr:04X}: W=${w:04X} L=${l:08X}")

# Check what JOBATT might be — look for values > $7AC2 and < $C000
print(f"\nCandidate JOBATT values (longwords $7AC2-$C000 in first $600 bytes):")
for addr in range(0, 0x600, 2):
    l = bus.read_long(addr)
    if 0x7AC2 < l < 0xC000:
        print(f"  ${addr:04X}: ${l:08X}")

# Check the DDT for cross-references
print(f"\nDDT at $7038 (full dump):")
for off in range(0, 0x90, 2):
    addr = 0x7038 + off
    w = bus.read_word(addr)
    if w != 0:
        print(f"  +${off:02X} (${addr:06X}): ${w:04X}")
