#!/usr/bin/env python3
"""Test: use 68000-style 6-byte exception frames instead of 68010 8-byte.

Hypothesis: The AM-1200 uses a 68000 (or the OS expects 68000 frames).
With 68000 frames, the corrupt JOBCUR pointer = $00FFFE11 (I/O space)
instead of $FE112100 (RAM). I/O reads may return 0, fixing the hang.
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

# KEY CHANGE: Use 68000-style 6-byte exception frames
cpu.use_68000_frames = True

# Track ACIA output
output_bytes = bytearray()
def tx_callback(port, byte_val):
    if port == 0:
        output_bytes.append(byte_val)
acia.tx_callback = tx_callback

# Track LED changes
prev_led = 0
led_changes = []

# Track SVCAs
svca_log = []
svca_names = {
    0o32: 'QGET', 0o33: 'QRET', 0o34: 'QADD', 0o35: 'QINS',
    0o36: 'JRUN', 0o37: 'JWAIT', 0o42: 'TIMER', 0o43: 'SLEEP',
    0o46: 'JLOCK', 0o47: 'JUNLOK', 0o50: 'SUPVR', 0o51: 'USRBAS',
    0o52: 'USREND', 0o53: 'JOBIDX', 0o60: 'GETMEM', 0o66: 'SRCH',
    0o67: 'RQST', 0o70: 'RLSE', 0o75: 'GDATES', 0o76: 'SDATES',
    0o120: 'FSPEC', 0o166: 'COMINT',
}

cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)
print(f"[MODE] 68000-style 6-byte exception frames", file=sys.stderr)

count = 0
while not cpu.halted and count < config.max_instructions:
    pc_before = cpu.pc

    try:
        opword = bus.read_word(pc_before)
        if (opword & 0xF000) == 0xA000 and len(svca_log) < 1000:
            svca_num = (opword - 0xA000) // 2
            svca_log.append((count, pc_before, svca_num, opword))
    except:
        pass

    cpu.step()
    bus.tick(1)
    count += 1

    if led.value != prev_led:
        print(f"[{count:8d}] LED {prev_led:02X} → {led.value:02X}  PC=${pc_before:06X}", file=sys.stderr)
        led_changes.append((count, prev_led, led.value))
        prev_led = led.value

    # Periodic progress
    if count % 5_000_000 == 0:
        print(f"[{count:8d}] PC=${cpu.pc:06X} LED={led.value:02X} ACIA={len(output_bytes)} bytes", file=sys.stderr)

print(f"\n[DONE] {count} instructions, PC=${cpu.pc:06X}, LED={led.value:02X}", file=sys.stderr)

# Final state
print(f"\n=== Final CPU State ===")
print(f"  PC=${cpu.pc:06X}  SR=${cpu.sr:04X}")
print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")

# LED changes
print(f"\n=== LED changes ({len(led_changes)}) ===")
for cnt, old, new in led_changes:
    print(f"  [{cnt:8d}] {old:02X} → {new:02X}")

# ACIA output
print(f"\n=== ACIA output ({len(output_bytes)} bytes) ===")
if output_bytes:
    text = output_bytes.decode('ascii', errors='replace')
    print(f"  Text: {repr(text[:500])}")
    lines = text.split('\n')
    for i, line in enumerate(lines[:30]):
        print(f"  Line {i}: {repr(line)}")
else:
    print("  (no output)")

# SVCAs
from collections import Counter
svca_counts = Counter(s[2] for s in svca_log)
print(f"\n=== SVCAs ({len(svca_log)} logged) ===")
for svca_num, cnt in sorted(svca_counts.items()):
    name = svca_names.get(svca_num, f'?{oct(svca_num)}')
    print(f"  SVCA #{svca_num} ({name}): {cnt} calls")

# Key memory
print(f"\n=== Key Memory ===")
for name, addr in [
    ("SYSTEM", 0x0400), ("DEVTBL", 0x0404), ("DDBCHN", 0x0408),
    ("MEMBAS", 0x040C), ("MEMEND", 0x0410), ("SYSBAS", 0x0414),
    ("JOBTBL", 0x0418), ("JOBCUR", 0x041C), ("WEREUP", 0x042E),
]:
    if name == "WEREUP":
        val = bus.read_word(addr)
        print(f"  {name} (${addr:04X}): ${val:04X}")
    else:
        val = bus.read_long(addr)
        print(f"  {name} (${addr:04X}): ${val:08X}")

# Show last 20 SVCAs
print(f"\n=== Last 20 SVCAs ===")
for cnt, pc, num, op in svca_log[-20:]:
    name = svca_names.get(num, f'?{oct(num)}')
    print(f"  [{cnt:8d}] PC=${pc:06X} SVCA #{num} ({name})")
