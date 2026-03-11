#!/usr/bin/env python3
"""Trace the ROM memory test to understand its passes.

Watch for LED=0F (the memory test LED code) and trace the ROM code
that writes to RAM during this phase.
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
acia.tx_callback = lambda port, val: None

cpu.reset()

count = 0
in_memtest = False
memtest_pcs = set()
prev_led = 0
memtest_start = 0
memtest_end = 0

# Track what happens during LED=0F (memory sizing)
# Also track LED=06 (early memory test) and LED=0B
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc

    cpu.step()
    bus.tick(1)
    count += 1

    # Detect LED changes
    if led.value != prev_led:
        if prev_led == 0x0F:
            print(f"\n[{count:8d}] LED left 0F → {led.value:02X}")
            print(f"  Memory test ran from instruction {memtest_start} to {count}")
            print(f"  Unique PCs in test: {len(memtest_pcs)}")
            # Show the PCs used during memtest
            for pc in sorted(memtest_pcs):
                try:
                    w = bus.read_word(pc)
                except:
                    w = 0xDEAD
                print(f"    ${pc:06X}: ${w:04X}")
            in_memtest = False

        if led.value == 0x0F:
            in_memtest = True
            memtest_start = count
            memtest_pcs = set()
            print(f"\n[{count:8d}] LED → 0F (memory test start)")
            print(f"  PC=${pc_before:06X}")
            print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
            print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

        prev_led = led.value

    if in_memtest:
        memtest_pcs.add(pc_before)
        # Show first 100 instructions of the memory test
        if count - memtest_start < 100:
            try:
                op = bus.read_word(pc_before)
            except:
                op = 0xDEAD
            regs = f"D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} A0=${cpu.a[0]:08X} A1=${cpu.a[1]:08X}"
            print(f"  [{count-memtest_start:4d}] PC=${pc_before:06X} op=${op:04X} {regs}")

# Check if MEMEND was set properly
memend = bus.read_long(0x0410)
print(f"\n=== Post-boot MEMEND ($0410) = ${memend:08X} ===")

# Check RAM at a few addresses to see the final state
print(f"\n=== RAM state after memory test ===")
for addr in [0x1000, 0x10000, 0x100000, 0x200000, 0x3FF000]:
    b = bus._read_byte_physical(addr)
    print(f"  ${addr:06X}: ${b:02X}")
