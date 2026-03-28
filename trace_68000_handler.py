#!/usr/bin/env python3
"""Trace A-line handler with 68000 frames, focusing on $041C (JOBCUR).

With 68000 frames, SP at COMINT's MOVEM = $041C. The MOVEM pushes BELOW
$041C, so it doesn't corrupt JOBCUR directly. But the exception frame
puts SR at $041C. And the handler's dispatch push might also affect $041C.

Let's trace exactly what happens to address $041C through the entire
A-line dispatch + COMINT flow.
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
cpu.use_68000_frames = True
cpu.reset()

# Run until COMINT
count = 0
while not cpu.halted and count < config.max_instructions:
    pc = cpu.pc
    try:
        opword = bus.read_word(pc)
    except:
        opword = 0

    if (opword & 0xF000) == 0xA000:
        svca_num = (opword - 0xA000) // 2
        if svca_num == 0o166:  # COMINT
            print(f"[{count}] COMINT A-line at PC=${pc:06X}")
            print(f"  Pre-trap SP=${cpu.a[7]:08X}  SR=${cpu.sr:04X}")
            print(f"  Pre-trap JOBCUR ($041C) = ${bus.read_long(0x041C):08X}")

            # Watch $041C through the entire process
            # Step: execute A-line trap
            cpu.step()
            bus.tick(1)
            count += 1
            print(f"\n  After A-line trap (in handler $6F6):")
            print(f"  SP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}")
            print(f"  $041C = ${bus.read_long(0x041C):08X}")

            # Run through handler to COMINT ($682E)
            handler_steps = 0
            while cpu.pc != 0x682E and not cpu.halted and handler_steps < 50:
                hpc = cpu.pc
                try:
                    hop = bus.read_word(hpc)
                except:
                    hop = 0xDEAD
                sp = cpu.a[7]
                jobcur = bus.read_long(0x041C)
                print(f"  handler[{handler_steps}] PC=${hpc:06X} op=${hop:04X}"
                      f"  SP=${sp:08X}  $041C=${jobcur:08X}")
                cpu.step()
                bus.tick(1)
                count += 1
                handler_steps += 1

            print(f"\n  At COMINT entry ($682E):")
            print(f"  SP=${cpu.a[7]:08X}")
            print(f"  $041C = ${bus.read_long(0x041C):08X}")

            # Execute COMINT MOVEM
            cpu.step()
            bus.tick(1)
            count += 1
            print(f"\n  After COMINT MOVEM:")
            print(f"  SP=${cpu.a[7]:08X}")
            print(f"  $041C = ${bus.read_long(0x041C):08X}")

            # Continue COMINT to the SRCH call
            for step in range(30):
                cpc = cpu.pc
                try:
                    cop = bus.read_word(cpc)
                except:
                    cop = 0xDEAD
                sp = cpu.a[7]
                if (cop & 0xF000) == 0xA000:
                    snum = (cop - 0xA000) // 2
                    if snum == 0o66:  # SRCH
                        print(f"\n  SRCH call at PC=${cpc:06X}")
                        print(f"  SP=${sp:08X}")
                        print(f"  $041C = ${bus.read_long(0x041C):08X}")
                        print(f"  D6=${cpu.d[6]:08X}  A6=${cpu.a[6]:08X}")

                        # Execute SRCH trap
                        cpu.step()
                        bus.tick(1)
                        count += 1

                        # Run to SRCH entry
                        while cpu.pc != 0x1C30 and not cpu.halted:
                            cpu.step()
                            bus.tick(1)
                            count += 1

                        print(f"\n  At SRCH entry ($1C30):")
                        print(f"  SP=${cpu.a[7]:08X}")
                        print(f"  $041C = ${bus.read_long(0x041C):08X}")

                        # Execute SRCH MOVEM
                        cpu.step()
                        bus.tick(1)
                        count += 1
                        print(f"  After SRCH MOVEM:")
                        print(f"  SP=${cpu.a[7]:08X}")
                        print(f"  $041C = ${bus.read_long(0x041C):08X}")

                        # Trace to the JOBCUR read at $1C58
                        for s in range(30):
                            spc = cpu.pc
                            try:
                                sop = bus.read_word(spc)
                            except:
                                sop = 0xDEAD

                            if spc == 0x1C58:
                                print(f"\n  At $1C58: MOVEA.L ($041C).W,A6")
                                print(f"  $041C = ${bus.read_long(0x041C):08X}")
                                print(f"  This value will be loaded into A6!")

                                # Check what's at the target address
                                target = bus.read_long(0x041C) & 0xFFFFFF
                                print(f"  Target address (masked): ${target:06X}")
                                try:
                                    target_val = bus.read_long(target)
                                    print(f"  [${target:06X}] = ${target_val:08X}")
                                    target_0c = bus.read_long(target + 0x0C)
                                    print(f"  [${target+0xC:06X}] (offset $0C) = ${target_0c:08X}")
                                    if target_0c == 0:
                                        print(f"  *** TST.L ($0C,A6) will be ZERO ***")
                                        print(f"  *** BEQ will branch to $1C96 ***")
                                        print(f"  *** Module search SKIPPED! ***")
                                    else:
                                        print(f"  *** TST.L ($0C,A6) will be NON-ZERO ***")
                                        print(f"  *** Module search ENTERED ***")
                                except Exception as e:
                                    print(f"  [${target:06X}] = FAULT: {e}")

                            cpu.step()
                            bus.tick(1)
                            count += 1

                            if spc in (0x1C6E, 0x1C72):
                                print(f"  *** MODULE SEARCH LOOP at ${spc:06X} ***")
                                break
                            if spc == 0x1C96:
                                print(f"  *** SKIPPED module search (at $1C96) ***")
                                break
                        break
                cpu.step()
                bus.tick(1)
                count += 1
            break

    cpu.step()
    bus.tick(1)
    count += 1
