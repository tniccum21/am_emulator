#!/usr/bin/env python3
"""Disassemble the SRCH handler at $1C30 and surrounding code."""
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
output_bytes = bytearray()
def tx_callback(port, byte_val):
    if port == 0:
        output_bytes.append(byte_val)
acia.tx_callback = tx_callback

cpu.reset()
count = 0
while not cpu.halted and count < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count += 1

# Dump the SRCH handler code at $1C30-$1CA0
print("=== SRCH handler ($1C30-$1CA0) — raw words ===")
for addr in range(0x1C30, 0x1CA0, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# Also dump the COMINT handler at $6820-$6880
print("\n=== COMINT handler ($6820-$6880) ===")
for addr in range(0x6820, 0x6880, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# Check what's at the SYSBAS-like addresses
print("\n=== Key system addresses ===")
for name, addr in [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C), ("JOBESZ", 0x0420),
]:
    if name == "JOBESZ":
        val = bus.read_word(addr)
        print(f"  {name} (${addr:04X}): ${val:04X}")
    else:
        val = bus.read_long(addr)
        print(f"  {name} (${addr:04X}): ${val:08X}")

# Dump raw physical bytes at system communication area
print("\n=== Raw physical bytes at $0400-$0440 ===")
for base in range(0x0400, 0x0440, 16):
    phys = []
    for off in range(16):
        b = bus._read_byte_physical(base + off)
        phys.append(f"{b:02X}")
    ascii_repr = ""
    for off in range(16):
        b = bus._read_byte_physical(base + off)
        if 0x20 <= b <= 0x7E:
            ascii_repr += chr(b)
        else:
            ascii_repr += "."
    print(f"  ${base:06X}: {' '.join(phys)}  |{ascii_repr}|")

# Check if JOBCUR points somewhere valid
jobcur = bus.read_long(0x041C)
print(f"\n=== JOBCUR = ${jobcur:08X} ===")
masked = jobcur & 0xFFFFFF
print(f"  Masked to 24-bit: ${masked:06X}")
if masked < 0x400000:
    print(f"  Memory at masked JOBCUR:")
    for base in range(masked, min(masked + 64, 0x400000), 2):
        w = bus.read_word(base)
        print(f"    ${base:06X}: ${w:04X}")

# Check SYSBAS
sysbas = bus.read_long(0x0414)
print(f"\n=== SYSBAS = ${sysbas:08X} ===")
masked_s = sysbas & 0xFFFFFF
print(f"  Masked to 24-bit: ${masked_s:06X}")
if masked_s < 0x400000:
    print(f"  Memory at masked SYSBAS:")
    for base in range(masked_s, min(masked_s + 64, 0x400000), 2):
        w = bus.read_word(base)
        print(f"    ${base:06X}: ${w:04X}")
