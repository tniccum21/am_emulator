#!/usr/bin/env python3
"""Trace all accesses to the RTC at $FFFE04-$FFFE05 during boot."""
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

# Hook byte reads/writes to see RTC access pattern
rtc_accesses = []
orig_read = bus._read_byte_physical
orig_write = bus._write_byte_physical

def hooked_read(address):
    phys = address & 0xFFFFFF
    val = orig_read(address)
    if 0xFFFE04 <= phys <= 0xFFFE06:
        rtc_accesses.append(('R', phys, val, cpu.pc, count[0]))
    return val

def hooked_write(address, value):
    phys = address & 0xFFFFFF
    if 0xFFFE04 <= phys <= 0xFFFE06:
        rtc_accesses.append(('W', phys, value & 0xFF, cpu.pc, count[0]))
    orig_write(address, value)

bus._read_byte_physical = hooked_read
bus._write_byte_physical = hooked_write

cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)

count = [0]
while not cpu.halted and count[0] < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count[0] += 1

print(f"\n[DONE] {count[0]} instructions, PC=${cpu.pc:06X}, LED={led.value:02X}", file=sys.stderr)

# Show accesses
print(f"\n=== RTC accesses ({len(rtc_accesses)} total) ===")
# Show first 100 and last 20
show = rtc_accesses[:100]
if len(rtc_accesses) > 120:
    show += [None]  # separator
    show += rtc_accesses[-20:]

for entry in show:
    if entry is None:
        print(f"  ... ({len(rtc_accesses) - 120} more) ...")
        continue
    rw, addr, val, pc, inst = entry
    print(f"  [{inst:8d}] {rw} ${addr:06X} = ${val:02X}  PC=${pc:06X}")

# Analyze pattern: group write-then-read pairs
print(f"\n=== Write→Read pairs (register accesses) ===")
pairs = []
i = 0
while i < len(rtc_accesses):
    if rtc_accesses[i][0] == 'W' and rtc_accesses[i][1] == 0xFFFE04:
        cmd = rtc_accesses[i][2]
        # Look for next read from $FFFE05
        for j in range(i+1, min(i+10, len(rtc_accesses))):
            if rtc_accesses[j][0] == 'R' and rtc_accesses[j][1] == 0xFFFE05:
                data = rtc_accesses[j][2]
                pairs.append((cmd, data, rtc_accesses[i][3], rtc_accesses[i][4]))
                break
    i += 1

print(f"Total pairs: {len(pairs)}")
for i, (cmd, data, pc, inst) in enumerate(pairs[:50]):
    reg = cmd & 0x0F
    print(f"  [{i:3d}] cmd=${cmd:02X} (reg={reg:X}) → data=${data:02X}  PC=${pc:06X} @{inst}")
