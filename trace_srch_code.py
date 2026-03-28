#!/usr/bin/env python3
"""Dump the SRCH handler code at $1C30 and the USRBAS handler at $2B56.

Goal: Understand the exact instruction sequence of SRCH, especially:
- What displacement is used in TST.L (d16,A6) after reading JOBCUR
- How USRBAS works with the JOBCUR pointer
- What the module search loop does exactly
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

# Run until disk boot complete (LED past 0F → 00)
count = 0
while not cpu.halted and count < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count += 1

print("=== SRCH handler code at $1C30 ===")
addr = 0x1C30
for i in range(60):
    try:
        w = bus.read_word(addr)
        print(f"  ${addr:06X}: ${w:04X}")
    except Exception as e:
        print(f"  ${addr:06X}: ??? ({e})")
    addr += 2

print("\n=== USRBAS handler code at $2B56 ===")
addr = 0x2B56
for i in range(20):
    try:
        w = bus.read_word(addr)
        print(f"  ${addr:06X}: ${w:04X}")
    except:
        print(f"  ${addr:06X}: ???")
    addr += 2

print("\n=== COMINT handler code at $682E ===")
addr = 0x682E
for i in range(30):
    try:
        w = bus.read_word(addr)
        print(f"  ${addr:06X}: ${w:04X}")
    except:
        print(f"  ${addr:06X}: ???")
    addr += 2

# Also check: what's at $041C (JOBCUR) at this point in boot?
print(f"\nJOBCUR at $041C = ${bus.read_long(0x041C):08X}")
print(f"Memory at $0000-$0020 (vector table):")
for a in range(0, 0x20, 4):
    val = bus.read_long(a)
    print(f"  ${a:04X}: ${val:08X}")

# Check what's at address $02C6 (the pre-trap JOBCUR value)
print(f"\nMemory at $02C6 (pre-trap JOBCUR target):")
for a in range(0x02C0, 0x02E0, 4):
    val = bus.read_long(a)
    print(f"  ${a:04X}: ${val:08X}")

# System area
print(f"\nSystem area $0400-$0440:")
for a in range(0x0400, 0x0440, 4):
    val = bus.read_long(a)
    print(f"  ${a:04X}: ${val:08X}")
