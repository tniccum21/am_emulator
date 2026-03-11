#!/usr/bin/env python3
"""Diagnose whether SASI driver code is actually loaded at DDT+$04.

Check:
1. What DDT+$04 actually points to
2. Whether there's code at that address
3. How much of AMOSL.MON was loaded by the ROM bootstrap
4. What the raw disk image contains
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

# Run until the scheduler loop is reached (OS loaded)
count = 0
scheduler_seen = False
while not cpu.halted and count < config.max_instructions:
    pc = cpu.pc
    if pc == 0x1250:  # TST.L ($041C).W in scheduler idle loop
        if not scheduler_seen:
            scheduler_seen = True
            print(f"Scheduler reached at instruction {count}")
        break
    cpu.step()
    bus.tick(1)
    count += 1

if not scheduler_seen:
    print(f"WARNING: scheduler not reached after {count} instructions")
    sys.exit(1)

# Now examine the DDT and driver code area
print(f"\n=== DDT at $7038 ===")
for offset in range(0, 0x90, 4):
    addr = 0x7038 + offset
    val = bus.read_long(addr)
    label = ""
    if offset == 0x00: label = " (status/capability)"
    elif offset == 0x04: label = " (driver code ptr)"
    elif offset == 0x08: label = " (link)"
    elif offset == 0x0C: label = " (device data ptr)"
    elif offset == 0x34: label = " (RAD50 name)"
    elif offset == 0x78: label = " (wait chain)"
    elif offset == 0x84: label = " (result status)"
    print(f"  +${offset:02X} (${addr:06X}): ${val:08X}{label}")

# Get the driver code pointer
driver_ptr = bus.read_long(0x703C)
print(f"\n=== Driver code pointer: ${driver_ptr:08X} ===")

# Dump memory around driver pointer
if driver_ptr > 0 and driver_ptr < 0x400000:
    print(f"\n=== Memory at driver code ptr ${driver_ptr:06X} (256 bytes) ===")
    all_zero = True
    for base in range(driver_ptr, driver_ptr + 256, 16):
        words = []
        for off in range(0, 16, 2):
            w = bus.read_word(base + off)
            if w != 0:
                all_zero = False
            words.append(f"{w:04X}")
        print(f"  ${base:06X}: {' '.join(words)}")
    if all_zero:
        print("  *** ALL ZEROS — driver code NOT present ***")
    else:
        print("  *** NON-ZERO DATA FOUND — driver code may be present ***")

# Also check if driver code might be at a different interpretation
# Maybe byte-swap is involved in reading the pointer
raw_bytes = []
for i in range(4):
    raw_bytes.append(bus.read_byte(0x703C + i))
print(f"\n=== Raw bytes at DDT+$04 ($703C): {' '.join(f'{b:02X}' for b in raw_bytes)} ===")
print(f"  As longword (bus.read_long): ${bus.read_long(0x703C):08X}")
print(f"  Raw byte order: ${raw_bytes[0]:02X}{raw_bytes[1]:02X}{raw_bytes[2]:02X}{raw_bytes[3]:02X}")
alt_ptr = (raw_bytes[1] << 24) | (raw_bytes[0] << 16) | (raw_bytes[3] << 8) | raw_bytes[2]
print(f"  Word-swapped interpretation: ${alt_ptr:08X}")

# Check a wider area around $7AC2 — maybe code is nearby
print(f"\n=== Scan $7800-$7E00 for non-zero regions ===")
for base in range(0x7800, 0x7E00, 16):
    words = []
    any_nonzero = False
    for off in range(0, 16, 2):
        w = bus.read_word(base + off)
        if w != 0:
            any_nonzero = True
        words.append(f"{w:04X}")
    if any_nonzero:
        print(f"  ${base:06X}: {' '.join(words)}")

# Check DDB structures that reference the DDT
print(f"\n=== DDB #1 at $182A ===")
for offset in range(0, 0x30, 4):
    addr = 0x182A + offset
    val = bus.read_long(addr)
    label = ""
    if offset == 0x00: label = " (device code)"
    elif offset == 0x04: label = " (driver/DDT ptr)"
    elif offset == 0x08: label = " (link to next DDB)"
    print(f"  +${offset:02X} (${addr:06X}): ${val:08X}{label}")

# Check what device table looks like
print(f"\n=== Device table from $0408 ===")
devtbl = bus.read_long(0x0408)
print(f"  DDBCHN ($0408) = ${devtbl:08X}")
if devtbl > 0 and devtbl < 0x400000:
    print(f"  Following DDB chain:")
    addr = devtbl
    for i in range(10):
        if addr == 0 or addr >= 0x400000:
            break
        code = bus.read_long(addr)
        driver = bus.read_long(addr + 4)
        link = bus.read_long(addr + 8)
        print(f"    DDB #{i} at ${addr:06X}: code=${code:08X} driver=${driver:08X} link=${link:08X}")

        # Check what's at the driver address
        if driver > 0 and driver < 0x400000:
            drv_w0 = bus.read_word(driver)
            drv_w1 = bus.read_word(driver + 2)
            drv_w2 = bus.read_word(driver + 4)
            drv_w3 = bus.read_word(driver + 6)
            print(f"      driver[${driver:06X}]: ${drv_w0:04X} ${drv_w1:04X} ${drv_w2:04X} ${drv_w3:04X}")
        addr = link

# Check how much of RAM has been loaded (scan for last non-zero region)
print(f"\n=== RAM load extent (scanning for last non-zero) ===")
last_nonzero = 0
for addr in range(0, 0x20000, 256):
    for off in range(0, 256, 4):
        v = bus.read_long(addr + off)
        if v != 0:
            last_nonzero = addr + off
print(f"  Last non-zero address below $20000: ${last_nonzero:06X}")

# Also check what the LINE-A $03C handler does
print(f"\n=== LINE-A handler dispatch ===")
aline_vector = bus.read_long(0x028)  # A-line trap vector
print(f"  A-line vector ($028) = ${aline_vector:08X}")
print(f"\n=== Memory at A-line handler ${aline_vector & 0xFFFFFF:06X} ===")
handler = aline_vector & 0xFFFFFF
for base in range(handler, handler + 64, 16):
    words = []
    for off in range(0, 16, 2):
        w = bus.read_word(base + off)
        words.append(f"{w:04X}")
    print(f"  ${base:06X}: {' '.join(words)}")

# Check where the DDT pointer ($7038) appears in DDB chain
# The DDB at $182A has driver=$7038? Let's verify
print(f"\n=== Checking DDB→DDT pointer interpretation ===")
ddb1_drv = bus.read_long(0x182E)  # DDB#1 + $04 = driver ptr
print(f"  DDB#1+$04 ($182E) = ${ddb1_drv:08X}")
# Is this a DDT pointer or a code pointer?
# Check first few words at the target
if ddb1_drv > 0 and ddb1_drv < 0x400000:
    target = ddb1_drv & 0xFFFFFF
    print(f"  First 8 words at ${target:06X}:")
    for off in range(0, 16, 2):
        w = bus.read_word(target + off)
        print(f"    +${off:02X}: ${w:04X}")
    print(f"  DDT+$04 at ${target+4:06X}: ${bus.read_long(target+4):08X}")
