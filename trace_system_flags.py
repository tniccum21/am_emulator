#!/usr/bin/env python3
"""Trace ALL writes to the SYSTEM longword ($0400-$0403) during boot.

Hooks _write_byte_physical in the memory bus to catch every single byte
written to the SYSTEM communication area, regardless of access size.
Also tracks what code is reading SYSTEM+3 ($0403) to understand the
scheduler's job-ready check.
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
    max_instructions=5_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)

# Hook _write_byte_physical to track writes to $0400-$0403
writes_to_system = []
orig_write = bus._write_byte_physical

def hooked_write(address, value):
    phys = address & 0xFFFFFF
    if 0x0400 <= phys <= 0x0403:
        writes_to_system.append((len(writes_to_system), cpu.pc, phys, value & 0xFF))
    orig_write(address, value)

bus._write_byte_physical = hooked_write

# Also hook reads from $0403 to see who checks the flag
reads_from_0403 = []
orig_read = bus._read_byte_physical
read_count = [0]

def hooked_read(address):
    phys = address & 0xFFFFFF
    val = orig_read(address)
    if phys == 0x0403:
        reads_from_0403.append((read_count[0], cpu.pc, val))
    read_count[0] += 1
    return val

bus._read_byte_physical = hooked_read

# Boot
cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)

count = 0
while not cpu.halted and count < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count += 1

print(f"\n[DONE] {count} instructions, PC=${cpu.pc:06X}, LED={led.value:02X}", file=sys.stderr)

# Show all writes
print(f"\n=== ALL writes to SYSTEM ($0400-$0403) ===")
print(f"Total writes: {len(writes_to_system)}")
for i, (seq, pc, addr, val) in enumerate(writes_to_system):
    # Read current full SYSTEM value after each write
    print(f"  [{i:4d}] PC=${pc:06X}  → ${addr:04X} = ${val:02X}")

# Show final SYSTEM value
print(f"\n=== Final SYSTEM longword ===")
sys_bytes = []
for a in range(0x0400, 0x0404):
    b = orig_read(a)
    sys_bytes.append(b)
print(f"  Raw bytes: {' '.join(f'{b:02X}' for b in sys_bytes)}")
# Word-swapped (as CPU sees it)
w0 = (sys_bytes[1] << 8) | sys_bytes[0]
w1 = (sys_bytes[3] << 8) | sys_bytes[2]
long_val = (w0 << 16) | w1
print(f"  CPU longword: ${long_val:08X}")
print(f"  Byte $0403 = ${sys_bytes[3]:02X} = {sys_bytes[3]:08b}")
print(f"  Bit 4 of $0403 = {(sys_bytes[3] >> 4) & 1}")

# Show reads from $0403
print(f"\n=== Reads from $0403 (first 20, last 5) ===")
print(f"Total reads: {len(reads_from_0403)}")
for r in reads_from_0403[:20]:
    seq, pc, val = r
    print(f"  PC=${pc:06X}  val=${val:02X}")
if len(reads_from_0403) > 25:
    print(f"  ... ({len(reads_from_0403) - 25} more) ...")
    for r in reads_from_0403[-5:]:
        seq, pc, val = r
        print(f"  PC=${pc:06X}  val=${val:02X}")
