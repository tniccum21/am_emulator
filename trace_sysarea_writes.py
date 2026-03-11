#!/usr/bin/env python3
"""Trace writes to system communication area to determine word order."""
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
    max_instructions=12_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None

# Hook byte writes to catch ALL writes to system communication area
writes = []
orig_write = bus._write_byte_physical
count = [0]

def hooked_write(address, value):
    phys = address & 0xFFFFFF
    if 0x0400 <= phys <= 0x0440:
        writes.append((count[0], cpu.pc, phys, value & 0xFF))
    orig_write(address, value)

bus._write_byte_physical = hooked_write

cpu.reset()

while not cpu.halted and count[0] < config.max_instructions:
    cpu.step()
    bus.tick(1)
    count[0] += 1

print(f"=== Writes to system area $0400-$0440 ({len(writes)} total) ===")
# Group by target address
from collections import defaultdict
by_addr = defaultdict(list)
for inst, pc, addr, val in writes:
    by_addr[addr].append((inst, pc, val))

# Show DDBCHN ($0408-$040B) writes in detail
for target_name, start, end in [
    ("SYSTEM", 0x0400, 0x0403),
    ("DEVTBL", 0x0404, 0x0407),
    ("DDBCHN", 0x0408, 0x040B),
    ("MEMBAS", 0x040C, 0x040F),
    ("MEMEND", 0x0410, 0x0413),
    ("SYSBAS", 0x0414, 0x0417),
    ("JOBTBL", 0x0418, 0x041B),
    ("JOBCUR", 0x041C, 0x041F),
]:
    print(f"\n  --- {target_name} (${start:04X}-${end:04X}) ---")
    combined = []
    for addr in range(start, end + 1):
        for inst, pc, val in by_addr.get(addr, []):
            combined.append((inst, pc, addr, val))
    combined.sort()

    for inst, pc, addr, val in combined:
        # Detect if this is part of a word or long write by checking adjacent writes
        print(f"    [{inst:8d}] PC=${pc:06X} → phys[${addr:04X}] = ${val:02X}")

    if not combined:
        print(f"    (no writes)")

# Also check: are writes to adjacent bytes happening at the same PC?
print(f"\n=== Write pairs (same PC, adjacent addresses) ===")
# Group writes by PC
from itertools import groupby
sorted_writes = sorted(writes, key=lambda w: (w[0], w[2]))  # sort by instruction, address

prev = None
pairs = []
for inst, pc, addr, val in sorted_writes:
    if prev and prev[1] == pc and abs(prev[2] - addr) == 1:
        pairs.append((prev, (inst, pc, addr, val)))
    prev = (inst, pc, addr, val)

print(f"Found {len(pairs)} adjacent-byte write pairs at same PC")
for p1, p2 in pairs[:30]:
    _, pc, a1, v1 = p1
    _, _, a2, v2 = p2
    word_val = (v2 << 8) | v1 if a2 == a1 + 1 else (v1 << 8) | v2
    print(f"  PC=${pc:06X}: phys[${a1:04X}]=${v1:02X}, phys[${a2:04X}]=${v2:02X} → word ${word_val:04X}")

# Check the opcode at each write PC to identify instruction type
print(f"\n=== Instruction at each write PC ===")
seen_pcs = set()
for inst, pc, addr, val in sorted(writes, key=lambda w: w[0]):
    if pc not in seen_pcs and 0x0404 <= addr <= 0x041F:
        seen_pcs.add(pc)
        # Read the opcode
        try:
            opword = bus.read_word(pc)
            opword2 = bus.read_word(pc + 2)
        except:
            opword = opword2 = 0
        # Find all writes from this PC
        pc_writes = [(a, v) for _, p, a, v in writes if p == pc and 0x0404 <= a <= 0x041F]
        addrs_written = sorted(set(a for a, _ in pc_writes))
        print(f"  PC=${pc:06X} opcode=${opword:04X} ${opword2:04X} → writes to {','.join(f'${a:04X}' for a in addrs_written)}")
