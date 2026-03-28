#!/usr/bin/env python3
"""Trace supervisor stack pointer history during boot.

Goal: Find where SP transitions from the initial $032400 to $0426.
This will reveal whether our emulator is setting up the init job's
stack incorrectly.
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

prev_sp = cpu.a[7]
sp_changes = []
count = 0
comint_reached = False

# Track significant SP changes (more than 100 bytes from previous)
while not cpu.halted and count < config.max_instructions:
    pc = cpu.pc
    cpu.step()
    bus.tick(1)
    count += 1

    new_sp = cpu.a[7]
    delta = abs(new_sp - prev_sp)

    # Track large SP changes (more than 100 bytes) that might indicate
    # stack reinitialization rather than normal push/pop
    if delta > 100 and prev_sp != 0:
        try:
            opword = bus.read_word(pc)
        except:
            opword = 0
        entry = (count, pc, prev_sp, new_sp, delta, opword)
        sp_changes.append(entry)
        if len(sp_changes) <= 50:
            print(f"[{count:8d}] PC=${pc:06X} op=${opword:04X}"
                  f"  SP: ${prev_sp:08X} -> ${new_sp:08X}"
                  f"  (delta={delta})")

    # Also track when SP first enters the $0400-$0450 range
    if prev_sp > 0x0450 and new_sp >= 0x0400 and new_sp <= 0x0450:
        try:
            opword = bus.read_word(pc)
        except:
            opword = 0
        print(f"\n*** SP entered system area range! ***")
        print(f"[{count:8d}] PC=${pc:06X} op=${opword:04X}"
              f"  SP: ${prev_sp:08X} -> ${new_sp:08X}")
        print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
        print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")
        print(f"  SR=${cpu.sr:04X}")

    # Check for COMINT
    try:
        opword = bus.read_word(cpu.pc)
    except:
        opword = 0
    if (opword & 0xF000) == 0xA000:
        svca_num = (opword - 0xA000) // 2
        if svca_num == 0o166:  # COMINT
            print(f"\n*** COMINT about to be called! ***")
            print(f"[{count:8d}] PC=${cpu.pc:06X}")
            print(f"  SP=${cpu.a[7]:08X}  SR=${cpu.sr:04X}")
            print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
            print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")
            comint_reached = True
            break

    prev_sp = new_sp

print(f"\n=== Summary ===")
print(f"Total SP changes > 100 bytes: {len(sp_changes)}")
print(f"Instructions executed: {count}")
