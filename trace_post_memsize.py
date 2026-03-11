#!/usr/bin/env python3
"""Trace ROM code right after memory sizing to find any clear pass."""
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

# Run until the memory test exit (PC=$0080B6)
count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x0080B6:
        print(f"=== Memory test exit at instruction {count} ===")
        print(f"  A4=${cpu.a[4]:08X} (MEMEND candidate)")
        print(f"  D7=${cpu.d[7]:08X} (test pattern)")
        print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
        print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

        # Trace next 200 instructions to see what happens after sizing
        print(f"\n=== Post-sizing code ===")
        for step in range(200):
            pc = cpu.pc
            try:
                op = bus.read_word(pc)
            except:
                op = 0xDEAD

            regs = f"D0=${cpu.d[0]:08X} D7=${cpu.d[7]:08X} A0=${cpu.a[0]:08X} A4=${cpu.a[4]:08X}"
            print(f"  [{step:3d}] PC=${pc:06X} op=${op:04X} {regs}")

            cpu.step()
            bus.tick(1)
            count += 1

            # Check if D7 changes (might be loading a clear pattern like 0)
            # Check if we see any write loop to RAM
            if cpu.d[7] == 0 and step > 5:
                print(f"    *** D7 = 0 (potential clear pattern)")

            # If LED changes, note it
        break

    cpu.step()
    bus.tick(1)
    count += 1

# Also dump the ROM code from $8096 to $80FF to see what follows
print(f"\n=== ROM code $8096-$8100 ===")
for addr in range(0x808096 - 0x800000 + 0x800000, 0x808100 - 0x800000 + 0x800000, 2):
    rom_addr = addr
    try:
        w = bus.read_word(rom_addr)
    except:
        w = 0xDEAD
    print(f"  ${rom_addr:06X}: ${w:04X}")
