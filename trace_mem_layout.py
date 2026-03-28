#!/usr/bin/env python3
"""Check memory layout around $06F4-$0A00 to see if we can safely
increase the supervisor stack base.

Also check what sets $0434 and when.
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
cpu.reset()

# Run to just before COMINT
count = 0
while not cpu.halted and count < config.max_instructions:
    pc = cpu.pc
    try:
        opword = bus.read_word(pc)
    except:
        opword = 0

    if (opword & 0xF000) == 0xA000:
        svca_num = (opword - 0xA000) // 2
        if svca_num == 0o166:  # COMINT
            break

    cpu.step()
    bus.tick(1)
    count += 1

print(f"Stopped at COMINT call, instruction {count}")
print(f"SP=${cpu.a[7]:08X}")

# Dump memory from $0400 to $0800
print(f"\n=== Memory $0400-$04FF (system area) ===")
for addr in range(0x0400, 0x0500, 16):
    hexvals = []
    for i in range(0, 16, 2):
        try:
            w = bus.read_word(addr + i)
            hexvals.append(f"{w:04X}")
        except:
            hexvals.append("????")
    print(f"  ${addr:04X}: {' '.join(hexvals)}")

print(f"\n=== Memory $0500-$0700 ===")
for addr in range(0x0500, 0x0700, 16):
    hexvals = []
    all_zero = True
    for i in range(0, 16, 2):
        try:
            w = bus.read_word(addr + i)
            if w != 0:
                all_zero = False
            hexvals.append(f"{w:04X}")
        except:
            hexvals.append("????")
    if not all_zero:
        print(f"  ${addr:04X}: {' '.join(hexvals)}")

print(f"\n=== Memory $06E0-$0800 (around stack base $06F4) ===")
for addr in range(0x06E0, 0x0800, 16):
    hexvals = []
    for i in range(0, 16, 2):
        try:
            w = bus.read_word(addr + i)
            hexvals.append(f"{w:04X}")
        except:
            hexvals.append("????")
    print(f"  ${addr:04X}: {' '.join(hexvals)}")

# Check if anything references addresses in $0700-$0A00
# by searching for those values in the system area
print(f"\n=== Key system pointers ===")
for name, addr in [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C),
    ("$0420", 0x0420), ("$0424", 0x0424), ("$0428", 0x0428),
    ("$042C", 0x042C), ("$0430", 0x0430), ("SVSTK", 0x0434),
    ("$0438", 0x0438), ("$043C", 0x043C), ("$0440", 0x0440),
    ("$0444", 0x0444), ("$0448", 0x0448), ("$044C", 0x044C),
    ("$0450", 0x0450),
]:
    val = bus.read_long(addr)
    print(f"  {name:8s} (${addr:04X}): ${val:08X}")

# Check the full stack trace by looking at what's between SP and $06F4
sp = cpu.a[7]
print(f"\n=== Stack contents SP=${sp:04X} to $06F4 ===")
print(f"  (Total {0x06F4 - sp} bytes on stack)")
for addr in range(sp, 0x06F4, 4):
    try:
        val = bus.read_long(addr)
        # Check if this looks like a return address (ROM or OS code range)
        note = ""
        if 0x0000 <= val <= 0x7000:
            note = " (possible code ptr)"
        if 0x2000 <= val <= 0x27FF:
            note = " (possible SR value)"
        if val == 0:
            note = " (zero)"
        print(f"  ${addr:04X}: ${val:08X}{note}")
    except:
        print(f"  ${addr:04X}: ???")
