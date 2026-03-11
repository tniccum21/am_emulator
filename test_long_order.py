#!/usr/bin/env python3
"""Test if our bus long word order matches what the CPU expects.

The Alpha Micro has word-level byte swap. The question is whether
long words also need word-swapping (PDP-11 "middle endian" order).

Test: After boot, check if system communication area values make more
sense with word-swapped read_long vs standard read_long.
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
    max_instructions=12_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None  # suppress output
cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count += 1

# Read system area using BOTH byte orders
print("=== System Communication Area — Standard vs Word-Swapped ===")
print(f"{'Name':<10} {'Addr':<6} {'Phys bytes':<14} {'Standard':<12} {'Word-swapped':<12}")
print("-" * 60)

vars = [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C),
]

for name, addr in vars:
    # Read raw physical bytes
    b0 = bus._read_byte_physical(addr)
    b1 = bus._read_byte_physical(addr + 1)
    b2 = bus._read_byte_physical(addr + 2)
    b3 = bus._read_byte_physical(addr + 3)

    # Standard read_long (hi word at base)
    w0 = bus.read_word(addr)      # word at base = hi word
    w1 = bus.read_word(addr + 2)  # word at base+2 = lo word
    standard = (w0 << 16) | w1

    # Word-swapped read_long (lo word at base, PDP-11 style)
    swapped = (w1 << 16) | w0

    phys = f"{b0:02X} {b1:02X} {b2:02X} {b3:02X}"
    print(f"{name:<10} ${addr:04X}  {phys}  ${standard:08X}  ${swapped:08X}")

# Now do a definitive test: trace a specific MOVE.L instruction
# and see what physical bytes it produces
print(f"\n=== Definitive test: write and read a known long value ===")
# Write $12345678 to a known RAM location using bus.write_long
test_addr = 0x100000  # 1MB into RAM
bus.write_long(test_addr, 0x12345678)

b0 = bus._read_byte_physical(test_addr)
b1 = bus._read_byte_physical(test_addr + 1)
b2 = bus._read_byte_physical(test_addr + 2)
b3 = bus._read_byte_physical(test_addr + 3)
print(f"write_long($100000, $12345678):")
print(f"  Physical: {b0:02X} {b1:02X} {b2:02X} {b3:02X}")
print(f"  read_long: ${bus.read_long(test_addr):08X}")
w0 = bus.read_word(test_addr)
w1 = bus.read_word(test_addr + 2)
print(f"  read_word(base): ${w0:04X}, read_word(base+2): ${w1:04X}")

# Now trace what the OS did: check the init code that sets up DDBCHN
# The OS writes to $0408 (DDBCHN). Let's trace HOW it writes.
print(f"\n=== Memory test: what does MOVE.L CPU instruction produce? ===")
# Use a fresh test area and simulate a CPU MOVE.L
# First, let's manually do what the CPU does for MOVE.L #$AABBCCDD,(addr)
# 68000 MOVE.L to memory: bus.write_word(addr, hi_word), bus.write_word(addr+2, lo_word)
test_addr2 = 0x100010
bus.write_word(test_addr2, 0xAABB)      # hi word
bus.write_word(test_addr2 + 2, 0xCCDD)  # lo word
b0 = bus._read_byte_physical(test_addr2)
b1 = bus._read_byte_physical(test_addr2 + 1)
b2 = bus._read_byte_physical(test_addr2 + 2)
b3 = bus._read_byte_physical(test_addr2 + 3)
print(f"Manual MOVE.L sim (hi word first): phys = {b0:02X} {b1:02X} {b2:02X} {b3:02X}")
print(f"  read_long: ${bus.read_long(test_addr2):08X}")

# Now try PDP-11 order: lo word first
test_addr3 = 0x100020
bus.write_word(test_addr3, 0xCCDD)      # lo word first
bus.write_word(test_addr3 + 2, 0xAABB)  # hi word second
b0 = bus._read_byte_physical(test_addr3)
b1 = bus._read_byte_physical(test_addr3 + 1)
b2 = bus._read_byte_physical(test_addr3 + 2)
b3 = bus._read_byte_physical(test_addr3 + 3)
print(f"PDP-11 order (lo word first): phys = {b0:02X} {b1:02X} {b2:02X} {b3:02X}")
print(f"  Standard read_long: ${bus.read_long(test_addr3):08X}")
print(f"  Swapped read_long: ${(bus.read_word(test_addr3+2) << 16) | bus.read_word(test_addr3):08X}")

# Check a value we KNOW: the ROM vectors
# SSP should be $00032400, PC should be $800018
# These are loaded during reset from ROM at $800000-$800007
print(f"\n=== ROM reset vectors ===")
for addr in range(0x800000, 0x800008):
    b = bus._read_byte_physical(addr)
    print(f"  ROM phys[${addr:06X}] = ${b:02X}")
print(f"  read_long($800000) = ${bus.read_long(0x800000):08X}  (should be SSP=$00032400)")
print(f"  read_long($800004) = ${bus.read_long(0x800004):08X}  (should be PC=$800018)")
# Try swapped
w0 = bus.read_word(0x800000)
w1 = bus.read_word(0x800002)
print(f"  Swapped: ${(w1 << 16) | w0:08X}  (swapped SSP)")
w0 = bus.read_word(0x800004)
w1 = bus.read_word(0x800006)
print(f"  Swapped: ${(w1 << 16) | w0:08X}  (swapped PC)")
