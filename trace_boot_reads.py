#!/usr/bin/env python3
"""Trace all SASI disk reads during ROM boot to see if AMOSL.MON is found."""
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

# Monkey-patch SASI to log all reads
sasi = None
for start, end, dev in bus._devices:
    if hasattr(dev, '_do_read_sector'):
        sasi = dev
        break

reads = []
orig_read = sasi._do_read_sector

def traced_read():
    orig_read()
    track = sasi._sno
    sector = sasi._sct
    head = (sasi._sdh >> 4) & 1
    spt = 10
    physical = track * spt + (max(sector, 1) - 1)
    lba = physical * 2 + head + 1
    block = lba - 1  # AMOS block = LBA - 1
    first16 = sasi._data_buffer[:16].hex()
    reads.append((lba, block, track, sector, head, first16))

sasi._do_read_sector = traced_read

# Boot
cpu.reset()
print(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}", file=sys.stderr)

count = 0
while not cpu.halted and count < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count += 1

print(f"\n[DONE] {count} instructions, PC=${cpu.pc:06X}, LED={led.value:02X}", file=sys.stderr)
print(f"Total SASI reads: {len(reads)}", file=sys.stderr)

# Print all reads
print(f"\nAll SASI reads ({len(reads)} total):")
for i, (lba, block, track, sec, head, first16) in enumerate(reads):
    print(f"  [{i:4d}] LBA={lba:5d} (block={block:5d})  CHS=t{track}/s{sec}/h{head}  data={first16}")

# Check if block 3257 (AMOSL.MON) was read
amosl_reads = [r for r in reads if r[1] == 3257]
if amosl_reads:
    print(f"\n*** Block 3257 (AMOSL.MON first block) WAS read ***")
else:
    print(f"\n*** Block 3257 (AMOSL.MON first block) was NOT read ***")
    # Show the highest block read
    if reads:
        max_block = max(r[1] for r in reads)
        print(f"    Highest block read: {max_block}")
