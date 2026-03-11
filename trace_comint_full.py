#!/usr/bin/env python3
"""Trace COMINT execution from MOVEM through SRCH call.

Goal: Understand the full COMINT flow — what does it do between saving
registers and calling SRCH? Does it read JOBCUR? How does SRCH use it?
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

# Run until COMINT A-line trap
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
            # Show pre-COMINT state
            print(f"[{count}] COMINT A-line at PC=${pc:06X}")
            print(f"  Pre-trap JOBCUR at $041C: ${bus.read_long(0x041C):08X}")
            print(f"  SP=${cpu.a[7]:08X}  SR=${cpu.sr:04X}")

            # Execute the trap
            cpu.step()
            bus.tick(1)
            count += 1

            # Now we're in the A-line handler. Run through it to COMINT.
            # Run until we reach $682E
            while cpu.pc != 0x682E and not cpu.halted and count < config.max_instructions:
                cpu.step()
                bus.tick(1)
                count += 1

            print(f"  At COMINT entry ($682E): SP=${cpu.a[7]:08X}")
            print(f"  JOBCUR at $041C: ${bus.read_long(0x041C):08X}")

            # Now trace every instruction of COMINT for 200 steps
            print(f"\n=== COMINT execution trace ===")
            for step in range(500):
                cpc = cpu.pc
                try:
                    cop = bus.read_word(cpc)
                except:
                    cop = 0xDEAD

                sp = cpu.a[7]
                d0 = cpu.d[0]
                d1 = cpu.d[1]
                a0 = cpu.a[0]
                a5 = cpu.a[5]
                a6 = cpu.a[6]

                # Check for A-line opcodes (SVCA calls within COMINT)
                is_svca = ""
                if (cop & 0xF000) == 0xA000:
                    snum = (cop - 0xA000) // 2
                    svca_names = {
                        0o32: 'QGET', 0o33: 'QRET', 0o34: 'QADD', 0o35: 'QINS',
                        0o36: 'JRUN', 0o37: 'JWAIT', 0o42: 'TIMER', 0o43: 'SLEEP',
                        0o46: 'JLOCK', 0o47: 'JUNLOK', 0o50: 'SUPVR', 0o51: 'USRBAS',
                        0o52: 'USREND', 0o53: 'JOBIDX', 0o60: 'GETMEM', 0o66: 'SRCH',
                        0o67: 'RQST', 0o70: 'RLSE', 0o75: 'GDATES', 0o76: 'SDATES',
                        0o120: 'FSPEC', 0o166: 'COMINT',
                    }
                    name = svca_names.get(snum, f'?{oct(snum)}')
                    is_svca = f"  <<< SVCA {name} (0o{snum:o}) >>>"

                # Check for memory reads from $041C (JOBCUR)
                jobcur_val = bus.read_long(0x041C)

                # Show key info
                line = (f"  [{step:3d}] PC=${cpc:06X} op=${cop:04X}"
                        f"  SP=${sp:08X} A6=${a6:08X}"
                        f"  JOBCUR=${jobcur_val:08X}{is_svca}")
                print(line)

                # Check for reads from absolute short $041C
                # MOVEA.L $041C.W,A6 is $2C78 followed by $041C
                if cop == 0x2C78:
                    try:
                        next_word = bus.read_word(cpc + 2)
                        if next_word == 0x041C:
                            print(f"        *** READING JOBCUR ($041C) into A6 ***")
                            print(f"        *** Value will be: ${jobcur_val:08X} ***")
                    except:
                        pass

                # If we see SRCH, trace into it
                if is_svca and 'SRCH' in is_svca:
                    print(f"\n  --- SRCH SVCA called ---")
                    print(f"  D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X}")
                    print(f"  A0=${cpu.a[0]:08X} A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X}")

                cpu.step()
                bus.tick(1)
                count += 1

                if cpu.halted:
                    print(f"  CPU HALTED")
                    break

                # If we've gone past COMINT (returned to caller), stop
                # Check if we're in the module search loop
                if cpc in (0x1C6E, 0x1C72):
                    print(f"\n  *** MODULE SEARCH LOOP DETECTED at ${cpc:06X} ***")
                    # Show some context
                    print(f"  A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X}")
                    print(f"  Reading from A5: ", end="")
                    try:
                        val_a5 = bus.read_long(cpu.a[5])
                        print(f"${val_a5:08X}")
                    except:
                        print("FAULT")

                    # Count loop iterations
                    loop_count = 0
                    while cpu.pc in (0x1C58, 0x1C5A, 0x1C5C, 0x1C5E, 0x1C60,
                                     0x1C62, 0x1C64, 0x1C66, 0x1C68, 0x1C6A,
                                     0x1C6C, 0x1C6E, 0x1C70, 0x1C72, 0x1C74):
                        cpu.step()
                        bus.tick(1)
                        count += 1
                        if cpu.pc == 0x1C72:
                            loop_count += 1
                            if loop_count > 10:
                                print(f"  Confirmed infinite loop after {loop_count} iterations")
                                break
                    break

            break

    cpu.step()
    bus.tick(1)
    count += 1

# Show final system area
print(f"\n=== System area ===")
for name, addr in [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C), ("$0420", 0x0420),
    ("$0424", 0x0424), ("$0428", 0x0428), ("$042C", 0x042C),
]:
    val = bus.read_long(addr)
    print(f"  {name} (${addr:04X}): ${val:08X}")
