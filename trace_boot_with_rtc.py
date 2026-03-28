#!/usr/bin/env python3
"""Boot with RTC device and trace progress — see if boot gets further."""
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

# Track LED changes
prev_led = 0
led_changes = []

# Track SVCAs (A-line traps)
svca_log = []
orig_step = cpu.step

# Boot
cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)

count = 0
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc
    cpu.step()
    bus.tick(1)
    count += 1

    if led.value != prev_led:
        print(f"[{count:8d}] LED {prev_led:02X} → {led.value:02X}  PC=${pc_before:06X}", file=sys.stderr)
        led_changes.append((count, prev_led, led.value, pc_before))
        prev_led = led.value

    # Log A-line traps (SVCAs)
    try:
        opword = bus.read_word(pc_before)
        if (opword & 0xF000) == 0xA000 and len(svca_log) < 500:
            svca_num = (opword - 0xA000) // 2
            svca_log.append((count, pc_before, svca_num, opword))
    except:
        pass

print(f"\n[DONE] {count} instructions, PC=${cpu.pc:06X}, LED={led.value:02X}", file=sys.stderr)

# Final state
print(f"\n=== Final CPU State ===")
print(f"  PC=${cpu.pc:06X}  SR=${cpu.sr:04X}")
print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

# Show ACIA output
print(f"\n=== ACIA output ({len(output_bytes)} bytes) ===")
if output_bytes:
    # Show hex
    print(f"  Hex: {output_bytes.hex()}")
    # Show printable
    text = output_bytes.decode('ascii', errors='replace')
    print(f"  Text: {repr(text)}")
    # Show as lines
    lines = text.split('\n')
    for i, line in enumerate(lines[:30]):
        print(f"  Line {i}: {repr(line)}")

# Show SVCA summary
print(f"\n=== SVCAs ({len(svca_log)} logged) ===")
# Count by svca number
from collections import Counter
svca_counts = Counter(s[2] for s in svca_log)
# Map known SVCAs (octal numbers from SYS.M68)
svca_names = {
    0o32: 'QGET', 0o33: 'QRET', 0o34: 'QADD', 0o35: 'QINS',
    0o36: 'JRUN', 0o37: 'JWAIT', 0o40: 'JWAITC', 0o41: 'TBUF',
    0o42: 'TIMER', 0o43: 'SLEEP', 0o44: 'TCRT', 0o46: 'JLOCK',
    0o47: 'JUNLOK', 0o50: 'SUPVR', 0o51: 'USRBAS', 0o52: 'USREND',
    0o53: 'JOBIDX', 0o60: 'GETMEM', 0o66: 'SRCH', 0o67: 'RQST',
    0o70: 'RLSE', 0o75: 'GDATES', 0o76: 'SDATES', 0o120: 'FSPEC',
    0o166: 'COMINT',
}
for svca_num, cnt in sorted(svca_counts.items()):
    name = svca_names.get(svca_num, f'?{oct(svca_num)}')
    print(f"  SVCA #{svca_num} ({name}): {cnt} calls")

# Show unique PCs at end (where it's looping)
if count >= config.max_instructions:
    print(f"\n=== Last 20 unique PCs ===")
    recent_pcs = []
    # Check last few SVCAs
    for s in svca_log[-20:]:
        cnt, pc, num, op = s
        name = svca_names.get(num, f'?{oct(num)}')
        print(f"  [{cnt:8d}] PC=${pc:06X} SVCA #{num} ({name})")

# Key memory locations
print(f"\n=== Key Memory ===")
sys_val = bus.read_long(0x0400)
print(f"  SYSTEM ($0400): ${sys_val:08X}")
print(f"  SYSTEM+3 byte: ${bus.read_byte(0x0403):02X}")
jobcur = bus.read_long(0x041C)
print(f"  JOBCUR ($041C): ${jobcur:08X}")
devtbl = bus.read_long(0x0404)
print(f"  DEVTBL ($0404): ${devtbl:08X}")
membas = bus.read_long(0x040C)
print(f"  MEMBAS ($040C): ${membas:08X}")
memend = bus.read_long(0x0410)
print(f"  MEMEND ($0410): ${memend:08X}")
jobtbl = bus.read_long(0x0418)
print(f"  JOBTBL ($0418): ${jobtbl:08X}")
wereup = bus.read_word(0x042E)
print(f"  WEREUP ($042E): ${wereup:04X}")
