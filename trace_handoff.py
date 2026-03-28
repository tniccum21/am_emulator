#!/usr/bin/env python3
"""Trace the ROM-to-OS handoff: where does JMP @A6 go after loading AMOSL.MON?"""
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
cpu.reset()

# We know LED goes 06→0B→00→0E→0F→00
# LED=0E (14) is written at the handoff point (L0170: MOVB #14.,HW.LED)
# We want to catch when LED changes to 0E and see the next JMP

led_history = []
prev_led = led.value
prev_pc = cpu.pc
count = 0
handoff_found = False

while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc
    cpu.step()
    bus.tick(1)
    count += 1

    if led.value != prev_led:
        print(f"[{count:7d}] LED {prev_led:02X} → {led.value:02X}  PC=${pc_before:06X}")
        prev_led = led.value

        # LED=0E is the handoff LED write
        if led.value == 0x0E and not handoff_found:
            handoff_found = True
            print(f"\n=== HANDOFF POINT ===")
            print(f"  PC=${cpu.pc:06X}  SR=${cpu.sr:04X}")
            print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
            print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

            # Read memory around where the JMP will go
            # The ROM does: MOV OS.ENT,A6 then JMP @A6
            # A6 should now contain the OS entry point (or will after next instruction)
            # Let's step a few more and watch A6
            for step in range(20):
                pc_b = cpu.pc
                a6_b = cpu.a[6]
                cpu.step()
                bus.tick(1)
                count += 1
                a6_a = cpu.a[6]
                # Try to read the opcode
                try:
                    opword = bus.read_word(pc_b)
                except:
                    opword = 0
                print(f"  step {step}: PC=${pc_b:06X} opcode=${opword:04X}  A6=${a6_b:08X}→${a6_a:08X}  D0=${cpu.d[0]:08X}")

                # Check if we jumped to RAM (PC < $4000 is ROM, >= $4000 is RAM after phantom off)
                if cpu.pc >= 0x4000 and pc_b < 0x4000:
                    print(f"\n  *** JUMPED TO RAM: PC=${cpu.pc:06X} ***")
                elif cpu.pc < 0x800000 and pc_b >= 0x800000:
                    print(f"\n  *** JUMPED FROM ROM TO RAM: PC=${cpu.pc:06X} ***")

            # Dump memory at the OS entry point
            print(f"\n  Current PC=${cpu.pc:06X}, A6=${cpu.a[6]:08X}")
            print(f"  Memory at current PC:")
            for base in range(cpu.pc, cpu.pc + 64, 16):
                words = []
                for off in range(0, 16, 2):
                    try:
                        w = bus.read_word(base + off)
                    except:
                        w = 0xDEAD
                    words.append(f"{w:04X}")
                print(f"    ${base:06X}: {' '.join(words)}")

            # Also check what's at address 0 (where AMOSL.MON was loaded)
            print(f"\n  Memory at $000000 (start of loaded OS):")
            for base in range(0, 128, 16):
                words = []
                for off in range(0, 16, 2):
                    try:
                        w = bus.read_word(base + off)
                    except:
                        w = 0xDEAD
                    words.append(f"{w:04X}")
                print(f"    ${base:06X}: {' '.join(words)}")
            break

if not handoff_found:
    print("LED never reached 0E — handoff not found")
    print(f"Final: PC=${cpu.pc:06X}, LED={led.value:02X}, {count} instructions")
