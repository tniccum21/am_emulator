#!/usr/bin/env python3
"""Test: targeted RAM clear after memory sizing.

Only clear RAM from $10000 upward to preserve the ROM's initialized
low-memory areas (vectors, stack, system area). The garbage JOBCUR
pointer ($112100) is above this threshold, so it will find zero.
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
led_changes = []

cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)

ram_cleared = False
count = 0
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc

    # Hook: targeted RAM clear after memory sizing exits
    if not ram_cleared and pc_before == 0x0080B6:
        memend = cpu.a[4]
        print(f"\n[{count:8d}] Memory sizing exit — targeted clear", file=sys.stderr)
        print(f"  MEMEND candidate: ${memend:08X}", file=sys.stderr)
        # Only clear RAM from $10000 upward — preserves vectors, system area, ROM stack
        ram = bus._ram
        clear_start = 0x10000
        clear_end = min(len(ram.data), memend & 0xFFFFFF)
        for i in range(clear_start, clear_end):
            ram.data[i] = 0
        ram_cleared = True
        print(f"  Cleared ${clear_start:06X}-${clear_end:06X} ({clear_end - clear_start} bytes)", file=sys.stderr)

    cpu.step()
    bus.tick(1)
    count += 1

    if led.value != prev_led:
        print(f"[{count:8d}] LED {prev_led:02X} → {led.value:02X}  PC=${pc_before:06X}", file=sys.stderr)
        led_changes.append((count, prev_led, led.value))
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
print(f"  LED changes: {len(led_changes)}")
for cnt, old, new in led_changes:
    print(f"    [{cnt:8d}] {old:02X} → {new:02X}")

# Show ACIA output
print(f"\n=== ACIA output ({len(output_bytes)} bytes) ===")
if output_bytes:
    text = output_bytes.decode('ascii', errors='replace')
    print(f"  Text: {repr(text[:500])}")
    lines = text.split('\n')
    for i, line in enumerate(lines[:30]):
        print(f"  Line {i}: {repr(line)}")
else:
    print("  (no output)")

# Key memory locations
print(f"\n=== Key Memory ===")
for name, addr in [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C), ("WEREUP", 0x042E),
]:
    if name == "WEREUP":
        val = bus.read_word(addr)
        print(f"  {name} (${addr:04X}): ${val:04X}")
    else:
        val = bus.read_long(addr)
        print(f"  {name} (${addr:04X}): ${val:08X}")
