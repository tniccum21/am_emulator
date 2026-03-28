#!/usr/bin/env python3
"""Trace the SSP value at COMINT call and how SRCH reads JOBCUR.

Key questions:
1. What is SSP when COMINT (SVCA #118, opcode $A0EC) is called?
2. How did SSP get there from initial $032400?
3. Does the COMINT MOVEM really overwrite JOBCUR?
4. How does SRCH (SVCA #54, opcode $A06C) read JOBCUR?
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
    max_instructions=20_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None

cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}")
print(f"       Initial SSP from ROM: ${cpu.ssp:08X}")

# Track SSP changes to see how it goes from $032400 to whatever
ssp_history = []
prev_ssp = cpu.a[7]

# Track where A7 changes significantly
count = 0
comint_found = False
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc
    sr_before = cpu.sr
    a7_before = cpu.a[7]

    # Check for COMINT opcode
    try:
        opword = bus.read_word(pc_before)
    except:
        opword = 0

    if opword == 0xA0EC and not comint_found:
        comint_found = True
        is_super = bool(sr_before & 0x2000)
        print(f"\n=== COMINT found at PC=${pc_before:06X}, instruction {count} ===")
        print(f"  SR=${sr_before:04X} (supervisor={is_super})")
        print(f"  A7(SP)=${a7_before:08X}")
        print(f"  SSP=${cpu.ssp:08X}  USP=${cpu.usp:08X}")
        print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
        print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

        # Show what JOBCUR currently is BEFORE the A-line trap
        jobcur_val = bus.read_long(0x041C)
        print(f"\n  JOBCUR ($041C) before COMINT: ${jobcur_val:08X}")
        jobtbl_val = bus.read_long(0x0418)
        print(f"  JOBTBL ($0418) before COMINT: ${jobtbl_val:08X}")
        sysbas_val = bus.read_long(0x0414)
        print(f"  SYSBAS ($0414) before COMINT: ${sysbas_val:08X}")

        # Show memory around the stack pointer
        print(f"\n  Memory around SP=${a7_before:08X}:")
        for base in range(max(0, a7_before - 16), min(a7_before + 32, 0x400000), 2):
            w = bus.read_word(base)
            marker = " <-- SP" if base == a7_before else ""
            print(f"    ${base:06X}: ${w:04X}{marker}")

        # Now step through the A-line trap and COMINT handler
        print(f"\n=== Stepping through COMINT entry ===")
        for step in range(200):
            pc_b = cpu.pc
            sr_b = cpu.sr
            a7_b = cpu.a[7]
            try:
                op = bus.read_word(pc_b)
            except:
                op = 0xDEAD

            cpu.step()
            bus.tick(1)
            count += 1

            a7_after = cpu.a[7]

            # Show all steps where SP changes, or the first 30 steps
            if a7_after != a7_b or step < 30:
                sp_delta = (a7_after - a7_b) & 0xFFFFFFFF
                if sp_delta > 0x80000000:
                    sp_delta = sp_delta - 0x100000000
                sp_info = f" ΔSP={sp_delta:+d}" if sp_delta != 0 else ""

                # Check if this is a MOVEM
                movem_info = ""
                if (op & 0xFFF8) == 0x48E0:  # MOVEM.L reglist,-(An)
                    reg = op & 7
                    try:
                        regmask = bus.read_word(pc_b + 2)
                    except:
                        regmask = 0
                    nregs = bin(regmask).count('1')
                    movem_info = f" MOVEM.L {nregs} regs,-(A{reg})"

                # Check for A-line traps
                svca_info = ""
                if (op & 0xF000) == 0xA000:
                    snum = (op - 0xA000) // 2
                    svca_names = {
                        0o32: 'QGET', 0o33: 'QRET', 0o34: 'QADD',
                        0o66: 'SRCH', 0o67: 'RQST', 0o120: 'FSPEC',
                        0o166: 'COMINT',
                    }
                    name = svca_names.get(snum, f'?{oct(snum)}')
                    svca_info = f" *** SVCA #{snum} ({name})"

                print(f"  [{step:3d}] PC=${pc_b:06X} op=${op:04X} SP=${a7_b:08X}→${a7_after:08X}{sp_info}{movem_info}{svca_info}")

            # After MOVEM at step ~1-3, check JOBCUR
            if step in [5, 10, 15, 20, 25, 30]:
                jobcur_now = bus.read_long(0x041C)
                print(f"         *** JOBCUR check at step {step}: ${jobcur_now:08X}")

            # Check if we hit the SRCH handler
            if (op & 0xF000) == 0xA000:
                snum = (op - 0xA000) // 2
                if snum == 0o66:  # SRCH
                    print(f"\n=== SRCH (SVCA #54) called at step {step} ===")
                    print(f"  SP=${a7_b:08X} → ${a7_after:08X}")
                    print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
                    print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")
                    jobcur_at_srch = bus.read_long(0x041C)
                    print(f"  JOBCUR ($041C) at SRCH entry: ${jobcur_at_srch:08X}")

                    # Step through SRCH handler
                    print(f"\n  === SRCH handler trace ===")
                    reads_in_range = []
                    for srch_step in range(100):
                        pc_s = cpu.pc
                        try:
                            sop = bus.read_word(pc_s)
                        except:
                            sop = 0xDEAD

                        cpu.step()
                        bus.tick(1)
                        count += 1

                        regs = f"D0=${cpu.d[0]:08X} D6=${cpu.d[6]:08X} A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X}"
                        print(f"    [{srch_step:3d}] PC=${pc_s:06X} op=${sop:04X} {regs}")

                        # Check if we're at $1C6E (the known hang point)
                        if pc_s == 0x1C6E:
                            print(f"    *** HIT $1C6E (module search entry) ***")
                            print(f"    A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X}")
                            a6 = cpu.a[6] & 0xFFFFFF
                            if a6 < 0x400000:
                                print(f"    Memory at A6=${a6:06X}:")
                                for off in range(0, 32, 2):
                                    w = bus.read_word(a6 + off)
                                    print(f"      ${a6+off:06X}: ${w:04X}")
                            break
                    break
        break

    cpu.step()
    bus.tick(1)
    count += 1

    # Track major A7 changes (supervisor mode)
    if cpu.a[7] != a7_before and abs((cpu.a[7] - a7_before) & 0xFFFFFFFF) > 0x100:
        if bool(cpu.sr & 0x2000):
            ssp_history.append((count, pc_before, a7_before, cpu.a[7]))

if not comint_found:
    print(f"COMINT not found in {count} instructions")

# Show significant SSP changes
if ssp_history:
    print(f"\n=== Significant SSP changes ({len(ssp_history)}) ===")
    for cnt, pc, old, new in ssp_history[:20]:
        print(f"  [{cnt:8d}] PC=${pc:06X}: SP ${old:08X} → ${new:08X}")
