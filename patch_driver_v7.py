#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch SCZ.DVR into AMOSL.MON v7 -- with disk I/O bypass.

Changes from v6:
  - Bypasses disk I/O at the OS level
  - Monitors DDT+$84 (I/O status) via write hook
  - When DDT+$84 becomes $FFFFFFFF (I/O pending):
    a) Mount (no DDB): set success immediately
    b) Read/Write (DDB present): handle from disk image directly
  - Removes DDT from JOBCUR chain after handling
  - Logs all disk I/O events for debugging
"""
import sys
import os
import shutil
import select
import tty
import termios
sys.path.insert(0, ".")

INTERACTIVE = '--interactive' in sys.argv or '-i' in sys.argv
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

DRIVER_RAM = 0x7AC2
MAX_COPY = 1432
AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510

CANTMR_DST = 0x9000
CLRCDB_DST = 0x9050

CORRUPT_START = 0x058E
CORRUPT_END = 0x0596

# DDT addresses
DDT_ADDR = 0x7038
DDT_QUEUE = DDT_ADDR + 0x78   # $70B0 — I/O DDB queue link
DDT_SAVED_A6 = DDT_ADDR + 0x7C  # $70B4
DDT_SAVED_SP = DDT_ADDR + 0x80  # $70B8
DDT_STATUS = DDT_ADDR + 0x84  # $70BC — I/O status (0=done, $FFFFFFFF=pending)

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# Suppress boot output in interactive mode
if INTERACTIVE:
    _real_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')

# ─── Step 1: Create disk patch ───
print("Step 1: Creating disk patch...")
with open(src_path, "rb") as f:
    img = bytearray(f.read())

# Keep the raw disk image for I/O bypass
disk_image = img  # Reference to full image

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
print(f"  SCZ.DVR: {len(scz_data)} bytes, copying {len(driver)} bytes")

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

print(f"  Disk patch saved: {dst_path}")

# ─── Step 2: Boot to $006C0E ───
print(f"\nStep 2: Booting to port init ($006C0E)...")

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
acia_output = []
banner_chars = bytearray()

def _tx_cb(port, val):
    ch = val & 0x7F
    acia_output.append(ch)
    if ch >= 0x20 or ch in (0x0A, 0x0D):
        sys.stdout.buffer.write(bytes([ch]))
        sys.stdout.buffer.flush()
acia.tx_callback = _tx_cb

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

# ─── Write hook: MEMBAS + JOBCUR protection + DDT+$84 monitoring ───
# AMOSL.MON code+data extends to ~$87DA. $5252 JCB fill starts at ~$87DC.
# Layout:
#   $8800-$91FF: reserved for init job stack (2.5KB, SP starts at $9200, grows DOWN)
#   $9200-$93FF: gap (exception frame is copied to SP, must not overlap MEMBAS)
#   $9400+: free memory pool (MEMBAS)
CORRECT_MEMBAS = 0x9400
INIT_JOB_SP = 0x9200
mem_patch_done = False
write_043B_count = 0
ddt84_dirty = False
disk_io_count = 0
job_port_addr = 0  # Set after boot
disk_io_enabled = False  # Gate: only handle disk I/O after terminal init
ddt00_monitor_count = [0]  # mutable counter for write hook
ddt84_write_log_count = [0]  # mutable counter for DDT+$84 write log

def combined_write_hook(address, value):
    global mem_patch_done, write_043B_count, skip_hook
    global ddt84_dirty, job_port_addr
    addr = address & 0xFFFFFF
    if skip_hook:
        orig_write_byte(address, value)
        return
    orig_write_byte(address, value)

    # MEMBAS hook (during boot)
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
            # Verify the write stuck
            v0 = read_long(CORRECT_MEMBAS)
            v4 = read_long(CORRECT_MEMBAS + 4)
            print(f"    MEMBAS hook fired: set ${CORRECT_MEMBAS:06X}")
            print(f"    Verify: [{CORRECT_MEMBAS:06X}]=${v0:08X} [{CORRECT_MEMBAS+4:06X}]=${v4:08X}")
            print(f"    MEMBAS($0430)=${read_long(0x0430):08X} MEMEND($0438)=${read_long(0x0438):08X}")

    # JOBCUR protection (prevent OS from zeroing $041C or setting it to DDT)
    if addr in (0x041C, 0x041D, 0x041E, 0x041F) and job_port_addr != 0:
        new_jobcur = read_long(0x041C)
        if new_jobcur == 0 or new_jobcur == DDT_ADDR:
            skip_hook = True
            raw_write_long(0x041C, job_port_addr)
            skip_hook = False

    # DDT+$84 write detection ($7038+$84 = $70BC-$70BF) — handle I/O immediately
    if 0x70BC <= addr <= 0x70BF:
        ddt84_dirty = True
        if disk_io_enabled:
            val_after = read_long(0x70BC)
            if val_after == 0xFFFFFFFF:
                # I/O request detected — handle immediately from disk image
                handle_pending_disk_io()

    # DDT+$78 write protection ($7038+$78 = $70B0-$70B3) — prevent scheduler queue
    if 0x70B0 <= addr <= 0x70B3 and disk_io_enabled:
        val_after = read_long(0x70B0)
        if val_after != 0:
            # DDT being linked into scheduler queue — unlink immediately
            skip_hook = True
            raw_write_long(0x70B0, 0)
            skip_hook = False

    # Monitor DDT+$00 writes ($7038-$7039) to catch resets
    if addr in (0x7038, 0x7039) and disk_io_enabled:
        w = (bus._read_byte_physical(0x7039) << 8) | bus._read_byte_physical(0x7038)
        if not INTERACTIVE and ddt00_monitor_count[0] < 20:
            ddt00_monitor_count[0] += 1
            print(f"  [DDT+$00 WRITE] addr=${addr:06X} val=${value:02X} "
                  f"word=${w:04X} PC=${cpu.pc:06X}")

bus._write_byte_physical = combined_write_hook
cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x006C0E and count > 100000:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"  Port init at instr {count}, PC=${cpu.pc:06X}")
print(f"  $041C (job ptr): ${read_long(0x041C):08X}")

w2 = bus.read_word(DRIVER_RAM + 2)
print(f"  Driver at ${DRIVER_RAM:06X}+2: ${w2:04X} ({'present' if w2 != 0 else 'MISSING'})")

# ─── Step 3: Repair corrupted bytes ───
print(f"\nStep 3: Repairing boot-corrupted bytes...")
for off in range(CORRUPT_START, CORRUPT_END + 2, 2):
    ram_addr = DRIVER_RAM + off
    scz_w = read_word_le(scz_data, off)
    ram_w = bus.read_word(ram_addr)
    if scz_w != ram_w:
        bus.write_word(ram_addr, scz_w)
        print(f"  ${ram_addr:06X} (+${off:04X}): ${ram_w:04X} -> ${scz_w:04X}")

# ─── Step 4: Inject CANTMR and CLRCDB ───
print(f"\nStep 4: Injecting CANTMR and CLRCDB...")
CANTMR_SRC = 0x05C8
CANTMR_SIZE = 0x0616 - 0x05C8 + 2
cantmr_data = scz_data[CANTMR_SRC:CANTMR_SRC + CANTMR_SIZE]
for i in range(0, CANTMR_SIZE, 2):
    w = (cantmr_data[i+1] << 8) | cantmr_data[i]
    bus.write_word(CANTMR_DST + i, w)

CLRCDB_SRC = 0x0618
CLRCDB_SIZE = 0x0628 - 0x0618 + 2
clrcdb_data = scz_data[CLRCDB_SRC:CLRCDB_SRC + CLRCDB_SIZE]
for i in range(0, CLRCDB_SIZE, 2):
    w = (clrcdb_data[i+1] << 8) | clrcdb_data[i]
    bus.write_word(CLRCDB_DST + i, w)

print(f"  CANTMR: {CANTMR_SIZE} bytes at ${CANTMR_DST:06X}")
print(f"  CLRCDB: {CLRCDB_SIZE} bytes at ${CLRCDB_DST:06X}")

# ─── Step 5: Patch JSR(PC) displacements ───
print(f"\nStep 5: Patching JSR(PC) displacements...")
jsr_patches = [
    (0x0204, CANTMR_DST, "CANTMR (SELECT)"),
    (0x03B0, CLRCDB_DST, "CLRCDB (ERRHND)"),
    (0x040A, CLRCDB_DST, "CLRCDB (RQSENS)"),
    (0x043C, CLRCDB_DST, "CLRCDB (TSTUNIT)"),
    (0x046C, CLRCDB_DST, "CLRCDB (FORMAT)"),
    (0x0490, CLRCDB_DST, "CLRCDB (FMTTST)"),
    (0x057C, CLRCDB_DST, "CLRCDB (FMTLOP)"),
    (0x0592, CLRCDB_DST, "CLRCDB (FMTLOP2)"),
]
for drv_off, target, desc in jsr_patches:
    ram_addr = DRIVER_RAM + drv_off
    disp_addr = ram_addr + 2
    opcode = bus.read_word(ram_addr)
    if opcode != 0x4EBA:
        print(f"  ERROR: Expected $4EBA at ${ram_addr:06X}, got ${opcode:04X}")
        continue
    old_disp = bus.read_word(disp_addr)
    new_disp = (target - disp_addr) & 0xFFFF
    bus.write_word(disp_addr, new_disp)
    print(f"  ${ram_addr:06X} (+${drv_off:04X}): -> ${target:06X} {desc}")

# ─── Step 5b: Inject trap-door driver at DDT+$08 ───
print(f"\nStep 5b: Setting up trap-door driver...")
TRAPDOOR_ADDR = 0x9100

# Write a small 68000 routine at $9100:
#   $9100: NOP        ($4E71) — marker for Python intercept
#   $9102: RTS        ($4E75) — return to caller
bus.write_word(TRAPDOOR_ADDR, 0x4E71)      # NOP
bus.write_word(TRAPDOOR_ADDR + 2, 0x4E75)  # RTS

# DDT+$08 through DDT+$0D overlap with JCB fields that the OS reads
# (e.g., JCB+$08 is used by memory module validation code at $37xx).
# Previously we injected JMP $9100 here for a trap-door driver, but since
# we handle disk I/O synchronously via LINE-A intercept, the trap-door
# is never called. Zero these fields to prevent JCB field corruption.
DDT_XFR = DDT_ADDR + 0x08  # $7040
bus.write_word(DDT_XFR, 0x0000)
bus.write_word(DDT_XFR + 2, 0x0000)
bus.write_word(DDT_XFR + 4, 0x0000)
print(f"  DDT+$08 at ${DDT_XFR:06X}: ZEROED (no trap-door driver needed)")
# JCB+$0C is the job's memory module pointer, read by $A052.
# Must point to a valid memory block (even addr, within RAM, first long=0
# for empty block). Use $90F0 as a dummy empty module.
DUMMY_MODULE = 0x90F0
raw_write_long(DUMMY_MODULE, 0)       # next = 0 (empty)
raw_write_long(DUMMY_MODULE + 4, 0)   # size = 0
raw_write_long(DDT_ADDR + 0x0C, DUMMY_MODULE)
print(f"  DDT+$0C at ${DDT_ADDR + 0x0C:06X}: -> ${DUMMY_MODULE:06X} (dummy module)")

# ─── Step 6: JOBCUR fix and pending I/O ───
print(f"\nStep 6: JOBCUR fix and pending I/O...")

raw_jobcur = read_long(0x041C)
print(f"  JOBCUR at $041C = ${raw_jobcur:08X}")
ddt84_val = read_long(DDT_STATUS)
print(f"  DDT+$84 = ${ddt84_val:08X}")
print(f"  DDT+$78 = ${read_long(DDT_QUEUE):08X}")
print(f"  DDT+$80 = ${read_long(DDT_SAVED_SP):08X}")

# Find real JCB from JOBTBL ($0418)
jobtbl = read_long(0x0418) & 0xFFFFFF
print(f"  JOBTBL at $0418 = ${jobtbl:06X}")
if jobtbl != 0 and jobtbl < 0x400000:
    jcb0 = read_long(jobtbl) & 0xFFFFFF  # Job #0 = init job
    print(f"  JCB #0 (init job) = ${jcb0:06X}")
    # Also check a few more job slots
    for i in range(4):
        j = read_long(jobtbl + i * 4) & 0xFFFFFF
        if j != 0:
            print(f"  JCB #{i} = ${j:06X}")
else:
    jcb0 = 0

# JOBCUR=$7038 is actually the init job JCB (=DDT) — accept it
job_port_addr = raw_jobcur
ddt84_dirty = False  # Will handle pending I/O after function defs

# ─── Step 6b: Analyze $A03E kernel handler ───
print(f"\nStep 6b: $A03E kernel handler analysis...")
# Find LINE-A dispatch table
move_ext_6b = bus.read_word(0x070C)
disp_byte_6b = move_ext_6b & 0xFF
table_base_6b = 0x070C + disp_byte_6b
handler_3e_addr_6b = bus.read_word(table_base_6b + 0x03E)
handler_3c_addr_6b = bus.read_word(table_base_6b + 0x03C)
print(f"  LINE-A table base: ${table_base_6b:06X}")
print(f"  $A03C handler: ${handler_3c_addr_6b:06X}")
print(f"  $A03E handler: ${handler_3e_addr_6b:06X}")

print(f"  (handler disassembly skipped for brevity)")

# ─── Disk I/O bypass functions ───

def remove_ddt_from_jobcur_chain():
    """Remove DDT $7038 from the JOBCUR scheduling chain at $03A4+$78."""
    global skip_hook
    skip_hook = True
    prev = 0x03A4
    safety = 0
    while safety < 50:
        safety += 1
        next_entry = read_long(prev + 0x78) & 0xFFFFFF
        if next_entry == 0:
            break
        if next_entry == DDT_ADDR:
            our_next = read_long(DDT_ADDR + 0x78) & 0xFFFFFF
            raw_write_long(prev + 0x78, our_next)
            raw_write_long(DDT_ADDR + 0x78, 0)
            break
        prev = next_entry
        if prev >= 0x400000:
            break
    skip_hook = False

def find_ddb_for_ddt():
    """Walk DDB chain from $0408 to find a DDB referencing DDT $7038."""
    ddb = read_long(0x0408) & 0xFFFFFF
    found = []
    safety = 0
    while ddb != 0 and ddb < 0x400000 and safety < 20:
        safety += 1
        ddb_ddt = read_long(ddb + 0x08) & 0xFFFFFF
        if ddb_ddt == DDT_ADDR:
            found.append(ddb)
        ddb = read_long(ddb) & 0xFFFFFF
    return found

def handle_pending_disk_io():
    """Handle a pending disk I/O operation on DDT $7038."""
    global disk_io_count, skip_hook
    disk_io_count += 1

    # Dump full DDT state for debugging
    if disk_io_count <= 5:
        print(f"\n  DISK I/O #{disk_io_count}: DDT state dump:")
        print(f"    PC=${cpu.pc:06X} SR=${cpu.sr:04X}")
        for i in range(8):
            print(f"    D{i}=${cpu.d[i]:08X}  A{i}=${cpu.a[i]:08X}")
        print(f"    DDT ($7038) full dump:")
        for off in range(0, 0x88, 4):
            val = read_long(DDT_ADDR + off)
            if val != 0:
                print(f"      +${off:02X}: ${val:08X}")

    # Walk DDB chain to find DDBs for our DDT
    ddb_list = find_ddb_for_ddt()
    if disk_io_count <= 5:
        print(f"    DDB chain entries for DDT $7038: {[f'${d:06X}' for d in ddb_list]}")
        for ddb in ddb_list:
            print(f"    DDB at ${ddb:06X}:")
            for off in range(0, 0x30, 4):
                val = read_long(ddb + off)
                if val != 0 or off in (0x00, 0x08, 0x0C, 0x10):
                    print(f"      +${off:02X}: ${val:08X}")

    if not ddb_list:
        # No DDB queued — this is a mount or simple status command
        print(f"\n  DISK I/O #{disk_io_count}: MOUNT (no DDB)")
        skip_hook = True
        raw_write_long(DDT_STATUS, 0)
        skip_hook = False
        # DON'T remove from chain — let scheduler handle naturally
    else:
        # Use first DDB found
        ddb_ptr = ddb_list[0]

        # DDB format (discovered from dump):
        # +$00: link (long)
        # +$04: hardware address (long) — same as DD.HWA
        # +$08: DDT pointer (long)
        # +$0C: buffer address (long)
        # +$10: block number (long)
        ddb_buffer = read_long(ddb_ptr + 0x0C) & 0xFFFFFF
        ddb_block = read_long(ddb_ptr + 0x10)

        # Partition offset from DD (driver descriptor at $7AC2)
        partition_offset_raw = read_long(DRIVER_RAM + 0x14)

        # For mount (block=0), skip partition offset — read disk label at LBA 0
        # For reads (block>0), add partition offset
        if ddb_block == 0:
            partition_offset = 0
        else:
            partition_offset = partition_offset_raw & 0xFFFF

        # Calculate LBA — AMOS block N maps to LBA N+1 (LBA 0 is boot sector)
        lba = ddb_block + partition_offset + 1
        byte_offset = lba * 512

        if disk_io_count <= 10:
            print(f"\n  DISK I/O #{disk_io_count}: DDB at ${ddb_ptr:06X}")
            print(f"    Block=${ddb_block:08X} Buffer=${ddb_buffer:06X}")
            print(f"    Partition offset=${partition_offset} LBA={lba} "
                  f"(byte ${byte_offset:08X})")

        if byte_offset >= 0 and byte_offset + 512 <= len(disk_image):
            # Read 512 bytes from disk image into RAM buffer
            for i in range(0, 512, 2):
                w = read_word_le(disk_image, byte_offset + i)
                bus.write_word(ddb_buffer + i, w)

            if disk_io_count <= 5:
                print(f"    READ OK: {512} bytes → ${ddb_buffer:06X}")
                # Show first 16 words of data
                data_preview = []
                for j in range(0, 32, 2):
                    w = bus.read_word(ddb_buffer + j)
                    data_preview.append(f"${w:04X}")
                print(f"    Data: {' '.join(data_preview[:8])}")
                print(f"          {' '.join(data_preview[8:])}")
        else:
            print(f"    ERROR: LBA={lba} (offset ${byte_offset:08X}) out of range "
                  f"(image={len(disk_image)} bytes)")

        # Set success — DON'T remove from chain
        skip_hook = True
        raw_write_long(DDT_STATUS, 0)
        skip_hook = False

a03e_count = 0
a03c_count = 0
trapdoor_count = 0

def do_disk_read(ddb_ptr, log_prefix=""):
    """Read a disk block for the given DDB from the disk image."""
    global skip_hook
    ddb_buffer = read_long(ddb_ptr + 0x0C) & 0xFFFFFF
    ddb_block = read_long(ddb_ptr + 0x10)

    partition_offset_raw = read_long(DRIVER_RAM + 0x14)
    partition_offset = partition_offset_raw & 0xFFFF if ddb_block > 0 else 0
    # AMOS block N maps to LBA N+1 (LBA 0 is boot sector)
    lba = ddb_block + partition_offset + 1
    byte_offset = lba * 512

    if 0 <= byte_offset and byte_offset + 512 <= len(disk_image):
        skip_hook = True
        for i in range(0, 512, 2):
            w = read_word_le(disk_image, byte_offset + i)
            bus.write_word(ddb_buffer + i, w)

        # FIX: Block 0 is the disk label. Byte 0 = DK.FLG (disk flags).
        # The image has $00 here but mount code checks TST.B (A4) where
        # A4 = buffer address. Must be non-zero for mount to succeed.
        # $0F = formatted + MFD + bitmap + accounts flags.
        if ddb_block == 0:
            b0 = bus.read_byte(ddb_buffer)
            if b0 == 0:
                orig_write_byte(ddb_buffer, 0x0F)
                if not INTERACTIVE and a03c_count <= 20:
                    print(f"    ** Fixed label byte 0: $00 -> $0F at ${ddb_buffer:06X}")

        raw_write_long(DDT_STATUS, 0)
        # Also clear DDT+$00 (device status word) — the $A03E handler
        # checks this and yields while I/O pending bits are set.
        # Since we did the I/O synchronously, mark device as idle.
        raw_write_word(DDT_ADDR, 0)
        skip_hook = False
        return (True, ddb_block, lba, ddb_buffer)
    else:
        return (False, ddb_block, lba, ddb_buffer)


def handle_trapdoor_driver():
    """Handle trap-door driver call at $9100 — the scheduler dispatched to us."""
    global trapdoor_count, skip_hook
    trapdoor_count += 1

    if trapdoor_count <= 20:
        print(f"\n  TRAPDOOR #{trapdoor_count}: PC=${cpu.pc:06X}")
        print(f"    D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D2=${cpu.d[2]:08X}")
        print(f"    A0=${cpu.a[0]:08X} A1=${cpu.a[1]:08X} A2=${cpu.a[2]:08X}")
        print(f"    A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X} A6=${cpu.a[6]:08X}")

    # Find pending DDBs for our DDT
    ddb_list = find_ddb_for_ddt()
    if trapdoor_count <= 20:
        print(f"    DDBs: {len(ddb_list)}")

    handled_any = False
    for ddb_ptr in ddb_list:
        ok, blk, lba, buf = do_disk_read(ddb_ptr)
        if ok:
            handled_any = True
            if trapdoor_count <= 20:
                preview = [f"${bus.read_word(buf + j):04X}" for j in range(0, 16, 2)]
                print(f"    >> READ block={blk} LBA={lba} → ${buf:06X}")
                print(f"       Data: {' '.join(preview)}")
        else:
            if trapdoor_count <= 20:
                print(f"    >> OUT OF RANGE: block={blk} LBA={lba}")

    if not handled_any:
        # No DDBs or all out of range — set error
        if trapdoor_count <= 20:
            print(f"    >> No DDBs to handle, setting success anyway")
        skip_hook = True
        raw_write_long(DDT_STATUS, 0)
        skip_hook = False

    # Let the NOP+RTS at $9100 execute — will return to scheduler


def handle_a03c_io():
    """Handle $A03C LINE-A trap — queue I/O, handle immediately from disk image."""
    global a03c_count
    a03c_count += 1

    ddb_list = find_ddb_for_ddt()
    if a03c_count <= 20:
        print(f"\n  $A03C #{a03c_count}: PC=${cpu.pc:06X} DDBs={len(ddb_list)}")
        for ddb in ddb_list[:3]:
            buf = read_long(ddb + 0x0C) & 0xFFFFFF
            blk = read_long(ddb + 0x10)
            print(f"    DDB ${ddb:06X}: block={blk} buf=${buf:06X}")

    # Pre-handle I/O before kernel queues it
    for ddb_ptr in ddb_list:
        ok, blk, lba, buf = do_disk_read(ddb_ptr)
        if ok and a03c_count <= 20:
            print(f"    >> Pre-handled: block={blk} LBA={lba}")

    # Let $A03C execute normally


def handle_a03e_io():
    """Handle $A03E LINE-A trap — let it execute normally now that driver exists."""
    global a03e_count
    a03e_count += 1

    if a03e_count <= 10 or a03e_count in (50, 100, 500):
        ddt_status = bus.read_word(DDT_ADDR)
        io_stat = read_long(DDT_STATUS)
        ddb_list = find_ddb_for_ddt()
        print(f"\n  $A03E #{a03e_count}: DDT+$00=${ddt_status:04X} "
              f"DDT+$84=${io_stat:08X} DDBs={len(ddb_list)}")

    # Don't intercept — let kernel $A03E handler run
    # It will OR D6 into DDT+$00 and call scheduler
    # Scheduler should now dispatch to our trap-door driver at $9100

    if a03e_count > 1000:
        print(f"\n  $A03E LIMIT: {a03e_count} calls, stopping")
        cpu.halted = True

# ─── Step 6c: Handle pending I/O from boot ───
print(f"\nStep 6c: Handling pending I/O from boot...")

# Clear stale pending I/O from boot — DDT+$84=$FFFFFFFF was never serviced
# because DDT+$08 was zero during boot. Now DDT+$08=JMP $9100.
# Just clear the stale state so the OS can re-issue the request properly.
ddt84_val = read_long(DDT_STATUS)
if ddt84_val == 0xFFFFFFFF:
    skip_hook = True
    raw_write_long(DDT_STATUS, 0)
    skip_hook = False
    print(f"  Cleared stale DDT+$84 ($FFFFFFFF -> $00000000)")
else:
    print(f"  DDT+$84 = ${ddt84_val:08X} (not pending)")
ddt84_dirty = False
print(f"  JOBCUR = ${read_long(0x041C):08X}")
print(f"  DDT+$84 = ${read_long(DDT_STATUS):08X}")

# ─── Step 6d: Re-establish MEMBAS free chain ───
# The early boot write hook sets MEMBAS at $8C00, but ROM's init code
# fills RAM with $5252 pattern AFTER our hook, overwriting the free chain.
# Re-write it here, right before the main loop.
skip_hook = True
raw_write_long(0x0430, CORRECT_MEMBAS)
raw_write_long(0x0438, 0x3F0000)
raw_write_long(CORRECT_MEMBAS, 0)                          # next = 0 (single block)
raw_write_long(CORRECT_MEMBAS + 4, 0x3F0000 - CORRECT_MEMBAS)  # size of free block
skip_hook = False
mb_next = read_long(CORRECT_MEMBAS)
mb_size = read_long(CORRECT_MEMBAS + 4)
print(f"\nStep 6d: Re-established MEMBAS at ${CORRECT_MEMBAS:06X}")
print(f"  chain: next=${mb_next:08X} size=${mb_size:08X}")

# ─── Step 6e: Create terminal control block ───
# The init job JCB IS the DDT at $7038. DDT+$38 (= JCB+$38, terminal pointer)
# contains $3E8000 (a DDT device field), pointing to uninitialized RAM.
# The ACIA terminal init code allocated a terminal block via $A034/$A044 but
# never set JCB+$38 because the ACIA detect/handshake bypasses prevented the
# full initialization from completing.
# Create a minimal terminal control block so COMINT's input wait loop can work.
TCB_ADDR = 0x9080       # Terminal control block (needs ~$50 bytes: $9080-$90CF)
TCB_BUF_ADDR = 0x9110   # Input buffer (64 bytes, after TRAPDOOR at $9100-$9103)
TCB_BUF_SIZE = 64

skip_hook = True
# Clear the entire TCB area
for i in range(0, 0x70, 2):
    raw_write_word(TCB_ADDR + i, 0)
# term+$00: status word — bits 0 and 3 must be set so the wait loop at
# $1E18 (AND.W (A5),#9; BEQ → yield) passes. We intercept $A072 directly
# in the main loop to handle read pointer updates ourselves.
raw_write_word(TCB_ADDR + 0x00, 0x0009)
# term+$12: input character count (initially 0 — no input yet)
raw_write_word(TCB_ADDR + 0x12, 0)
# term+$1A: input buffer capacity
raw_write_long(TCB_ADDR + 0x1A, TCB_BUF_SIZE)
# term+$44: input buffer base pointer
raw_write_long(TCB_ADDR + 0x44, TCB_BUF_ADDR)
# term+$48: input buffer size
raw_write_long(TCB_ADDR + 0x48, TCB_BUF_SIZE)
# Clear the input buffer
for i in range(0, TCB_BUF_SIZE, 2):
    raw_write_word(TCB_BUF_ADDR + i, 0)

# Set JCB+$38 to point to our terminal block
raw_write_long(DDT_ADDR + 0x38, TCB_ADDR)

# Zero JCB+$20 — COMINT checks this at $390A to enter command-file mode.
# The OS's file-open code at $3720 fails (no disk driver module in chain),
# causing infinite $A03E yield loop. Instead, we zero JCB+$20 so COMINT
# takes the normal terminal-input path, and inject AMOSL.INI commands
# through the terminal buffer one by one (same mechanism as terminal I/O).
raw_write_word(DDT_ADDR + 0x20, 0)

skip_hook = False

# ─── Step 6f: Load AMOSL.INI for command file injection ───
import sys as _sys2
_sys2.path.insert(0, '/Volumes/RAID0/repos/Alpha-Python/lib')
from Alpha_Disk_Lib import AlphaDisk as _AlphaDisk
_amosl_ini_lines = []
_amosl_ini_pos = 0
_ini_data_pending = False  # True when data is in TCB buffer but TCB+$00 not yet set
with _AlphaDisk(str(src_path)) as _disk:
    _dsk0 = _disk.get_logical_device(0)
    _ini_data = _dsk0.read_file_contents((1, 4), "AMOSL", "INI")
    if _ini_data:
        # Parse into lines (AMOS uses CR+LF, terminated by LF=$0A)
        _text = ""
        for _b in _ini_data:
            if _b == 0:
                break
            _text += chr(_b)
        for _line in _text.split('\n'):
            _line = _line.rstrip('\r')
            if _line:  # skip empty lines
                _amosl_ini_lines.append(_line)
        print(f"\nStep 6f: Loaded AMOSL.INI: {len(_amosl_ini_lines)} command lines")
        for i, l in enumerate(_amosl_ini_lines[:10]):
            print(f"  [{i}] {l}")
        if len(_amosl_ini_lines) > 10:
            print(f"  ... ({len(_amosl_ini_lines) - 10} more)")
    else:
        print("\nStep 6f: WARNING - AMOSL.INI not found!")

print(f"\nStep 6e: Created terminal control block at ${TCB_ADDR:06X}")
print(f"  term+$00=${bus.read_word(TCB_ADDR):04X} term+$44=${read_long(TCB_ADDR+0x44):08X}"
      f" term+$48=${read_long(TCB_ADDR+0x48):08X}")
print(f"  JCB+$38 at ${DDT_ADDR+0x38:06X}: ${read_long(DDT_ADDR+0x38):08X}")
print(f"  JCB+$20 at ${DDT_ADDR+0x20:06X}: ${bus.read_word(DDT_ADDR+0x20):04X}")

# ─── Step 7: Main execution with ACIA bypasses + disk I/O ───

print(f"\n{'='*60}")
print("Step 7: RUNNING WITH ACIA BYPASSES + DISK I/O BYPASS")
print(f"{'='*60}")

bypass_counts = {
    0x006C3E: 0,
    0x006D80: 0,
    0x006BDE: 0,
    0x006D9C: 0,
    0x006B68: 0,
}
a00a_intercepts = 0
a006_count = 0
a006_chars = bytearray()
prompt_count = 0          # How many '.' prompts we've seen
input_injected = False    # Whether we've injected input for current prompt
lf_processed = False      # Whether line input handler returned after LF
lf_processed_at = 0       # Instruction count when LF was processed

DRIVER_END = DRIVER_RAM + MAX_COPY
driver_entries = 0
last_driver_pc = None

extra_count = 0
max_extra = 200_000_000 if INTERACTIVE else 50_000_000
disk_a03c_just_skipped = False
comint_stack_fixed = False

# --- Interactive terminal setup ---
_old_termios = None
_stdin_buf = bytearray()  # Characters waiting to be injected into TCB

def _setup_raw_terminal():
    global _old_termios
    if sys.stdin.isatty():
        _old_termios = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())

def _restore_terminal():
    if _old_termios is not None:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_termios)

_stdin_eof = False

def _poll_stdin():
    """Non-blocking read from stdin, returns bytes available."""
    global _stdin_eof
    if _stdin_eof:
        return bytearray()
    result = bytearray()
    try:
        while select.select([sys.stdin], [], [], 0)[0]:
            ch = os.read(sys.stdin.fileno(), 1)
            if not ch:  # EOF
                _stdin_eof = True
                break
            result.append(ch[0])
    except (OSError, ValueError):
        _stdin_eof = True
    return result

def _inject_stdin_to_tcb():
    """Move buffered stdin characters into TCB input buffer."""
    global skip_hook
    tcb_count = bus.read_word(TCB_ADDR + 0x12)
    if tcb_count == 0:
        # Reset read pointer when buffer is empty
        skip_hook = True
        raw_write_long(TCB_ADDR + 0x1E, TCB_BUF_ADDR)
        skip_hook = False
    # Ensure TCB status has bits 0+3 set (required by wait loop at $1E18)
    # Boot code at $80B2/$38E8 clears this after our initial setup
    skip_hook = True
    raw_write_word(TCB_ADDR + 0x00, 0x0009)
    skip_hook = False
    while _stdin_buf and tcb_count < TCB_BUF_SIZE:
        ch = _stdin_buf.pop(0)
        if ch == 3:  # Ctrl+C
            cpu.halted = True
            return
        if ch == 13:  # CR → LF for AMOS
            ch = 0x0A
        wr_pos = TCB_BUF_ADDR + tcb_count
        skip_hook = True
        bus.write_byte(wr_pos, ch)
        raw_write_word(TCB_ADDR + 0x12, tcb_count + 1)
        skip_hook = False
        tcb_count += 1
        # Echo the character to terminal
        if ch == 0x0A:
            sys.stdout.buffer.write(b'\r\n')
        elif 0x20 <= ch < 0x7F:
            sys.stdout.buffer.write(bytes([ch]))
        sys.stdout.buffer.flush()

if INTERACTIVE:
    sys.stdout = _real_stdout  # Restore stdout after boot
    sys.stdout.buffer.write(b"\r\n=== AMOS Interactive Mode ===\r\n"
                            b"Type commands at the '.' prompt. Ctrl+C to exit.\r\n\r\n")
    sys.stdout.buffer.flush()
    _setup_raw_terminal()

while not cpu.halted and extra_count < max_extra:
    pc = cpu.pc

    # --- Interactive: poll stdin periodically (buffer only, don't inject yet) ---
    if INTERACTIVE and extra_count % 256 == 0:
        new_chars = _poll_stdin()
        if new_chars:
            _stdin_buf.extend(new_chars)

    # === LINE-A opcode intercepts for disk I/O ===
    if disk_io_enabled:
        try:
            op = bus.read_word(pc)
        except:
            op = 0

        # $A03C — queue I/O: intercept and handle synchronously for disk
        # A0 = DDT for mount operations, A0 = DDB for file I/O
        # DDB+$08 contains DDT pointer; check if it references our disk DDT
        if op == 0xA03C:
            a0 = cpu.a[0] & 0xFFFFFF
            is_ddt = (a0 == DDT_ADDR)
            is_disk_ddb = False
            if not is_ddt and a0 > 0 and a0 < 0x400000:
                try:
                    ddb_ddt = read_long(a0 + 0x08) & 0xFFFFFF
                    is_disk_ddb = (ddb_ddt == DDT_ADDR)
                except:
                    pass

            if not INTERACTIVE and not is_ddt and not is_disk_ddb and a03c_count < 5:
                # Log unhandled $A03C calls for debugging
                ddb_ddt_val = 0
                if a0 > 0 and a0 < 0x400000:
                    try:
                        ddb_ddt_val = read_long(a0 + 0x08) & 0xFFFFFF
                    except:
                        pass
                print(f"\n  $A03C SKIPPED: A0=${a0:06X} DDB+$08=${ddb_ddt_val:06X} "
                      f"D6=${cpu.d[6]:08X} PC=${pc:06X}")

            if is_ddt or is_disk_ddb:
                a03c_count += 1
                d6 = cpu.d[6] & 0xFF

                if is_disk_ddb:
                    # A0 is a DDB — do I/O directly on it
                    ok, blk, lba, buf = do_disk_read(a0)
                    if not INTERACTIVE and a03c_count <= 40:
                        if ok:
                            preview = [f"${bus.read_word(buf + j):04X}" for j in range(0, 16, 2)]
                            print(f"\n  $A03C #{a03c_count}: DDB ${a0:06X} D6={d6} READ block={blk} LBA={lba} → ${buf:06X}")
                            print(f"    Data: {' '.join(preview)}")
                        else:
                            print(f"\n  $A03C #{a03c_count}: DDB ${a0:06X} D6={d6} OUT OF RANGE block={blk} LBA={lba}")
                else:
                    # A0 is DDT — mount/status, also handle any queued DDBs
                    ddb_list = find_ddb_for_ddt()
                    for ddb_ptr in ddb_list:
                        ok, blk, lba, buf = do_disk_read(ddb_ptr)
                        if not INTERACTIVE and a03c_count <= 40:
                            if ok:
                                preview = [f"${bus.read_word(buf + j):04X}" for j in range(0, 16, 2)]
                                print(f"\n  $A03C #{a03c_count}: DDT+DDB ${ddb_ptr:06X} READ block={blk} LBA={lba} → ${buf:06X}")
                                print(f"    Data: {' '.join(preview)}")
                            else:
                                print(f"\n  $A03C #{a03c_count}: DDT+DDB OUT OF RANGE block={blk} LBA={lba}")
                    if not ddb_list:
                        if not INTERACTIVE and a03c_count <= 40:
                            print(f"\n  $A03C #{a03c_count}: MOUNT (no DDB) D6={d6}")
                        skip_hook = True
                        raw_write_long(DDT_STATUS, 0)
                        skip_hook = False

                cpu.pc = (pc + 2) & 0xFFFFFFFF
                disk_a03c_just_skipped = True
                if not INTERACTIVE and a03c_count <= 40:
                    next_op = bus.read_word(cpu.pc)
                    print(f"    Caller PC=${pc:06X}, next at ${cpu.pc:06X}: ${next_op:04X}")
                extra_count += 1
                continue

        # $A03E — yield/wait: if INI data is pending in TCB buffer,
        # set TCB+$00 = $0009 so TTYLIN's wait loop finds data on next check.
        if op == 0xA03E and _ini_data_pending:
            skip_hook = True
            raw_write_word(TCB_ADDR + 0x00, 0x0009)
            skip_hook = False
            _ini_data_pending = False
            # Skip the yield — data is ready, no need to wait
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            extra_count += 1
            continue

        # $A03E — yield/wait: skip if disk I/O was just handled synchronously.
        # After synchronous $A03C, the caller may issue MULTIPLE $A03E calls
        # (e.g., file-open code loops waiting for I/O completion). Skip them all
        # within a window after the last $A03C intercept.
        if op == 0xA03E and disk_a03c_just_skipped:
            if not INTERACTIVE and a03c_count <= 40:
                print(f"    $A03E skipped at PC=${pc:06X} (disk I/O done synchronously)")
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            extra_count += 1
            continue

        # Clear the skip flag when we execute a non-$A03E instruction
        # (means the caller has moved on past the I/O wait)
        if disk_a03c_just_skipped and op != 0xA03E:
            disk_a03c_just_skipped = False

        # $A064 — memory module validation: bypass with Z=1 (success)
        # During early boot, JCB+$18 (module chain) is NULL, so all validation
        # fails with "Memory Map Destroyed". Bypass until modules are loaded.
        if op == 0xA064:
            if not INTERACTIVE and extra_count < 50000:
                print(f"  >> $A064 BYPASS at [{extra_count}] PC=${pc:06X}")
            # Set Z flag in SR to indicate success (BEQ will be taken by caller)
            cpu.sr = (cpu.sr & ~0x04) | 0x04  # set Z bit
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            extra_count += 1
            continue

        # $A006 — type character
        if op == 0xA006 and pc < 0x8000:
            a006_count += 1
            d1 = cpu.d[1] & 0x7F
            a006_chars.append(d1)
            if INTERACTIVE:
                # Write directly to stdout with CR/LF translation
                if d1 == 0x0A:
                    sys.stdout.buffer.write(b'\r\n')
                elif d1 == 0x0D:
                    sys.stdout.buffer.write(b'\r')
                elif d1 >= 0x20:
                    sys.stdout.buffer.write(bytes([d1]))
                sys.stdout.buffer.flush()
                banner_chars.append(d1)
            else:
                if d1 >= 0x20 or d1 in (0x0A, 0x0D):
                    acia.write(0xFFFFC9, 1, d1)
                    banner_chars.append(d1)
                if a006_count <= 30:
                    print(f"\n  $A006 #{a006_count}: char=${d1:02X} ('{chr(d1) if 0x20<=d1<0x7F else '?'}')"
                          f" PC=${pc:06X}")
            if d1 == 0x2E:  # '.' prompt character
                prompt_count += 1
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            extra_count += 1
            continue

        # $A008 — TTYLIN (read line): inject AMOSL.INI lines here.
        # The TTYLIN handler at $1E00 checks term+$00 & 9: if nonzero, returns
        # immediately without reading. If zero, enters wait loop calling $A03E.
        # Strategy: inject data into TCB buffer but leave TCB+$00 = 0. The handler
        # enters the wait loop, and at $A03E we set TCB+$00 = $0009 so the handler
        # finds data on its next iteration and reads via $A072.
        if op == 0xA008 and pc < 0x8000:
            if not INTERACTIVE and _amosl_ini_pos < len(_amosl_ini_lines):
                line = _amosl_ini_lines[_amosl_ini_pos]
                _amosl_ini_pos += 1
                cmd = line.encode('ascii', errors='replace') + b'\x0A'
                skip_hook = True
                for ci, ch in enumerate(cmd):
                    bus.write_byte(TCB_BUF_ADDR + ci, ch)
                raw_write_long(TCB_ADDR + 0x1E, TCB_BUF_ADDR)
                raw_write_word(TCB_ADDR + 0x12, len(cmd))
                # DO NOT set TCB+$00 here — leave it at 0 so TTYLIN enters wait loop
                raw_write_word(TCB_ADDR + 0x00, 0x0000)
                skip_hook = False
                _ini_data_pending = True
                print(f"\n  >> INI [{_amosl_ini_pos}/{len(_amosl_ini_lines)}]: '{line}'"
                      f" PC=${pc:06X}")
            elif not INTERACTIVE and _amosl_ini_pos == len(_amosl_ini_lines):
                _amosl_ini_pos += 1
                print(f"\n  >> AMOSL.INI COMPLETE: {len(_amosl_ini_lines)} lines processed")
            elif not INTERACTIVE and _amosl_ini_pos > len(_amosl_ini_lines) and prompt_count > len(_amosl_ini_lines) + 10:
                print(f"\n  >> {prompt_count} prompts seen (INI done), stopping")
                cpu.halted = True
            # Let the OS's $A008 handler run — it enters wait loop, finds data at $A03E

        # $A072 — read terminal character: intercept and serve from our buffer
        # The OS handler's shortcut path (TCB+$00 & 9 != 0) skips updating
        # the read pointer term+$1E, causing repeated reads of the same char.
        # Fix: handle $A072 entirely in Python.
        if op == 0xA072 and pc < 0x8000:
            tcb_count = bus.read_word(TCB_ADDR + 0x12)
            if tcb_count > 0:
                # Read character from buffer at term+$1E
                rd_ptr = read_long(TCB_ADDR + 0x1E)
                ch = bus.read_byte(rd_ptr) if rd_ptr < 0x400000 else 0
                # Update: advance read pointer, decrement count
                skip_hook = True
                raw_write_long(TCB_ADDR + 0x1E, rd_ptr + 1)
                raw_write_word(TCB_ADDR + 0x12, tcb_count - 1)
                skip_hook = False
                # Set D1 to the character (byte only, preserve upper bits)
                cpu.d[1] = (cpu.d[1] & 0xFFFFFF00) | ch
                cpu.pc = (pc + 2) & 0xFFFFFFFF
                extra_count += 1
                continue

        # Non-interactive: halt after INI processing when terminal wait is reached
        if not INTERACTIVE and op == 0xA03E and (cpu.d[6] & 0xFFFF) == 2 and _amosl_ini_pos > len(_amosl_ini_lines):
            print(f"\n  >> INI processing complete, system waiting for terminal input. Halting.")
            cpu.halted = True

        # Interactive mode: inject stdin at $A03E yield points
        if op == 0xA03E and prompt_count > 0 and (cpu.d[6] & 0xFFFF) == 2:
            if INTERACTIVE:
                new_chars = _poll_stdin()
                if new_chars:
                    _stdin_buf.extend(new_chars)
                if _stdin_buf:
                    _inject_stdin_to_tcb()
                elif _stdin_eof and bus.read_word(TCB_ADDR + 0x12) == 0:
                    cpu.halted = True
                if bus.read_word(TCB_ADDR + 0x12) > 0:
                    skip_hook = True
                    raw_write_word(TCB_ADDR + 0x00, 0x0009)
                    skip_hook = False

    # (diagnostic traces removed — COMINT command processing verified working)

    # === Terminal init trace: $6C6A-$6D20 ===
    if not INTERACTIVE and disk_io_enabled and 0x6C6A <= pc <= 0x6D20 and extra_count < 5000:
        try:
            op = bus.read_word(pc)
        except:
            op = 0
        print(f"  >> TERMINIT [{extra_count}] PC=${pc:06X} op=${op:04X}"
              f" A0=${cpu.a[0]&0xFFFFFF:06X} A2=${cpu.a[2]&0xFFFFFF:06X}"
              f" D1=${cpu.d[1]:08X} D7=${cpu.d[7]:08X} SR=${cpu.sr:04X}")

    # === Code path trace: $3700-$37D0 (memory validation area) ===
    if not INTERACTIVE and disk_io_enabled and 0x3700 <= pc <= 0x37D0 and extra_count < 50000:
        try:
            op = bus.read_word(pc)
        except:
            op = 0
        print(f"  >> MEMVAL [{extra_count}] PC=${pc:06X} op=${op:04X}"
              f" D6=${cpu.d[6]:08X} D1=${cpu.d[1]:08X} A4=${cpu.a[4]&0xFFFFFF:06X}")

    # === COMINT error trace ===
    if not INTERACTIVE and pc == 0x0068AA and disk_io_enabled and extra_count < 50000:
        # $68AA: MOVE.W D0,D6 — D0 is the error message number
        print(f"  >> COMINT ERRMSG at [{extra_count}]: D0=${cpu.d[0]:08X} D6=${cpu.d[6]:08X}"
              f" A3=${cpu.a[3]:08X} (A3)=${bus.read_word(cpu.a[3] & 0xFFFFFF):04X}"
              f" A1=${cpu.a[1]:08X}")

    # === Trap-door driver intercept (fallback) ===
    if pc == TRAPDOOR_ADDR and disk_io_enabled:
        handle_trapdoor_driver()

    # === COMINT stack relocation ===
    # Init job SP can be inside the system variable area ($0400-$04FF).
    # COMINT's MOVEM.L D0-D5/A0-A5,-(SP) pushes 48 bytes below SP,
    # overwriting JOBCUR ($041C), JOBTBL ($0418), etc. → crash.
    # Fix: every time COMINT enters at $682E with SP in danger zone,
    # relocate SP to safe area at $8700 (below MEMBAS=$8800).
    # Also patch JCB+$80 (saved SP in job control block) so the scheduler
    # restores SP to the safe area on future dispatches.
    if pc == 0x00682E and disk_io_enabled:
        old_sp = cpu.a[7] & 0xFFFFFF
        if old_sp < 0x8000:  # SP is in danger zone (system area or low RAM)
            new_sp = INIT_JOB_SP  # Top of init job stack ($8800-$91FF)
            # Copy exception frame: SR (2 bytes) + PC (4 bytes) = 6 bytes
            skip_hook = True
            for i in range(0, 6, 2):
                w = bus.read_word(old_sp + i)
                bus.write_word(new_sp + i, w)
            skip_hook = False
            cpu.a[7] = (new_sp & 0xFFFFFFFF)
            # Patch JCB+$80 (saved SP) so scheduler restores to safe area
            skip_hook = True
            raw_write_long(DDT_ADDR + 0x80, new_sp)
            skip_hook = False
            if not comint_stack_fixed:
                comint_stack_fixed = True
                if not INTERACTIVE:
                    print(f"  >> COMINT STACK FIX: SP ${old_sp:06X} -> ${new_sp:06X}, JCB+$80 patched")
        # Restore DDT+$0C to dummy module pointer so $A052 returns valid ptr.
        # (Previously zeroed here, but that caused memory validation failures.)
        skip_hook = True
        raw_write_long(DDT_ADDR + 0x0C, DUMMY_MODULE)
        skip_hook = False

    # === Post-injection tracing ===
    if not INTERACTIVE and input_injected and extra_count < (inject_at + 5000) if 'inject_at' in dir() else False:
        try:
            op2 = bus.read_word(pc)
        except:
            op2 = 0
        pass  # (post-injection trace removed)

    # === Key code path tracking ===
    if not INTERACTIVE and disk_io_enabled and extra_count < 50000:
        if pc == 0x00682E:  # COMINT entry
            sp = cpu.a[7] & 0xFFFFFF
            # Read exception frame from stack: SR(2) + PC(4)
            stk_sr = bus.read_word(sp) if sp < 0x400000 else 0
            stk_pc = read_long(sp + 2) if sp + 2 < 0x400000 else 0
            print(f"  >> COMINT at {extra_count}: D6=${cpu.d[6]:08X} D0=${cpu.d[0]:08X}"
                  f" SP=${sp:06X} A6=${cpu.a[6]&0xFFFFFF:06X}"
                  f" frame:SR=${stk_sr:04X} PC=${stk_pc:08X}"
                  f" JOBCUR=${read_long(0x041C):08X}")
        elif pc == 0x001C30:  # SRCH handler
            print(f"  >> SRCH at {extra_count}")
        elif pc == 0x002B1C:  # GETMEM handler
            d1_req = cpu.d[1]
            membas = read_long(0x0430)
            mb_next = read_long(membas) if membas < 0x400000 else 0xDEAD
            mb_size = read_long(membas + 4) if membas + 4 < 0x400000 else 0xDEAD
            print(f"  >> GETMEM at {extra_count}: req=${d1_req:08X}"
                  f" MEMBAS=${membas:08X} chain:[next=${mb_next:08X} size=${mb_size:08X}]")
        elif pc == 0x0012F4:  # $A03C handler (should NOT fire for disk)
            a0v = cpu.a[0] & 0xFFFFFF
            ddb08 = read_long(a0v + 0x08) & 0xFFFFFF if 0 < a0v < 0x400000 else 0
            print(f"  >> $A03C HANDLER at {extra_count}: A0=${a0v:06X} DDB+$08=${ddb08:06X} D6=${cpu.d[6]:08X} (UNEXPECTED!)")
        elif pc == 0x0011DE:  # $A03E handler
            a0v = cpu.a[0] & 0xFFFFFF
            ddb08 = read_long(a0v + 0x08) & 0xFFFFFF if 0 < a0v < 0x400000 else 0
            print(f"  >> $A03E HANDLER at {extra_count}: A0=${a0v:06X} DDB+$08=${ddb08:06X} D6=${cpu.d[6]:04X}")
        # Mount code trace ($503C-$506A and callers)
        elif 0x503C <= pc <= 0x510A:
            try:
                op = bus.read_word(pc)
            except:
                op = 0
            a4 = cpu.a[4] & 0xFFFFFF
            a4_byte = bus.read_byte(a4) if a4 < 0x400000 else -1
            a4_1f = bus.read_byte(a4 + 0x1F) if a4 + 0x1F < 0x400000 else -1
            extra_info = ""
            if pc == 0x503C:
                sp = cpu.a[7] & 0xFFFFFF
                stk_top = read_long(sp) if sp < 0x400000 else 0
                extra_info = f" SP=${sp:06X} (SP)=${stk_top:08X}"
            elif pc == 0x5042:  # BNE.S $505C — branch check after counter decrement
                extra_info = f" Z={1 if (cpu.sr & 0x04) else 0} (BNE→{'$505C' if not (cpu.sr & 0x04) else 'fall'})"
            elif pc == 0x5044:  # TST.B (A4) — test device status
                extra_info = f" (A4)=${a4_byte:02X} → {'NZ' if a4_byte else 'ZERO'}"
            elif pc == 0x5046:  # BLE.S $505C — branch if <= 0
                extra_info = f" N={1 if (cpu.sr & 0x08) else 0} Z={1 if (cpu.sr & 0x04) else 0}"
            elif pc == 0x505C:  # CLR.L D7
                extra_info = " (about to check (A4) for error)"
            elif pc == 0x505E:  # TST.B (A4)
                extra_info = f" (A4)=${a4_byte:02X}"
            elif pc == 0x5062:  # MOVEQ #4,D7 — error code
                extra_info = " ERROR: D7=4 (mount failed)"
            elif pc == 0x5064:  # MOVEM.L — restore regs
                extra_info = f" D7=${cpu.d[7]:08X}"
            elif pc == 0x506A:  # RTE
                extra_info = f" D7=${cpu.d[7]:08X} (return code)"
            print(f"  >> MOUNT [{extra_count}] PC=${pc:06X} op=${op:04X}"
                  f" A4=${a4:06X} (A4)=${a4_byte:02X} A4+$1F=${a4_1f:02X}"
                  f" D5=${cpu.d[5]:08X}{extra_info}")
        elif 0x4E00 <= pc <= 0x4E5E:
            try:
                op = bus.read_word(pc)
            except:
                op = 0
            print(f"  >> CALLER [{extra_count}] PC=${pc:06X} op=${op:04X}"
                  f" A0=${cpu.a[0]:08X} A3=${cpu.a[3]:08X} D0=${cpu.d[0]:08X}"
                  f" D6=${cpu.d[6]:08X}")

    # === ACIA Bypasses ===

    if pc == 0x006C3E:
        bypass_counts[0x006C3E] += 1
        cpu.d[1] = 0x02
        cpu.d[2] = 0x28
        acia.write(0xFFFFC8, 1, 0x03)
        acia.write(0xFFFFC8, 1, 0x15)
        acia._echo_enabled = [False, False, False]
        cpu.pc = 0x006C5E
        if not INTERACTIVE and bypass_counts[0x006C3E] <= 3:
            print(f"  BYPASS terminal detect #{bypass_counts[0x006C3E]}")
        # Enable disk I/O bypass after first terminal init
        if not disk_io_enabled:
            disk_io_enabled = True
            ddt84_dirty = False  # Reset any stale dirty flag
            # Check MEMBAS chain integrity
            mb = read_long(0x0430)
            mb_next = read_long(mb) if mb < 0x400000 else 0xDEAD
            mb_size = read_long(mb + 4) if mb < 0x400000 else 0xDEAD
            if not INTERACTIVE:
                print(f"  >> DISK I/O BYPASS ENABLED")
            if not INTERACTIVE:
                print(f"     MEMBAS=${mb:08X} chain: next=${mb_next:08X} size=${mb_size:08X}")
        extra_count += 1
        continue

    if pc == 0x006D80:
        bypass_counts[0x006D80] += 1
        a0 = cpu.a[0] & 0xFFFFFF
        cpu.a[6] = (a0 + 0x0634) & 0xFFFFFFFF
        for off in range(10):
            bus.write_byte(a0 + 0x062A + off, 0)
        bus.write_byte(a0 + 0x0634, 0x03)
        # Terminal type byte at A0+$0636 — must NOT be 6 (retry code).
        # Use $02 for standard terminal. This is checked at $6C7A.
        bus.write_byte(a0 + 0x0636, 0x02)
        bus.write_byte(a0 + 0x0638, 0x0C)
        acia.write(0xFFFFC8, 1, 0x15)
        acia._tdre[0] = True
        acia._tsr_active[0] = False
        acia._tdr_full[0] = False
        acia._rx_cooldown[0] = 0
        acia._echo_pending[0].clear()
        ret_addr = read_long(cpu.a[7] & 0xFFFFFF)
        cpu.a[7] = (cpu.a[7] + 4) & 0xFFFFFFFF
        cpu.pc = ret_addr
        # Real handshake returns with MOVE #4,CCR (Z=1, all others clear).
        # Must set Z=1 so BNE at $6C6A falls through to terminal init.
        cpu.sr = (cpu.sr & 0xFF00) | 0x04  # CCR = Z=1
        if not INTERACTIVE and bypass_counts[0x006D80] <= 3:
            print(f"  BYPASS handshake #{bypass_counts[0x006D80]} -> ${ret_addr:06X} (Z=1)"
                  f" A0=${a0:06X} A5=${cpu.a[5]&0xFFFFFF:06X}"
                  f" A0+$636=${bus.read_byte(a0+0x636):02X}")
        extra_count += 1
        continue

    if pc == 0x006BDE:
        bypass_counts[0x006BDE] += 1
        cpu.d[7] = 0x16
        cpu.pc = 0x006BEC
        extra_count += 1
        continue

    if pc == 0x006D9C:
        bypass_counts[0x006D9C] += 1
        cpu.d[7] = 0x0E
        cpu.pc = 0x006DAA
        extra_count += 1
        continue

    if pc == 0x006B68:
        bypass_counts[0x006B68] += 1
        if (acia._control[0] & 0x03) != 0x03:
            cpu.pc = 0x006B70
            extra_count += 1
            continue

    # LINE-A $A00A intercept — direct character output
    if pc == 0x002AB0:
        a00a_intercepts += 1
        d1 = cpu.d[1] & 0x7F
        banner_chars.append(d1)
        if INTERACTIVE:
            if d1 == 0x0A:
                sys.stdout.buffer.write(b'\r\n')
            elif d1 == 0x0D:
                sys.stdout.buffer.write(b'\r')
            elif d1 >= 0x20:
                sys.stdout.buffer.write(bytes([d1]))
            sys.stdout.buffer.flush()
        else:
            acia.write(0xFFFFC9, 1, d1)
            if a00a_intercepts <= 100:
                sp = cpu.a[7] & 0xFFFFFF
                rets = []
                for ri in range(0, 24, 4):
                    rv = read_long(sp + ri) if sp + ri < 0x400000 else 0
                    rets.append(f"${rv:06X}")
                ch = chr(d1) if 0x20 <= d1 < 0x7F else '.'
                print(f"  $A00A [{extra_count}] '{ch}' (${d1:02X}) SP=${sp:06X} stack={','.join(rets[:4])}")
            if a00a_intercepts == 1:
                mb = read_long(0x0430)
                mb_n = read_long(mb) if 0 < mb < 0x400000 else 0xDEAD
                mb_s = read_long(mb + 4) if 0 < mb < 0x400000 else 0xDEAD
                print(f"    ** MEMBAS at first $A00A: ${mb:08X} next=${mb_n:08X} size=${mb_s:08X}")
                print(f"    ** D0-D7: " + " ".join(f"${cpu.d[i]:08X}" for i in range(8)))
                print(f"    ** A0-A7: " + " ".join(f"${cpu.a[i]:08X}" for i in range(8)))
                sp = cpu.a[7] & 0xFFFFFF
                for si in range(0, 64, 2):
                    w = bus.read_word(sp + si)
                    print(f"    ** SP+${si:02X}: ${w:04X}", end="")
                print()
        cpu.pc = 0x002AB2
        extra_count += 1
        continue

    # === Driver tracking ===
    if not INTERACTIVE and (DRIVER_RAM <= pc < DRIVER_END or pc == CANTMR_DST or pc == CLRCDB_DST):
        if DRIVER_RAM <= pc < DRIVER_END and (last_driver_pc is None or
            not (DRIVER_RAM <= last_driver_pc < DRIVER_END)):
            driver_entries += 1
            if driver_entries <= 10:
                off = pc - DRIVER_RAM
                print(f"\n  DRIVER entry #{driver_entries} at PC=${pc:06X} (+${off:04X})")
                print(f"    D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} A0=${cpu.a[0]:08X}")
                print(f"    A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X} SR=${cpu.sr:04X}")
        last_driver_pc = pc
    else:
        last_driver_pc = pc

    cpu.step()
    bus.tick(1)
    extra_count += 1

    if not INTERACTIVE and extra_count % 5_000_000 == 0:
        print(f"\n  Progress: {count + extra_count} instrs, PC=${cpu.pc:06X}, "
              f"disk_io={disk_io_count}, driver={driver_entries}, banner={len(banner_chars)}")

# Restore terminal before printing results
_restore_terminal()

# ─── Results ───
if not INTERACTIVE:
    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    print(f"  Total: {count + extra_count} instructions")
    print(f"  Disk I/O ops: {disk_io_count} (DDT+$84), {a03e_count} ($A03E)")
    print(f"  Driver entries: {driver_entries}")
    print(f"  $A00A intercepts: {a00a_intercepts}")
    print(f"  $A006 intercepts: {a006_count}")
    print(f"  Prompts seen: {prompt_count}, Input injections: {prompt_count if input_injected else prompt_count - 1}")
    print(f"  Bypasses: C3E={bypass_counts[0x006C3E]} D80={bypass_counts[0x006D80]} "
          f"BDE={bypass_counts[0x006BDE]} D9C={bypass_counts[0x006D9C]} B68={bypass_counts[0x006B68]}")

    # Final state
    print(f"\n  DDT+$84 = ${read_long(DDT_STATUS):08X}")
    print(f"  DDT+$78 = ${read_long(DDT_QUEUE):08X}")
    print(f"  JOBCUR = ${read_long(0x041C):08X}")

    if cpu.halted:
        print(f"  CPU HALTED at PC=${cpu.pc:06X}")
    else:
        print(f"  Final PC=${cpu.pc:06X}")
        # Dump code at final PC
        print(f"\n  Code at final PC:")
        for addr in range(cpu.pc - 8, cpu.pc + 16, 2):
            w = bus.read_word(addr)
            m = " <-- PC" if addr == cpu.pc else ""
            print(f"    ${addr:06X}: ${w:04X}{m}")
        # Check stack
        sp = cpu.a[7] & 0xFFFFFF
        print(f"\n  Stack at SP=${sp:06X}:")
        for i in range(0, 16, 2):
            w = bus.read_word(sp + i)
            print(f"    ${sp + i:06X}: ${w:04X}")
        print(f"\n  Registers:")
        for i in range(8):
            print(f"    D{i}=${cpu.d[i]:08X}  A{i}=${cpu.a[i]:08X}")
else:
    print(f"\r\n\r\n[Session ended: {count + extra_count} instructions, {prompt_count} prompts]")

if not INTERACTIVE:
    if banner_chars:
        text = bytes(banner_chars).decode('ascii', errors='replace')
        printable = ''.join(c if (0x20 <= ord(c) < 0x7F or c in '\r\n') else '.' for c in text)
        print(f"\n  Banner text ({len(banner_chars)} chars):")
        for line in printable.split('\n')[:30]:
            if line.strip():
                print(f"    {line.rstrip()}")

    if a006_chars:
        text = bytes(a006_chars).decode('ascii', errors='replace')
        printable = ''.join(c if (0x20 <= ord(c) < 0x7F or c in '\r\n') else '.' for c in text)
        print(f"\n  $A006 output ({len(a006_chars)} chars):")
        for line in printable.split('\n')[:30]:
            print(f"    {line.rstrip()}")

    print(f"\nDone!")
