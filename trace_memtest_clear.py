#!/usr/bin/env python3
"""Check what the ROM memory test leaves in RAM.

Questions:
1. Does the memory test clear its patterns after testing?
2. What pattern remains at various RAM addresses after boot?
3. What address does the garbage JOBCUR pointer resolve to?
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
acia.tx_callback = lambda port, val: None

# Track when memory test writes happen
# The memory test is in the ROM, runs early (LED=06)
# We want to see what pattern is left after the test
mem_test_writes = {}
orig_write = bus._write_byte_physical
led_at_write = [0]

def hooked_write(address, value):
    phys = address & 0xFFFFFF
    # Track writes to sample addresses in the memory test region
    if phys in (0x112100, 0x112101, 0x112102, 0x112103,
                0x200000, 0x200001, 0x300000, 0x050000):
        if phys not in mem_test_writes:
            mem_test_writes[phys] = []
        mem_test_writes[phys].append((count[0], value & 0xFF, led.value))
    orig_write(address, value)

count = [0]
bus._write_byte_physical = hooked_write

cpu.reset()

while not cpu.halted and count[0] < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count[0] += 1

print(f"=== Boot completed: {count[0]} instructions ===")

# Check sample RAM locations for leftover patterns
print(f"\n=== RAM contents at sample addresses ===")
test_addrs = [
    0x050000, 0x100000, 0x112100, 0x200000, 0x300000, 0x3FF000,
    0x010000, 0x020000, 0x030000, 0x040000,
]
for addr in sorted(test_addrs):
    b0 = bus._read_byte_physical(addr)
    b1 = bus._read_byte_physical(addr + 1)
    b2 = bus._read_byte_physical(addr + 2)
    b3 = bus._read_byte_physical(addr + 3)
    w0 = bus.read_word(addr)
    w1 = bus.read_word(addr + 2)
    long_val = (w0 << 16) | w1
    print(f"  ${addr:06X}: phys={b0:02X} {b1:02X} {b2:02X} {b3:02X}  long=${long_val:08X}")

# Check the garbage JOBCUR target
print(f"\n=== Memory around garbage JOBCUR target ($112100) ===")
for base in range(0x1120F0, 0x112120, 4):
    b0 = bus._read_byte_physical(base)
    b1 = bus._read_byte_physical(base + 1)
    b2 = bus._read_byte_physical(base + 2)
    b3 = bus._read_byte_physical(base + 3)
    print(f"  ${base:06X}: {b0:02X} {b1:02X} {b2:02X} {b3:02X}")

# Show write history for key addresses
print(f"\n=== Write history for $112100 ===")
for phys in sorted(mem_test_writes.keys()):
    writes = mem_test_writes[phys]
    print(f"  ${phys:06X}: {len(writes)} writes")
    for cnt, val, led_val in writes[:10]:
        print(f"    [{cnt:8d}] value=${val:02X} LED={led_val:02X}")
    if len(writes) > 10:
        print(f"    ... and {len(writes) - 10} more")
        for cnt, val, led_val in writes[-3:]:
            print(f"    [{cnt:8d}] value=${val:02X} LED={led_val:02X}")

# Quick survey: how much RAM has non-zero content?
print(f"\n=== RAM usage survey ===")
for region_start in range(0, 0x400000, 0x40000):
    nonzero = 0
    for off in range(0, 0x40000, 64):  # Sample every 64 bytes
        if bus._read_byte_physical(region_start + off) != 0:
            nonzero += 1
    total_samples = 0x40000 // 64
    pct = nonzero * 100 / total_samples
    pattern_sample = bus._read_byte_physical(region_start + 0x100)
    print(f"  ${region_start:06X}-${region_start+0x3FFFF:06X}: {pct:5.1f}% non-zero (sample: ${pattern_sample:02X})")
