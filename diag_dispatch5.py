#!/usr/bin/env python3
"""Deep analysis of I/O processing flow and DDB format.

Now that we know:
1. DD.HWA at +$18 = $FFFFFFC8 (ACIA address, WRONG for SASI)
2. XFER at $7B1E uses MOVEA.L $0018(A0),A5 to get HWA
3. DDT at $7038 is used as pseudo-JCB, scheduler dispatches it
4. I/O processing code is at $1380+

This script:
1. Disassemble XFER more fully to understand what it does
2. Disassemble the I/O dispatch code at $1380-$1470 
3. Check DDB format to understand I/O request structure
4. Determine what we need to set up for I/O to work
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

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

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

print(f"Boot: PC=${cpu.pc:06X}")

# ─── 1. Full XFER disassembly ───
print(f"\n{'='*60}")
print("1. FULL XFER CODE ($7B1E - $7BA0)")
print(f"{'='*60}")
for addr in range(0x7B1E, 0x7BA0, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── 2. DD.MNT (INIT) disassembly ───
print(f"\n{'='*60}")
print("2. DD.MNT (INIT) CODE")
print(f"{'='*60}")
mnt_entry = DRIVER_RAM + 0x28  # $7AEA
mnt_disp = bus.read_word(mnt_entry + 2)
# JMP (d16,PC) target
if bus.read_word(mnt_entry) == 0x4EFA:
    mnt_target = (mnt_entry + 2) + (mnt_disp if mnt_disp < 0x8000 else mnt_disp - 0x10000)
    print(f"  DD.MNT JMP target: ${mnt_target:06X}")
    for addr in range(mnt_target, mnt_target + 80, 2):
        w = bus.read_word(addr)
        print(f"  ${addr:06X}: ${w:04X}")

# ─── 3. I/O processing disassembly at $1380-$1470 ───
# Let me hand-decode key parts of the I/O processing loop
print(f"\n{'='*60}")
print("3. I/O PROCESSING CODE — HAND DECODE")
print(f"{'='*60}")

# $1380: CMPA.L ($0434).W,A7  -- compare SP with supervisor stack base
# $1384: BLS.W $146A           -- if below or same, branch (stack overflow check)
# $1388: TST.B ($04C0).W       -- test I/O in-progress flag
# $138C: BNE.S $1388           -- spin if still in progress
# $138E: MOVEA.L ($041C).W,A0  -- load JOBCUR into A0
# $1392: ANDI.W #$EFFF,$0002(A0)  -- clear bit 12 in DDT+$02 status
# $1398: MOVEA.L ($0514).W,A6  -- load function table
# $139C: JSR (A6)              -- call +$00 (timer init?)
# $139E: CLR.L D7              -- clear D7
# $13A0: MOVE.W $0092(A0),D7   -- load DDT+$92 (timer/tick counter?)
# $13A4: ADDI.W #$0080,D7      -- add $80 to it
# $13A8: LSR.W #8,D7           -- shift right 8 = divide by 256
# $13AA: ADD.L $0094(A0),D7    -- add DDT+$94 (time accumulator?)
# $13AE: MOVE.W (A0),D7        -- load DDT+$00 (status word)
# $13B0: ANDI.W #$2000,D7      -- check bit 13 (I/O pending?)
# $13B4: BNE.W $1436           -- branch if set → skip I/O dispatch
# $13B8: LEA $0078(A0),A3      -- A3 = DDT+$78 (I/O queue link)
# $13BC: MOVE.L (A3),D7        -- load queue link
# $13BE: BEQ.S $1436           -- if null, no I/O queued → skip
# $13C0: MOVE.W $003C(A7),D6   -- load from stack (SR from exception frame?)
# $13C4: ANDI.W #$0700,D6      -- mask interrupt priority
# $13C8: BNE.W $1470           -- if interrupts masked, skip I/O dispatch
# $13CC: MOVE.L D7,($041C).W   -- save D7 to JOBCUR
# $13D0: CLR.L (A3)+           -- clear the queue link
# $13D2: MOVE USP,A3           -- get USP
# $13D4: MOVE.L (A6)+,A3       -- A3 = next function ptr
# $13D6: MOVE.L A7,(A3)        -- save SSP

# Wait, let me re-decode more carefully
# The above is getting complex. Let me trace what happens when I/O IS queued.
# Key question: how does the scheduler know to call the driver?
# The answer is in $13B8-$13D8:
# $13B8: LEA $0078(A0),A3     -- A3 = &DDT.queue
# $13BC: MOVE.L (A3),D7       -- D7 = DDT.queue link (DDB pointer)
# $13BE: BEQ.S $1436          -- no DDB queued → skip
# If a DDB IS queued (D7 ≠ 0):
# $13CC: MOVE.L D7,($041C).W  -- set JOBCUR = DDB (the queued DDB)
# $13D0: CLR.L (A3)+          -- clear queue link (dequeue)
# ...
# Then eventually calls the driver

print("\nKey I/O dispatch flow:")
print("  $1380: CMPA.L ($0434).W,A7  — stack check")
print("  $138E: MOVEA.L ($041C).W,A0 — A0 = JOBCUR (DDT)")
print("  $13B8: LEA $0078(A0),A3     — A3 = &DDT+$78 (I/O queue)")
print("  $13BC: MOVE.L (A3),D7       — D7 = queued DDB") 
print("  $13BE: BEQ.S $1436          — no DDB? skip")
print("  $13CC: MOVE.L D7,($041C).W  — JOBCUR = DDB (switch to I/O)")

# Now check DDT+$78 (I/O queue) for the disk DDT
ddt = 0x7038
print(f"\n  DDT $7038 I/O queue:")
print(f"    DDT+$78 = ${read_long(ddt + 0x78):08X}")
print(f"    DDT+$7C = ${read_long(ddt + 0x7C):08X}  (saved A6)")
print(f"    DDT+$80 = ${read_long(ddt + 0x80):08X}  (saved SP)")
print(f"    DDT+$84 = ${read_long(ddt + 0x84):08X}  (I/O status)")

# ─── 4. Examine $A03C handler in detail ───
print(f"\n{'='*60}")
print("4. $A03C HANDLER ($12F4) — QUEUEIO DETAIL")
print(f"{'='*60}")
for addr in range(0x12F4, 0x1380, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── 5. What does the $A03C handler do with DDT and DDB? ───
# A03C is called from $4E46 with A0=DDT
# Let's decode the handler
print(f"\nDecoding $A03C handler (QUEUEIO):")
print("  It takes: A0 = DDT pointer")
print("  It queues a DDB into DDT+$78")
print("  Then scheduler processes DDT+$78 queue at $13BC")

# ─── 6. Check what code is at $6320-$6340 ─── 
# This is where MOVEA.L $0006(A2),A6 was found (DDT+$06 = driver ptr)
print(f"\n{'='*60}")
print("6. CODE AT $6320-$6380 (reads DDT+$06)")
print(f"{'='*60}")
for addr in range(0x6300, 0x6380, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── 7. Key DDT+$06 (driver base pointer) check ───
print(f"\n{'='*60}")
print("7. DDT+$06 (DRIVER BASE POINTER)")
print(f"{'='*60}")
drv_base = read_long(ddt + 0x06)
print(f"  DDT ($7038) + $06 = ${drv_base:08X}")
if drv_base != 0 and drv_base < 0x400000:
    print(f"  This points to the driver START (DD.BSZ etc.)")
    print(f"  DD.XFR would be at driver+$08 = ${drv_base + 0x08:06X}")
    xfr_w1 = bus.read_word(drv_base + 0x08)
    print(f"    Content: ${xfr_w1:04X}")
else:
    print(f"  INVALID or ZERO — no driver loaded!")

# ─── 8. Check the caller at $4E46 more carefully ───
print(f"\n{'='*60}")
print("8. MOUNT CODE AT $4E00-$4E80")
print(f"{'='*60}")
# Specifically look for how DDB is set up before $A03C call
for addr in range(0x4E00, 0x4E80, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── 9. Full DDT dump ───
print(f"\n{'='*60}")
print("9. FULL DDT AT $7038 (first $A0 bytes)")
print(f"{'='*60}")
for off in range(0, 0xA0, 2):
    addr = ddt + off
    w = bus.read_word(addr)
    l = read_long(addr) if off + 3 < 0xA0 else 0
    if w != 0 or off in (0x00, 0x02, 0x04, 0x06, 0x08, 0x78, 0x7C, 0x80, 0x84, 0x8E, 0x92, 0x94):
        name = ""
        if off == 0x00: name = "status"
        elif off == 0x02: name = "flags"  
        elif off == 0x04: name = "link"
        elif off == 0x06: name = "driver base (DD ptr)"
        elif off == 0x08: name = "DD.XFR (JMP)"
        elif off == 0x18: name = "DD.HWA"
        elif off == 0x28: name = "DD.MNT (JMP)"
        elif off == 0x78: name = "I/O queue link"
        elif off == 0x7C: name = "saved A6"
        elif off == 0x80: name = "saved SP" 
        elif off == 0x84: name = "I/O status"
        elif off == 0x8E: name = "time counter 1"
        elif off == 0x92: name = "time counter 2"
        elif off == 0x94: name = "time accumulator"
        print(f"  +${off:02X} (${addr:06X}): W=${w:04X} L=${l:08X}  {name}")

# ─── 10. Compare with terminal DDT ───
print(f"\n{'='*60}")
print("10. TERMINAL DDT AT $69D0 (key fields)")
print(f"{'='*60}")
term = 0x69D0
for off in [0x00, 0x02, 0x04, 0x06, 0x08, 0x18, 0x28, 0x78, 0x7C, 0x80, 0x84]:
    addr = term + off
    l = read_long(addr)
    name = ""
    if off == 0x00: name = "status"
    elif off == 0x02: name = "flags"
    elif off == 0x04: name = "link"
    elif off == 0x06: name = "driver base (DD ptr)"
    elif off == 0x08: name = "DD.XFR (JMP)"
    elif off == 0x18: name = "DD.HWA"
    elif off == 0x28: name = "DD.MNT (JMP)"
    elif off == 0x78: name = "I/O queue link"
    elif off == 0x7C: name = "saved A6"
    elif off == 0x80: name = "saved SP"
    elif off == 0x84: name = "I/O status"
    print(f"  +${off:02X} (${addr:06X}): L=${l:08X}  {name}")

print("\nDone!")
