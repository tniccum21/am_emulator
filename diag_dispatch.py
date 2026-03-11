#!/usr/bin/env python3
"""Diagnose I/O dispatch mechanism — trace what $A03E does and why driver is never called.

Questions to answer:
1. Where does the LINE-A dispatch table send $A03E?
2. Does that handler ever reference DDT+$08?
3. What does the scheduler dispatch at $129C do?
4. Does anything in the DDT status word prevent dispatch?
"""
import sys
sys.path.insert(0, ".")
from pathlib import Path
import shutil

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

DRIVER_RAM = 0x7AC2
MAX_COPY = 1432
AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510

CANTMR_DST = 0x9000
CLRCDB_DST = 0x9050

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# ─── Step 1: Disk patch ───
print("Step 1: Disk patch...")
with open(src_path, "rb") as f:
    img = bytearray(f.read())

scz_data = bytearray()
block = 1402
while block != 0:
    lba = block + 1
    offset = lba * 512
    link = read_word_le(img, offset)
    scz_data.extend(img[offset+2:offset+512])
    if link == 0:
        break
    block = link

driver = scz_data[:MAX_COPY]
shutil.copy2(src_path, dst_path)
with open(dst_path, "r+b") as f:
    patched = bytearray(f.read())
    file_off = DRIVER_RAM
    blk_idx = file_off // BYTES_PER_BLOCK
    data_off = file_off % BYTES_PER_BLOCK
    drv_pos = 0
    remaining = len(driver)
    while remaining > 0:
        lba = AMOSL_START_BLOCK + blk_idx + 1
        disk_offset = lba * 512
        space = BYTES_PER_BLOCK - data_off
        count = min(remaining, space)
        disk_pos = disk_offset + 2 + data_off
        patched[disk_pos:disk_pos + count] = driver[drv_pos:drv_pos + count]
        drv_pos += count
        remaining -= count
        blk_idx += 1
        data_off = 0
    f.seek(0)
    f.write(patched)
print("  Done")

# ─── Step 2: Boot ───
print("\nStep 2: Boot to $006C0E...")

from alphasim.config import SystemConfig
from alphasim.main import build_system

config = SystemConfig(
    rom_even_path=Path("roms/AM-178-01-B05.BIN"),
    rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
    ram_size=0x400000,
    config_dip=0x0A,
    disk_image_path=dst_path,
    trace_enabled=False,
    max_instructions=50_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
acia.tx_callback = lambda port, val: None

def read_long(addr):
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)

orig_write_byte = bus._write_byte_physical
skip_hook = False

def raw_write_word(addr, val):
    addr &= ~1
    orig_write_byte(addr, val & 0xFF)
    orig_write_byte(addr + 1, (val >> 8) & 0xFF)

def raw_write_long(addr, val):
    addr &= ~1
    raw_write_word(addr, (val >> 16) & 0xFFFF)
    raw_write_word(addr + 2, val & 0xFFFF)

# MEMBAS hook
CORRECT_MEMBAS = 0x8800
mem_patch_done = False
write_043B_count = 0

def patching_write(address, value):
    global mem_patch_done, write_043B_count, skip_hook
    addr = address & 0xFFFFFF
    if skip_hook:
        orig_write_byte(address, value)
        return
    orig_write_byte(address, value)
    if not mem_patch_done and addr == 0x043B and count > 10000:
        write_043B_count += 1
        if write_043B_count >= 2:
            mem_patch_done = True
            skip_hook = True
            raw_write_long(0x0430, CORRECT_MEMBAS)
            raw_write_long(0x0438, 0x3F0000)
            raw_write_word(0x0426, 4)
            raw_write_long(CORRECT_MEMBAS, 0)
            raw_write_long(CORRECT_MEMBAS + 4, 0x3F0000 - CORRECT_MEMBAS)
            skip_hook = False

bus._write_byte_physical = patching_write
cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x006C0E and count > 100000:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"  PC=${cpu.pc:06X} at instr {count}")
print(f"  JOBCUR=$041C: ${read_long(0x041C):08X}")

# ─── Step 3: Examine LINE-A dispatch table ───
print(f"\n{'='*60}")
print("Step 3: LINE-A Dispatch Table Analysis")
print(f"{'='*60}")

