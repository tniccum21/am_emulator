#!/usr/bin/env python3
"""Trace the A-line trap dispatch handler at $6F6.

Goal: Understand how the ROM's A-line handler processes the exception frame,
dispatches to SVCA handlers (specifically COMINT), and what happens to SP
through the entire process. This determines whether the ROM expects 68000
or 68010 exception frames.

We trace with BOTH frame sizes to compare behavior.
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from alphasim.config import SystemConfig
from alphasim.main import build_system

def run_trace(use_68000_frames):
    mode = "68000 (6-byte)" if use_68000_frames else "68010 (8-byte)"
    sep = '=' * 70
    print(f"\n{sep}")
    print(f"  A-LINE DISPATCH TRACE -- {mode} frames")
    print(f"{sep}")

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

    # Run until we see COMINT A-line trap (SVCA $66 = COMINT, opcode $A0CC)
    # COMINT is SVCA 0o166 = 118 decimal = $76, opcode $A0EC
    # Actually let's find the right opcode by catching any A-line near COMINT
    count = 0
    comint_found = False

    while not cpu.halted and count < config.max_instructions:
        pc = cpu.pc

        try:
            opword = bus.read_word(pc)
        except:
            opword = 0

        # Catch the first A-line trap that leads to COMINT ($682E area)
        # We know COMINT is at $682E from previous traces
        # The A-line trap for COMINT is opcode $A0CC (SVCA 0o146 = COMINT)
        # Actually from the previous trace, COMINT SVCA is 0o166
        # SVCA number = (opword - $A000) / 2 = (opword & $FFF) / 2
        # 0o166 = 118 decimal, opcode = $A000 + 118*2 = $A0EC

        if (opword & 0xF000) == 0xA000:
            svca_num = (opword - 0xA000) // 2
            if svca_num == 0o166:  # COMINT
                comint_found = True
                pre_trap_sp = cpu.a[7]
                pre_trap_sr = cpu.sr
                print(f"\n[{count}] COMINT A-line trap at PC=${pc:06X}")
                print(f"  opword=${opword:04X}  SVCA=0o{svca_num:o} (COMINT)")
                print(f"  Pre-trap SP=${pre_trap_sp:08X}  SR=${pre_trap_sr:04X}")
                print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
                print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

                # Execute the A-line trap instruction
                cpu.step()
                bus.tick(1)
                count += 1

                post_trap_sp = cpu.a[7]
                post_trap_pc = cpu.pc
                print(f"\n  After trap:")
                print(f"  SP=${post_trap_sp:08X}  PC=${post_trap_pc:06X}")
                print(f"  SP delta = {pre_trap_sp - post_trap_sp} bytes")

                # Show what's on the stack
                print(f"\n  Stack contents (SP=${post_trap_sp:08X}):")
                for offset in range(0, 16, 2):
                    addr = post_trap_sp + offset
                    try:
                        w = bus.read_word(addr)
                        print(f"    SP+{offset:2d} (${addr:06X}): ${w:04X}")
                    except:
                        print(f"    SP+{offset:2d} (${addr:06X}): ???")

                # Now trace each instruction through the handler
                print(f"\n  === A-line handler trace ===")
                for step in range(200):
                    handler_pc = cpu.pc
                    try:
                        hop = bus.read_word(handler_pc)
                    except:
                        hop = 0xDEAD

                    sp = cpu.a[7]
                    sr = cpu.sr
                    d7 = cpu.d[7]
                    d0 = cpu.d[0]
                    a0 = cpu.a[0]
                    a6 = cpu.a[6]

                    print(f"  [{step:3d}] PC=${handler_pc:06X} op=${hop:04X}"
                          f"  SP=${sp:08X} SR=${sr:04X}"
                          f"  D0=${d0:08X} D7=${d7:08X}"
                          f"  A0=${a0:08X} A6=${a6:08X}")

                    # If we reach the MOVEM at $682E, we're done tracing
                    if handler_pc == 0x682E:
                        print(f"\n  === Reached COMINT MOVEM at $682E ===")
                        print(f"  SP=${sp:08X} (will push 48 bytes from here)")
                        print(f"  Push range: ${sp:08X} down to ${sp-48:08X}")
                        print(f"  JOBCUR is at $041C")

                        # Calculate which register hits $041C
                        # MOVEM.L D0-D5/A0-A5,-(SP) pushes A5 first, then A4...A0, D5...D0
                        addr = sp
                        regs = [('A5', cpu.a[5]), ('A4', cpu.a[4]), ('A3', cpu.a[3]),
                                ('A2', cpu.a[2]), ('A1', cpu.a[1]), ('A0', cpu.a[0]),
                                ('D5', cpu.d[5]), ('D4', cpu.d[4]), ('D3', cpu.d[3]),
                                ('D2', cpu.d[2]), ('D1', cpu.d[1]), ('D0', cpu.d[0])]
                        print(f"\n  MOVEM push order (pre-decrement, A5 first):")
                        for name, val in regs:
                            addr -= 4
                            overlap = ""
                            if addr <= 0x041F and addr + 3 >= 0x041C:
                                overlap = " *** OVERLAPS JOBCUR ($041C) ***"
                            if addr <= 0x041B and addr + 3 >= 0x0418:
                                overlap += " *** OVERLAPS JOBTBL ($0418) ***"
                            print(f"    {name}=${val:08X} → ${addr:06X}-${addr+3:06X}{overlap}")
                        break

                    # If we hit an RTE, note it
                    if hop == 0x4E73:
                        print(f"    *** RTE — will restore SP ***")

                    # If we hit a JMP or JSR, note the target
                    if (hop & 0xFFC0) == 0x4EC0:  # JMP
                        print(f"    *** JMP instruction ***")
                    if (hop & 0xFFC0) == 0x4E80:  # JSR
                        print(f"    *** JSR instruction ***")

                    cpu.step()
                    bus.tick(1)
                    count += 1

                    if cpu.halted:
                        print(f"    *** CPU HALTED ***")
                        break

                # Show system area after MOVEM
                if cpu.pc == 0x682E and not cpu.halted:
                    # Execute the MOVEM
                    cpu.step()
                    bus.tick(1)
                    count += 1
                    print(f"\n  After MOVEM: SP=${cpu.a[7]:08X}")
                    print(f"\n  System area $0400-$0430:")
                    for addr in range(0x0400, 0x0430, 4):
                        val = bus.read_long(addr)
                        labels = {
                            0x0400: "SYSTEM", 0x0404: "DEVTBL", 0x0408: "DDBCHN",
                            0x040C: "MEMBAS", 0x0410: "MEMEND", 0x0414: "SYSBAS",
                            0x0418: "JOBTBL", 0x041C: "JOBCUR"
                        }
                        label = labels.get(addr, "")
                        print(f"    ${addr:04X}: ${val:08X}  {label}")

                break

        cpu.step()
        bus.tick(1)
        count += 1

    if not comint_found:
        print(f"  COMINT not reached in {count} instructions")

    return count

# Run with both frame sizes
run_trace(use_68000_frames=False)  # 68010 mode
run_trace(use_68000_frames=True)   # 68000 mode
