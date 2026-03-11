#!/usr/bin/env python3
"""Trace what happens after COMINT — specifically the SRCH SVCA and module search."""
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

# Track ACIA output
output_bytes = bytearray()
def tx_callback(port, byte_val):
    if port == 0:
        output_bytes.append(byte_val)
acia.tx_callback = tx_callback

cpu.reset()

# Run until COMINT is called (SVCA #118, opcode $A0EC)
count = 0
comint_found = False
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc
    try:
        opword = bus.read_word(pc_before)
    except:
        opword = 0

    if opword == 0xA0EC and not comint_found:
        comint_found = True
        print(f"\n=== COMINT found at PC=${pc_before:06X}, instruction {count} ===")
        print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
        print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")
        print(f"  SR=${cpu.sr:04X}")

        # Dump memory at the calling location
        print(f"\n  Code around COMINT call:")
        for base in range(pc_before - 16, pc_before + 48, 2):
            try:
                w = bus.read_word(base)
            except:
                w = 0xDEAD
            marker = " <-- COMINT" if base == pc_before else ""
            print(f"    ${base:06X}: ${w:04X}{marker}")

        # Now trace instruction-by-instruction for 2000 instructions
        print(f"\n  === Tracing after COMINT ===")
        for step in range(2000):
            pc_b = cpu.pc
            try:
                op = bus.read_word(pc_b)
            except:
                op = 0xDEAD

            cpu.step()
            bus.tick(1)
            count += 1

            # Detect A-line traps
            is_svca = (op & 0xF000) == 0xA000
            svca_info = ""
            if is_svca:
                snum = (op - 0xA000) // 2
                svca_names = {
                    0o32: 'QGET', 0o33: 'QRET', 0o34: 'QADD', 0o35: 'QINS',
                    0o36: 'JRUN', 0o37: 'JWAIT', 0o42: 'TIMER', 0o43: 'SLEEP',
                    0o46: 'JLOCK', 0o47: 'JUNLOK', 0o50: 'SUPVR', 0o51: 'USRBAS',
                    0o52: 'USREND', 0o53: 'JOBIDX', 0o60: 'GETMEM', 0o66: 'SRCH',
                    0o67: 'RQST', 0o70: 'RLSE', 0o75: 'GDATES', 0o120: 'FSPEC',
                    0o166: 'COMINT',
                }
                name = svca_names.get(snum, f'?{oct(snum)}')
                svca_info = f" *** SVCA #{snum} ({name})"

            # Log notable events
            if is_svca or step < 50 or step % 100 == 0:
                regs = f"D0=${cpu.d[0]:08X} A0=${cpu.a[0]:08X} A5=${cpu.a[5]:08X}"
                print(f"    [{step:4d}] PC=${pc_b:06X} op=${op:04X} {regs}{svca_info}")

            # If we hit the $1C6E/$1C72 area, dump state
            if 0x1C60 <= pc_b <= 0x1C78 and step > 10:
                print(f"    *** ENTERED MODULE SEARCH at step {step}")
                print(f"        D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
                print(f"        A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")
                # Dump memory around A5
                a5 = cpu.a[5]
                if a5 < 0x400000:
                    print(f"        Memory at A5=${a5:06X}:")
                    for base in range(a5, min(a5 + 32, 0x400000), 2):
                        try:
                            w = bus.read_word(base)
                        except:
                            w = 0xDEAD
                        print(f"          ${base:06X}: ${w:04X}")
                else:
                    print(f"        A5=${a5:08X} OUTSIDE RAM!")

                # Also check SYSBAS
                sysbas = bus.read_long(0x0414)
                print(f"        SYSBAS ($0414): ${sysbas:08X}")
                # Dump first few bytes at SYSBAS
                if sysbas < 0x400000:
                    print(f"        Memory at SYSBAS:")
                    for base in range(sysbas, min(sysbas + 64, 0x400000), 2):
                        try:
                            w = bus.read_word(base)
                        except:
                            w = 0xDEAD
                        print(f"          ${base:06X}: ${w:04X}")

                break

        break

    cpu.step()
    bus.tick(1)
    count += 1

if not comint_found:
    print(f"COMINT not found in {count} instructions")
    print(f"Final PC=${cpu.pc:06X}, LED={led.value:02X}")
