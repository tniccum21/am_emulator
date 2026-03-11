#!/usr/bin/env python3
"""Compare supervisor stack behavior with 68000 vs 68010 frames.

Goal: Check if the supervisor stack base at $0434 differs between CPU modes,
and trace SP from scheduler to COMINT in both modes.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from alphasim.config import SystemConfig
from alphasim.main import build_system

def run_test(use_68000_frames):
    mode = "68000" if use_68000_frames else "68010"
    print(f"\n{'='*60}")
    print(f"  Testing with {mode} exception frames")
    print(f"{'='*60}")

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
    cpu.use_68000_frames = use_68000_frames
    cpu.reset()

    count = 0
    last_scheduler_sp = 0
    scheduler_count = 0

    while not cpu.halted and count < config.max_instructions:
        pc = cpu.pc

        # Track when scheduler loads SP at $123A
        if pc == 0x123A:
            scheduler_count += 1
            # Execute the instruction
            cpu.step()
            bus.tick(1)
            count += 1
            last_scheduler_sp = cpu.a[7]
            if scheduler_count <= 3 or scheduler_count % 10 == 0:
                stack_base = bus.read_long(0x0434)
                print(f"  Scheduler #{scheduler_count}: SP loaded to ${cpu.a[7]:08X}"
                      f"  (from $0434=${stack_base:08X})")
            continue

        # Check for COMINT
        try:
            opword = bus.read_word(pc)
        except:
            opword = 0

        if (opword & 0xF000) == 0xA000:
            svca_num = (opword - 0xA000) // 2
            if svca_num == 0o166:  # COMINT
                print(f"\n  COMINT at instruction {count}, PC=${pc:06X}")
                print(f"  SP=${cpu.a[7]:08X}  SR=${cpu.sr:04X}")
                print(f"  Stack base ($0434) = ${bus.read_long(0x0434):08X}")
                print(f"  Stack used = ${last_scheduler_sp - cpu.a[7]:04X}"
                      f" ({last_scheduler_sp - cpu.a[7]} bytes)"
                      f" (from scheduler SP=${last_scheduler_sp:08X})")
                print(f"  JOBCUR ($041C) = ${bus.read_long(0x041C):08X}")
                print(f"  SYSTEM ($0400) = ${bus.read_long(0x0400):08X}")
                print(f"  Last scheduler iteration: #{scheduler_count}")

                # Now trace through COMINT MOVEM
                # Execute the A-line trap
                cpu.step()
                bus.tick(1)
                count += 1

                # Run through handler to COMINT entry
                while cpu.pc != 0x682E and not cpu.halted:
                    cpu.step()
                    bus.tick(1)
                    count += 1

                sp_at_comint = cpu.a[7]
                print(f"\n  At COMINT MOVEM ($682E): SP=${sp_at_comint:08X}")
                print(f"  MOVEM will push 48 bytes: ${sp_at_comint:08X} to ${sp_at_comint-48:08X}")
                print(f"  JOBCUR ($041C) in push range: "
                      f"{'YES - CORRUPTED!' if sp_at_comint - 48 <= 0x041C < sp_at_comint else 'No - safe'}")
                print(f"  SYSTEM ($0400) in push range: "
                      f"{'YES - CORRUPTED!' if sp_at_comint - 48 <= 0x0400 < sp_at_comint else 'No - safe'}")

                break

        cpu.step()
        bus.tick(1)
        count += 1

    return count

run_test(use_68000_frames=False)  # 68010
run_test(use_68000_frames=True)   # 68000
