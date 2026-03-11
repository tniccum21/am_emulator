#!/usr/bin/env python3
"""Trace the exact SP loading at $123A and surrounding code.

Goal: Understand what instruction loads SP and what address it reads from.
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
    max_instructions=15_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None
cpu.reset()

# Run until we first reach $123A
count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x123A:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"Reached $123A at instruction {count}")
print(f"SP before = ${cpu.a[7]:08X}")

# Dump code at $1230-$1250
print(f"\n=== Code at $1230-$1260 ===")
for addr in range(0x1230, 0x1260, 2):
    try:
        w = bus.read_word(addr)
        print(f"  ${addr:06X}: ${w:04X}")
    except:
        print(f"  ${addr:06X}: ???")

# Step through $123A and show what happens
print(f"\n=== Stepping through $123A ===")
for step in range(20):
    pc = cpu.pc
    try:
        op = bus.read_word(pc)
    except:
        op = 0xDEAD
    sp = cpu.a[7]
    d7 = cpu.d[7]
    sr = cpu.sr
    print(f"  [{step:2d}] PC=${pc:06X} op=${op:04X}"
          f"  SP=${sp:08X} D7=${d7:08X} SR=${sr:04X}"
          f"  D0=${cpu.d[0]:08X} A6=${cpu.a[6]:08X}")
    cpu.step()
    bus.tick(1)
    count += 1

# Now also dump the code at $12AE (where SP gets restored)
print(f"\n=== Code at $12A0-$12C0 ===")
for addr in range(0x12A0, 0x12C0, 2):
    try:
        w = bus.read_word(addr)
        print(f"  ${addr:06X}: ${w:04X}")
    except:
        print(f"  ${addr:06X}: ???")

# What's at the address that holds $06F4?
# The instruction at $123A reads from some absolute short address
# Let's check addresses in the system area that might hold $06F4
print(f"\n=== Searching for $06F4 in system area ===")
for addr in range(0x0400, 0x0500, 2):
    try:
        w = bus.read_word(addr)
        if w == 0x06F4 or w == 0x0006:
            print(f"  ${addr:06X}: ${w:04X}")
    except:
        pass

# Check longwords
for addr in range(0x0400, 0x0500, 4):
    try:
        v = bus.read_long(addr)
        if v == 0x000006F4:
            print(f"  ${addr:06X}: ${v:08X} (longword match!)")
    except:
        pass
