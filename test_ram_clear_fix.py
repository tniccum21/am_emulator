#!/usr/bin/env python3
"""Test: does clearing RAM after memory sizing fix the module search hang?

Hook at PC=$0080B6 (memory test exit) to clear RAM, then see if
COMINT/SRCH proceeds past the module search.
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

# Track ACIA output
output_bytes = bytearray()
def tx_callback(port, byte_val):
    if port == 0:
        output_bytes.append(byte_val)
acia.tx_callback = tx_callback

# Track LED changes
prev_led = 0

cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)

ram_cleared = False
count = 0
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc

    # Hook: clear RAM after memory sizing exits
    if not ram_cleared and pc_before == 0x0080B6:
        # A4 has the MEMEND value (first failing address)
        memend = cpu.a[4]
        print(f"\n[{count:8d}] Memory sizing exit — clearing RAM to zero", file=sys.stderr)
        print(f"  MEMEND candidate: ${memend:08X}", file=sys.stderr)
        # Clear all RAM to zero
        ram = bus._ram
        for i in range(len(ram.data)):
            ram.data[i] = 0
        ram_cleared = True
        print(f"  Cleared {len(ram.data)} bytes of RAM", file=sys.stderr)

    cpu.step()
    bus.tick(1)
    count += 1

    if led.value != prev_led:
        print(f"[{count:8d}] LED {prev_led:02X} → {led.value:02X}  PC=${pc_before:06X}", file=sys.stderr)
        prev_led = led.value

    # Periodic progress
    if count % 5_000_000 == 0:
        print(f"[{count:8d}] PC=${cpu.pc:06X} LED={led.value:02X} ACIA={len(output_bytes)} bytes", file=sys.stderr)

print(f"\n[DONE] {count} instructions, PC=${cpu.pc:06X}, LED={led.value:02X}", file=sys.stderr)

# Final state
print(f"\n=== Final CPU State ===")
print(f"  PC=${cpu.pc:06X}  SR=${cpu.sr:04X}")
print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

# Show ACIA output
print(f"\n=== ACIA output ({len(output_bytes)} bytes) ===")
if output_bytes:
    text = output_bytes.decode('ascii', errors='replace')
    print(f"  Hex: {output_bytes[:80].hex()}")
    print(f"  Text: {repr(text[:200])}")
    lines = text.split('\n')
    for i, line in enumerate(lines[:30]):
        print(f"  Line {i}: {repr(line)}")

# Key memory locations
print(f"\n=== Key Memory ===")
for name, addr in [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C),
]:
    val = bus.read_long(addr)
    print(f"  {name} (${addr:04X}): ${val:08X}")

# Check if RAM around $112100 is now zero
print(f"\n=== RAM around garbage JOBCUR target ===")
for addr in [0x112100, 0x200000, 0x300000]:
    b = bus._read_byte_physical(addr)
    print(f"  ${addr:06X}: ${b:02X}")
