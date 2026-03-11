#!/usr/bin/env python3
"""Trace driver execution with the patched disk image.

The driver at $7AC2 is now loaded but gets stuck. This script traces
what the driver is doing — specifically what registers it reads and
what values it gets back from the SASI controller.
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
    disk_image_path=Path("images/AMOS_1-3_Boot_OS_patched.img"),
    trace_enabled=False,
    max_instructions=50_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None

# Find SASI device
sasi = None
for start, end, dev in bus._devices:
    if start == 0xFFFFE0:
        sasi = dev
        break

# Instrument SASI reads to see what the driver accesses
import types

orig_read = sasi.read
sasi_accesses = []

def tracked_read(self, address, size):
    val = orig_read(address, size)
    if len(sasi_accesses) < 1000:  # Limit logging
        reg = address - 0xFFFFE0
        sasi_accesses.append(('R', reg, val, cpu.pc))
    return val

orig_write = sasi.write

def tracked_write(self, address, size, value):
    if len(sasi_accesses) < 1000:
        reg = address - 0xFFFFE0
        sasi_accesses.append(('W', reg, value, cpu.pc))
    orig_write(address, size, value)

sasi.read = types.MethodType(tracked_read, sasi)
sasi.write = types.MethodType(tracked_write, sasi)

cpu.reset()

# Run to scheduler or driver entry
DRIVER_BASE = 0x7AC2
DRIVER_END = DRIVER_BASE + 2040

count = 0
first_driver_entry = None
driver_entry_count = 0
driver_stuck_pc = None
driver_pc_repeat = 0
last_driver_pc = None
scheduler_reached = False

# Track PC history in driver
driver_pcs = []

while not cpu.halted and count < 10_000_000:  # Reduced limit
    pc = cpu.pc

    # Detect entry into driver code
    if DRIVER_BASE <= pc < DRIVER_END:
        if first_driver_entry is None:
            first_driver_entry = count
            print(f"First driver entry at instruction {count}, PC=${pc:06X}")
            # Show CPU state
            print(f"  D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D2=${cpu.d[2]:08X} D3=${cpu.d[3]:08X}")
            print(f"  D4=${cpu.d[4]:08X} D5=${cpu.d[5]:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X}")
            print(f"  A0=${cpu.a[0]:08X} A1=${cpu.a[1]:08X} A2=${cpu.a[2]:08X} A3=${cpu.a[3]:08X}")
            print(f"  A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X} SP=${cpu.a[7]:08X}")
            print(f"  SR=${cpu.sr:04X}")

        if len(driver_pcs) < 500:
            driver_pcs.append(pc)

        # Detect stuck loop
        if pc == last_driver_pc:
            driver_pc_repeat += 1
            if driver_pc_repeat == 100:
                driver_stuck_pc = pc
                print(f"\nDriver STUCK at PC=${pc:06X} (offset +${pc - DRIVER_BASE:04X}) at instruction {count}")
                print(f"  CPU state:")
                print(f"  D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D2=${cpu.d[2]:08X} D3=${cpu.d[3]:08X}")
                print(f"  D4=${cpu.d[4]:08X} D5=${cpu.d[5]:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X}")
                print(f"  A0=${cpu.a[0]:08X} A1=${cpu.a[1]:08X} A2=${cpu.a[2]:08X} A3=${cpu.a[3]:08X}")
                print(f"  A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X} SP=${cpu.a[7]:08X}")

                # Decode the loop
                print(f"\n  Memory around stuck PC:")
                for addr in range(max(DRIVER_BASE, pc - 16), min(DRIVER_END, pc + 16), 2):
                    w = bus.read_word(addr)
                    marker = " <-- STUCK" if addr == pc else ""
                    print(f"    ${addr:06X}: ${w:04X}{marker}")

                # Show what register A5 points to (common pattern: A5 = SASI base)
                if 0xFFFFE0 <= cpu.a[5] <= 0xFFFFE7:
                    print(f"\n  A5 = ${cpu.a[5]:08X} → SASI register {cpu.a[5] - 0xFFFFE0}")
                elif 0xFFFE00 <= cpu.a[5] <= 0xFFFFFF:
                    print(f"\n  A5 = ${cpu.a[5]:08X} → I/O space")
                else:
                    print(f"\n  A5 = ${cpu.a[5]:08X} → RAM/other")
                    # Show what's at (A5)
                    try:
                        a5_val = bus.read_byte(cpu.a[5] & 0xFFFFFF)
                        print(f"  (A5) = ${a5_val:02X}")
                    except:
                        print(f"  (A5) = <read error>")

                break
        else:
            driver_pc_repeat = 0
        last_driver_pc = pc
    else:
        if driver_pcs and last_driver_pc is not None and DRIVER_BASE <= last_driver_pc < DRIVER_END:
            # Just exited driver
            driver_entry_count += 1
            if driver_entry_count <= 5:
                print(f"  Driver exit #{driver_entry_count} at instruction {count}, returned to PC=${pc:06X}")

    if pc == 0x1250:
        scheduler_reached = True
        print(f"Scheduler reached at instruction {count}")
        break

    cpu.step()
    bus.tick(1)
    count += 1

if not scheduler_reached and driver_stuck_pc is None:
    print(f"Stopped at instruction {count}, PC=${cpu.pc:06X}")

# Show SASI access log
print(f"\n{'='*60}")
print(f"SASI REGISTER ACCESS LOG")
print(f"{'='*60}")
print(f"Total accesses logged: {len(sasi_accesses)}")

if sasi_accesses:
    # Show first 50 accesses
    print(f"\nFirst 50 accesses:")
    for i, (rw, reg, val, pc) in enumerate(sasi_accesses[:50]):
        in_driver = "DRV" if DRIVER_BASE <= pc < DRIVER_END else "ROM"
        print(f"  [{i:3d}] {rw} reg{reg} {'='*1} ${val:02X} PC=${pc:06X} ({in_driver})")

    # Show last 20 accesses
    if len(sasi_accesses) > 50:
        print(f"\nLast 20 accesses:")
        for i, (rw, reg, val, pc) in enumerate(sasi_accesses[-20:]):
            in_driver = "DRV" if DRIVER_BASE <= pc < DRIVER_END else "ROM"
            idx = len(sasi_accesses) - 20 + i
            print(f"  [{idx:3d}] {rw} reg{reg} {'='*1} ${val:02X} PC=${pc:06X} ({in_driver})")

    # Summarize accesses from within the driver
    driver_accesses = [(rw, reg, val, pc) for rw, reg, val, pc in sasi_accesses
                       if DRIVER_BASE <= pc < DRIVER_END]
    print(f"\nDriver-initiated accesses: {len(driver_accesses)}")
    if driver_accesses:
        print(f"  First 20:")
        for i, (rw, reg, val, pc) in enumerate(driver_accesses[:20]):
            offset = pc - DRIVER_BASE
            print(f"    {rw} reg{reg} = ${val:02X} at driver+${offset:04X}")

# Show driver PC trace
if driver_pcs:
    print(f"\n{'='*60}")
    print(f"DRIVER PC TRACE (first 100 unique PCs)")
    print(f"{'='*60}")
    seen = set()
    for pc in driver_pcs:
        if pc not in seen:
            seen.add(pc)
            offset = pc - DRIVER_BASE
            w = bus.read_word(pc)
            print(f"  ${pc:06X} (+${offset:04X}): ${w:04X}")
            if len(seen) >= 100:
                break

# Check if DDT is intact
print(f"\n{'='*60}")
print(f"DDT STATE")
print(f"{'='*60}")
for off in [0, 4, 8, 0x0C, 0x34]:
    addr = 0x7038 + off
    val = bus.read_long(addr)
    labels = {0: "status", 4: "driver ptr", 8: "link", 0xC: "device data", 0x34: "name"}
    print(f"  DDT+${off:02X} (${addr:06X}): ${val:08X} ({labels.get(off, '')})")
