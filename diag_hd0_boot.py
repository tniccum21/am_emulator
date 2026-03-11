#!/usr/bin/env python3
"""Try booting with HD0.img and check if SASI driver code is present."""
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
    disk_image_path=Path("images/HD0.img"),
    trace_enabled=False,
    max_instructions=50_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None
cpu.reset()

# Run until scheduler or halt
count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x1250:
        print(f"Scheduler reached at instruction {count}")
        break
    cpu.step()
    bus.tick(1)
    count += 1

if cpu.halted:
    print(f"CPU halted at instruction {count}, PC=${cpu.pc:06X}")
    sys.exit(1)

# Check DDT and driver code
print(f"\n=== DDT at $7038 ===")
for offset in [0x00, 0x04, 0x08, 0x0C, 0x34]:
    addr = 0x7038 + offset
    val = bus.read_long(addr)
    labels = {0: "flags", 4: "driver ptr", 8: "link", 0xC: "dev data", 0x34: "RAD50 name"}
    print(f"  +${offset:02X}: ${val:08X}  ({labels.get(offset, '')})")

driver_ptr = bus.read_long(0x703C)
print(f"\nDriver code pointer: ${driver_ptr:08X}")

if driver_ptr > 0 and driver_ptr < 0x400000:
    # Check for code at driver pointer
    print(f"\n=== Memory at ${driver_ptr:06X} (first 128 bytes) ===")
    all_zero = True
    for base in range(driver_ptr & ~0xF, (driver_ptr & ~0xF) + 128, 16):
        words = []
        for off in range(0, 16, 2):
            w = bus.read_word(base + off)
            if w != 0:
                all_zero = False
            words.append(f"{w:04X}")
        print(f"  ${base:06X}: {' '.join(words)}")

    if all_zero:
        print("  *** ALL ZEROS — no driver code ***")
    else:
        print("  *** DRIVER CODE PRESENT! ***")

# Also check if DDT is at a different address on this image
# Scan for DDT-like structures (RAD50 "DSK" = $1C03 at offset $34)
print(f"\n=== Scanning for DDT structures (RAD50 'DSK' = $1C03) ===")
for addr in range(0x4000, 0x20000, 2):
    w = bus.read_word(addr)
    if w == 0x1C03:
        # Check if this looks like a DDT (+$34 offset)
        ddt_base = addr - 0x34
        if ddt_base > 0:
            flags = bus.read_long(ddt_base)
            drv = bus.read_long(ddt_base + 4)
            print(f"  Possible DDT at ${ddt_base:06X}: flags=${flags:08X} driver=${drv:08X} name_at=${addr:06X}")
            if drv > 0 and drv < 0x400000:
                # Check first word at driver
                dw = bus.read_word(drv)
                print(f"    driver[${drv:06X}] first word: ${dw:04X}")

# Check wider memory for non-zero around DDT driver area
print(f"\n=== Non-zero regions $7000-$8000 ===")
last_nz = 0
for addr in range(0x7000, 0x8000, 2):
    w = bus.read_word(addr)
    if w != 0:
        last_nz = addr
if last_nz:
    print(f"  Last non-zero before $8000: ${last_nz:06X}")
else:
    print(f"  ALL ZEROS from $7000-$8000")

# Check where loaded data ends
print(f"\n=== OS load extent ===")
last_data = 0
for addr in range(0x0500, 0x40000, 4):
    v = bus.read_long(addr)
    if v != 0:
        last_data = addr
print(f"  Last non-zero: ${last_data:06X}")
print(f"  MEMBAS: ${bus.read_long(0x040C):08X}")
print(f"  MEMEND: ${bus.read_long(0x0410):08X}")

# Show LED history
print(f"\n=== LED history ===")
from alphasim.devices.led import LED
for s, e, dev in bus._devices:
    if isinstance(dev, LED):
        print(f"  Values: {', '.join(f'${v:02X}' for v in dev.history)}")
        break