# The handler at $06F6 uses a table to dispatch. Let's find the table.
# From previous analysis:
#   $070A: MOVE.W (xxxx,PC,D7),-(SP) — push handler offset from table
#   $070E: CLR.W -(SP) — push zero
#   $0710: RTS — jump via push+RTS
#
# The table base is at $070A + the displacement in the MOVE.W instruction.
# Let's read the MOVE.W instruction to find the table.

print("\nLINE-A handler disassembly at $06F6:")
for addr in range(0x06F6, 0x0716, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# The MOVE.W (d8,PC,D7.W),-(SP) at $070A
# Encoding: $3F3B XXYY where XX=displacement, D7 index reg
# Let's decode the displacement
move_instr = bus.read_word(0x070A)
move_ext = bus.read_word(0x070C)
print(f"\n  MOVE.W at $070A: ${move_instr:04X} ${move_ext:04X}")
# Extension word format: D7 is index reg, displacement is low byte
disp_byte = move_ext & 0xFF
table_base = 0x070C + disp_byte  # PC at $070C + disp
print(f"  Displacement byte: ${disp_byte:02X}")
print(f"  Table base: ${table_base:06X}")

# Now read the table entries
# $A03C = trap number $03C, table offset = $03C (each entry is 1 word, no *2 needed
# because the opcode masking already accounts for it)
# Actually: ANDI.L #$0FFE,D7 — so the trap number * 2 is already in D7
# For $A03C: ($A03C & $0FFE) = $003C, so table[offset $3C] = table_base + $3C

print(f"\nLINE-A dispatch table entries:")
# Show entries around $A03C and $A03E
for trap_num in [0x03A, 0x03C, 0x03E, 0x040, 0x042, 0x044]:
    # The trap *2 is already done by ANDI #$0FFE
    table_off = trap_num  # This IS the offset (already * 2 because low bit is masked)
    table_addr = table_base + table_off
    if table_addr < 0x8000:  # Sanity check
        entry = bus.read_word(table_addr)
        # The entry is a signed offset from... where?
        # The handler pushes this word on the stack then does RTS
        # The CLR.W -(SP) at $070E pushes a zero byte high word
        # So the RTS target = (0 << 16) | entry = entry as an absolute address
        # Wait, CLR.W pushes 0 on the stack BEFORE the entry,
        # so the stack has: [entry_word][0x0000]
        # RTS pops a longword: (entry << 16) | 0 ... no that's wrong
        # Actually MOVE.W pushes entry at (SP), then CLR.W pushes 0 at (SP-2)
        # Stack: SP -> [0x0000][entry]
        # RTS pops longword: high word = 0x0000, low word = entry
        # So target = 0x0000:entry = entry as a 16-bit absolute address
        print(f"  $A{trap_num:03X}: table[${table_off:03X}] at ${table_addr:06X} = ${entry:04X} -> handler at ${entry:06X}")

# Now let's look at what $A03E handler does
trap_3e_off = 0x03E
trap_3e_addr = table_base + trap_3e_off
handler_3e = bus.read_word(trap_3e_addr)
print(f"\n$A03E handler at ${handler_3e:06X} — disassembly:")
for addr in range(handler_3e, handler_3e + 80, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# Also check $A03C handler
trap_3c_off = 0x03C
trap_3c_addr = table_base + trap_3c_off
handler_3c = bus.read_word(trap_3c_addr)
print(f"\n$A03C handler at ${handler_3c:06X} — disassembly:")
for addr in range(handler_3c, handler_3c + 120, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── Step 4: Check DDT state ───
print(f"\n{'='*60}")
print("Step 4: DDT State at $7038")
print(f"{'='*60}")

ddt_base = 0x7038
for off in range(0, 0x40, 2):
    addr = ddt_base + off
    w = bus.read_word(addr)
    if w != 0:
        l = read_long(addr)
        print(f"  +${off:02X} (${addr:06X}): W=${w:04X} L=${l:08X}")

# ─── Step 5: Check driver entry points in RAM ───
print(f"\n{'='*60}")
print("Step 5: Driver Entry Points")
print(f"{'='*60}")

# DD.XFR at $7AC2 + $08 = $7ACA
for off, name in [(0x08, "DD.XFR"), (0x28, "DD.MNT")]:
    addr = DRIVER_RAM + off
    w1 = bus.read_word(addr)
    w2 = bus.read_word(addr + 2)
    w3 = bus.read_word(addr + 4)
    target = addr + 2 + (w2 if w2 < 0x8000 else w2 - 0x10000)
    print(f"  {name} at ${addr:06X}: ${w1:04X} ${w2:04X} ${w3:04X}")
    if w1 == 0x4EFA:  # JMP (d16,PC)
        print(f"    -> JMP to ${target:06X}")

# ─── Step 6: Scheduler dispatch analysis ───
print(f"\n{'='*60}")
print("Step 6: Scheduler Dispatch at $129C")
print(f"{'='*60}")

for addr in range(0x129C, 0x1340, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── Step 7: Search for JMP/JSR to DDT or driver area ───
print(f"\n{'='*60}")
print("Step 7: Search for references to DDT+$08 / driver dispatch")
print(f"{'='*60}")

# Search for any code that reads DDT+$08 or DDT+$06 (the driver pointer)
# DDT is at $7038, DDT+$06 = $703E, DDT+$08 = $7040
# But the kernel accesses DDT via A-register relative addressing
# Look for patterns like:
#   MOVEA.L (A0)+,A1 or MOVEA.L $0006(A0),A1 or JSR $0008(A0)

# Also search for the word $7ACA (DD.XFR entry) or $7AC2 (driver base)
print("\nSearching OS code for $7ACA (DD.XFR entry):")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    if w == 0x7ACA:
        context = [f"${bus.read_word(addr+i):04X}" for i in range(-4, 8, 2)]
        print(f"  ${addr:06X}: {' '.join(context)}")

# Search for JSR $0008(A0) — common pattern to call DDT+$08
# Encoding: JSR = $4EA8 $0008
print("\nSearching for JSR $0008(An) patterns:")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    if w in (0x4EA8, 0x4EA9, 0x4EAA, 0x4EAB, 0x4EAC, 0x4EAD, 0x4EAE, 0x4EAF):
        w2 = bus.read_word(addr + 2)
        if w2 == 0x0008:
            reg = w & 0x07
            print(f"  ${addr:06X}: JSR $0008(A{reg})")
            # Show more context
            for a in range(addr - 8, addr + 12, 2):
                ww = bus.read_word(a)
                m = " <--" if a == addr else ""
                print(f"    ${a:06X}: ${ww:04X}{m}")

# Also search for JMP $0008(An)
# Encoding: JMP = $4EE8 $0008
print("\nSearching for JMP $0008(An) patterns:")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    if w in (0x4EE8, 0x4EE9, 0x4EEA, 0x4EEB, 0x4EEC, 0x4EED, 0x4EEE, 0x4EEF):
        w2 = bus.read_word(addr + 2)
        if w2 == 0x0008:
            reg = w & 0x07
            print(f"  ${addr:06X}: JMP $0008(A{reg})")
            for a in range(addr - 8, addr + 12, 2):
                ww = bus.read_word(a)
                m = " <--" if a == addr else ""
                print(f"    ${a:06X}: ${ww:04X}{m}")

# Also search for JSR/JMP $0028(An) — DD.MNT entry
print("\nSearching for JSR $0028(An) patterns:")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    if w in (0x4EA8, 0x4EA9, 0x4EAA, 0x4EAB, 0x4EAC, 0x4EAD, 0x4EAE, 0x4EAF):
        w2 = bus.read_word(addr + 2)
        if w2 == 0x0028:
            reg = w & 0x07
            print(f"  ${addr:06X}: JSR $0028(A{reg})")
            for a in range(addr - 8, addr + 12, 2):
                ww = bus.read_word(a)
                m = " <--" if a == addr else ""
                print(f"    ${a:06X}: ${ww:04X}{m}")

print("\nSearching for JMP $0028(An) patterns:")
for addr in range(0, 0x7000, 2):
    w = bus.read_word(addr)
    if w in (0x4EE8, 0x4EE9, 0x4EEA, 0x4EEB, 0x4EEC, 0x4EED, 0x4EEE, 0x4EEF):
        w2 = bus.read_word(addr + 2)
        if w2 == 0x0028:
            reg = w & 0x07
            print(f"  ${addr:06X}: JMP $0028(A{reg})")
            for a in range(addr - 8, addr + 12, 2):
                ww = bus.read_word(a)
                m = " <--" if a == addr else ""
                print(f"    ${a:06X}: ${ww:04X}{m}")

print("\nDone!")
