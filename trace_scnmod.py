#!/usr/bin/env python3
"""Trace early AMOSL.INI command flow through COMINT/SCNMOD.

Goal: feed real AMOSL.INI lines through the patched terminal path and find the
next milestone after the built-in SYSTEM work: reaching the first TRMDEF line
and confirming the monitor asks for the following line.

Uses the same boot infrastructure as patch_driver_v7.py.
"""
import sys
import os
sys.path.insert(0, ".")
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

DDT_ADDR = 0x7038
DDT_QUEUE = DDT_ADDR + 0x78
DDT_SAVED_SP = DDT_ADDR + 0x80
DDT_STATUS = DDT_ADDR + 0x84

CORRECT_MEMBAS = 0x9400
INIT_JOB_SP = 0x9200
DUMMY_MODULE = 0x90F0
TRAPDOOR_ADDR = 0x9100
TCB_ADDR = 0x9080
TCB_BUF_ADDR = 0x9110
TCB_BUF_SIZE = 64

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

def format_bytes(data):
    return ''.join(chr(b) if 0x20 <= b < 0x7F else f'<{b:02X}>' for b in data)

# ─── Step 1: Create patched disk image ───
print("Step 1: Creating disk patch...")
with open(src_path, "rb") as f:
    img = bytearray(f.read())
disk_image = img

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

import shutil
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

# ─── Step 2: Boot to $006C0E ───
print("Step 2: Booting...")
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

# Write hook (same as v7 but minimal)
mem_patch_done = False
write_043B_count = 0
job_port_addr = 0
disk_io_enabled = False

def find_ddb_for_ddt():
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

def do_disk_read(ddb_ptr):
    global skip_hook
    ddb_buffer = read_long(ddb_ptr + 0x0C) & 0xFFFFFF
    ddb_block = read_long(ddb_ptr + 0x10)
    partition_offset = read_long(DRIVER_RAM + 0x14) & 0xFFFF if ddb_block > 0 else 0
    lba = ddb_block + partition_offset + 1
    byte_offset = lba * 512
    if byte_offset + 512 > len(disk_image):
        return False, ddb_block, lba, ddb_buffer
    skip_hook = True
    for i in range(0, 512, 2):
        w = read_word_le(disk_image, byte_offset + i)
        bus.write_word(ddb_buffer + i, w)
    raw_write_long(DDT_STATUS, 0)
    skip_hook = False
    return True, ddb_block, lba, ddb_buffer

def handle_pending_disk_io():
    global skip_hook
    ddb_list = find_ddb_for_ddt()
    for ddb_ptr in ddb_list:
        do_disk_read(ddb_ptr)
    if not ddb_list:
        skip_hook = True
        raw_write_long(DDT_STATUS, 0)
        skip_hook = False

def combined_write_hook(address, value):
    global mem_patch_done, write_043B_count, skip_hook, job_port_addr
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

    if addr in (0x041C, 0x041D, 0x041E, 0x041F) and job_port_addr != 0:
        new_jobcur = read_long(0x041C)
        if new_jobcur == 0 or new_jobcur == DDT_ADDR:
            skip_hook = True
            raw_write_long(0x041C, job_port_addr)
            skip_hook = False

    if 0x70BC <= addr <= 0x70BF and disk_io_enabled:
        val_after = read_long(0x70BC)
        if val_after == 0xFFFFFFFF:
            handle_pending_disk_io()

    if 0x70B0 <= addr <= 0x70B3 and disk_io_enabled:
        val_after = read_long(0x70B0)
        if val_after != 0:
            skip_hook = True
            raw_write_long(0x70B0, 0)
            skip_hook = False

bus._write_byte_physical = combined_write_hook
cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x006C0E and count > 100000:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"  Reached $006C0E at instr {count}")
job_port_addr = read_long(0x041C)
print(f"  JOBCUR = ${job_port_addr:08X}")

# ─── Step 3-5: Repair driver, inject subroutines, patch JSR ───
for off in range(CORRUPT_START, CORRUPT_END + 2, 2):
    ram_addr = DRIVER_RAM + off
    scz_w = read_word_le(scz_data, off)
    ram_w = bus.read_word(ram_addr)
    if scz_w != ram_w:
        bus.write_word(ram_addr, scz_w)

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

bus.write_word(TRAPDOOR_ADDR, 0x4E71)
bus.write_word(TRAPDOOR_ADDR + 2, 0x4E75)

DDT_XFR = DDT_ADDR + 0x08
bus.write_word(DDT_XFR, 0x0000)
bus.write_word(DDT_XFR + 2, 0x0000)
bus.write_word(DDT_XFR + 4, 0x0000)

raw_write_long(DUMMY_MODULE, 0)
raw_write_long(DUMMY_MODULE + 4, 0)
raw_write_long(DDT_ADDR + 0x0C, DUMMY_MODULE)

jsr_patches = [
    (0x0204, CANTMR_DST), (0x03B0, CLRCDB_DST), (0x040A, CLRCDB_DST),
    (0x043C, CLRCDB_DST), (0x046C, CLRCDB_DST), (0x0490, CLRCDB_DST),
    (0x057C, CLRCDB_DST), (0x0592, CLRCDB_DST),
]
for drv_off, target in jsr_patches:
    ram_addr = DRIVER_RAM + drv_off
    disp_addr = ram_addr + 2
    bus.write_word(disp_addr, (target - disp_addr) & 0xFFFF)

# ─── Step 6: System state setup ───
skip_hook = True
raw_write_long(DDT_STATUS, 0)
raw_write_long(0x0430, CORRECT_MEMBAS)
raw_write_long(0x0438, 0x3F0000)
raw_write_long(CORRECT_MEMBAS, 0)
raw_write_long(CORRECT_MEMBAS + 4, 0x3F0000 - CORRECT_MEMBAS)

# TCB setup
for i in range(0, 0x70, 2):
    raw_write_word(TCB_ADDR + i, 0)
raw_write_word(TCB_ADDR + 0x00, 0x0009)
raw_write_word(TCB_ADDR + 0x12, 0)
raw_write_long(TCB_ADDR + 0x1A, TCB_BUF_SIZE)
raw_write_long(TCB_ADDR + 0x44, TCB_BUF_ADDR)
raw_write_long(TCB_ADDR + 0x48, TCB_BUF_SIZE)
for i in range(0, TCB_BUF_SIZE, 2):
    raw_write_word(TCB_BUF_ADDR + i, 0)
raw_write_long(DDT_ADDR + 0x38, TCB_ADDR)
raw_write_word(DDT_ADDR + 0x20, 0)
skip_hook = False

disk_io_enabled = True
comint_stack_fixed = False
a03c_count = 0

print("Boot setup complete.\n")

# ─── Dump LINE-A dispatch table for $A01C ───
# The handler at $06F6 does:
#   $70A: MOVE.W (d8,PC,D7.W),-(SP)  ; push handler addr from table
# D7 = $001C for $A01C. Need to read the opcode at $70A to get d8.
print("=" * 60)
print("LINE-A DISPATCH TABLE ANALYSIS")
print("=" * 60)

# Read the extension word at $70C (after the opcode word at $70A)
op_70a = bus.read_word(0x70A)
ext_70c = bus.read_word(0x70C)
print(f"  Opcode at $070A: ${op_70a:04X}")
print(f"  Extension at $070C: ${ext_70c:04X}")
# Extension word: D/A(1) | reg(3) | W/L(1) | scale(2) | d8(8)
d8 = ext_70c & 0xFF
if d8 & 0x80:
    d8 = d8 - 256  # sign extend
print(f"  d8 displacement: {d8}")

# Table base = PC($70C) + d8
# For $A01C: table entry at PC + d8 + D7 = $70C + d8 + $001C
# But wait — the instruction is at $70A, and the 68000 PC for
# (d8,PC,Xn) is the address of the extension word = $70C.
table_entry_addr = 0x70C + d8 + 0x001C
handler_word = bus.read_word(table_entry_addr)
print(f"  Table entry for $A01C at ${table_entry_addr:06X}: ${handler_word:04X}")
print(f"  -> Handler at ${handler_word:06X}")

# Also dump nearby entries for context
print(f"\n  LINE-A dispatch table (around $A01C):")
for svca in range(0x14, 0x30, 2):
    entry_addr = 0x70C + d8 + svca
    handler = bus.read_word(entry_addr)
    opcode = 0xA000 + svca
    marker = " <<< $A01C (SCNMOD)" if svca == 0x1C else ""
    print(f"    ${opcode:04X}: handler=${handler:04X} (${handler:06X}){marker}")

# Dump the first 32 bytes of the SCNMOD handler
print(f"\n  SCNMOD handler disassembly at ${handler_word:06X}:")
for i in range(0, 48, 2):
    addr = handler_word + i
    w = bus.read_word(addr)
    # Annotate known patterns
    note = ""
    if w == 0xA052:
        note = " ; LINE-A $A052 (SRCH?)"
    elif w == 0xA03E:
        note = " ; LINE-A $A03E (IOWAIT)"
    elif w == 0xA008:
        note = " ; LINE-A $A008 (TTYLIN)"
    elif w == 0xA006:
        note = " ; LINE-A $A006 (TTYOUT)"
    elif w == 0xA00A:
        note = " ; LINE-A $A00A (TYPE)"
    elif w == 0xA00C:
        note = " ; LINE-A $A00C (CRLF)"
    elif (w & 0xF000) == 0xA000:
        note = f" ; LINE-A ${w:04X}"
    elif w == 0x4E75:
        note = " ; RTS"
    elif w == 0x4E73:
        note = " ; RTE"
    elif w == 0x4E71:
        note = " ; NOP"
    elif (w & 0xFF00) == 0x4E00:
        note = f" ; TRAP/LINK/UNLK/etc"
    elif w == 0x48E7:
        note = " ; MOVEM.L regs,-(SP)"
    elif w == 0x4CDF:
        note = " ; MOVEM.L (SP)+,regs"
    print(f"    ${addr:06X}: ${w:04X}{note}")

# ─── Dump code at $3A08 (SCNMOD redirect target) ───
print(f"\n{'='*60}")
print("CODE AT $3A08 (SCNMOD redirect target)")
print(f"{'='*60}")
for i in range(0, 96, 2):
    addr = 0x3A08 + i
    w = bus.read_word(addr)
    note = ""
    if (w & 0xF000) == 0xA000:
        note = f" ; LINE-A ${w:04X}"
    elif w == 0x4E75:
        note = " ; RTS"
    elif w == 0x4E73:
        note = " ; RTE"
    elif w == 0x48E7:
        note = " ; MOVEM.L regs,-(SP)"
    elif w == 0x4CDF:
        note = " ; MOVEM.L (SP)+,regs"
    print(f"  ${addr:06X}: ${w:04X}{note}")

# ─── Fix: Set JCB+$00 bit 7 so SCNMOD processes commands ───
print(f"\n{'='*60}")
print("FIXING JCB+$00 bit 7 for SCNMOD")
print(f"{'='*60}")
# Read current physical byte at $7038
old_byte = bus.read_byte(0x7038)
print(f"  Physical byte at $7038 (JCB+$00 low): ${old_byte:02X} (bit 7 = {(old_byte >> 7) & 1})")
# Set bit 7
new_byte = old_byte | 0x80
skip_hook = True
bus.write_byte(0x7038, new_byte)
skip_hook = False
verify = bus.read_byte(0x7038)
print(f"  After fix: ${verify:02X} (bit 7 = {(verify >> 7) & 1})")
print(f"  DDT+$00 word: ${bus.read_word(0x7038):04X}")

# ─── Step 7: Load AMOSL.INI and trace early init-file flow ───
print(f"\n{'='*60}")
print("TRACING EARLY AMOSL.INI FLOW THROUGH FIRST TRMDEF")
print(f"{'='*60}")

import sys as _sys2
_sys2.path.insert(0, "/Volumes/RAID0/repos/Alpha-Python/lib")
from Alpha_Disk_Lib import AlphaDisk as _AlphaDisk

_ini_lines = []
with _AlphaDisk(str(src_path)) as _disk:
    _dsk0 = _disk.get_logical_device(0)
    _ini_data = _dsk0.read_file_contents((1, 4), "AMOSL", "INI")
    if _ini_data:
        _text = ""
        for _b in _ini_data:
            if _b == 0:
                break
            _text += chr(_b)
        for _line in _text.split("\n"):
            _line = _line.rstrip("\r")
            if _line:
                _ini_lines.append(_line)

_first_trmdef_idx = next((i for i, line in enumerate(_ini_lines) if line.startswith("TRMDEF ")), None)
_second_trmdef_idx = next((i for i, line in enumerate(_ini_lines) if i > (_first_trmdef_idx or -1) and line.startswith("TRMDEF ")), None)
_ver_idx = next((i for i, line in enumerate(_ini_lines) if line == "VER"), None)
print(f"Loaded {len(_ini_lines)} AMOSL.INI lines")
for i, line in enumerate(_ini_lines[:12]):
    marker = " <<< first TRMDEF" if i == _first_trmdef_idx else ""
    print(f"  [{i:02d}] {line}{marker}")
if len(_ini_lines) > 12:
    print(f"  ... ({len(_ini_lines) - 12} more)")

_ini_data_pending = False
_tracing_scnmod = False
_scnmod_handler = handler_word
_scnmod_trace_count = 0
_scnmod_max_trace = 100_000
_scnmod_entry_count = 0
_a006_output = bytearray()
_ini_pos = 0
_line_injected_count = 0
_current_line_idx = None
_current_line_text = None
_last_completed_line_idx = None
_completed_lines = []
_scnmod_returned = False
_saw_srch = False
_saw_errmsg = False
_saw_lowmem = False
_stop_reason = None
_a03e_wait_count = 0
_seen_trace_keys = set()
_ttyin_chars = bytearray()
_ttyin_line_buf = bytearray()
_banner_detected = False
_banner_chars = bytearray()
_stop_after_idx = _ver_idx if _ver_idx is not None else ((_first_trmdef_idx or 0) + 3)

extra_count = 0
max_extra = 5_000_000
disk_a03c_just_skipped = False

def log_trace_event(label, pc, op, force=False):
    key = (label, pc, op, cpu.d[6] & 0xFFFF, cpu.d[7] & 0xFFFF, cpu.a[5] & 0xFFFFFF)
    if not force and key in _seen_trace_keys:
        return
    _seen_trace_keys.add(key)
    regs = (f"D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X} "
            f"A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
            f"A2=${cpu.a[2]&0xFFFFFF:06X} A5=${cpu.a[5]&0xFFFFFF:06X} "
            f"A6=${cpu.a[6]&0xFFFFFF:06X} SP=${cpu.a[7]&0xFFFFFF:06X}")
    print(f"  [{_scnmod_trace_count:5d}] {label}: PC=${pc:06X} OP=${op:04X} {regs}")

def halt_trace(reason):
    global _stop_reason
    if _stop_reason is None:
        _stop_reason = reason
        print(f"\n  >> {reason}. Halting.")
    cpu.halted = True

def dump_bytes(addr, limit=24):
    if not (0 < addr < 0x400000):
        return "<invalid>"
    data = bytearray()
    for i in range(limit):
        b = bus.read_byte(addr + i)
        if b == 0:
            break
        data.append(b)
    return format_bytes(data)

def inject_tty_line(line):
    global skip_hook, _ini_data_pending
    cmd = line.encode("ascii", errors="replace") + b"\x0A"
    skip_hook = True
    for ci, ch in enumerate(cmd):
        bus.write_byte(TCB_BUF_ADDR + ci, ch)
    raw_write_long(TCB_ADDR + 0x1E, TCB_BUF_ADDR)
    raw_write_word(TCB_ADDR + 0x12, len(cmd))
    raw_write_word(TCB_ADDR + 0x00, 0x0000)
    skip_hook = False
    _ini_data_pending = True

while not cpu.halted and extra_count < max_extra:
    pc = cpu.pc

    try:
        op = bus.read_word(pc)
    except:
        op = 0

    # === COMINT stack fix ===
    if pc == 0x00682E:
        old_sp = cpu.a[7] & 0xFFFFFF
        if old_sp < 0x8000:
            new_sp = INIT_JOB_SP
            skip_hook = True
            for i in range(0, 6, 2):
                w = bus.read_word(old_sp + i)
                bus.write_word(new_sp + i, w)
            skip_hook = False
            cpu.a[7] = new_sp & 0xFFFFFFFF
            skip_hook = True
            raw_write_long(DDT_ADDR + 0x80, new_sp)
            skip_hook = False
            if not comint_stack_fixed:
                comint_stack_fixed = True
                print(f"  COMINT STACK FIX: SP ${old_sp:06X} -> ${new_sp:06X}")
        skip_hook = True
        raw_write_long(DDT_ADDR + 0x0C, DUMMY_MODULE)
        skip_hook = False

    # === $A03C disk I/O bypass ===
    if op == 0xA03C:
        a0 = cpu.a[0] & 0xFFFFFF
        is_ddt = (a0 == DDT_ADDR)
        is_disk_ddb = False
        if not is_ddt and 0 < a0 < 0x400000:
            try:
                is_disk_ddb = (read_long(a0 + 0x08) & 0xFFFFFF) == DDT_ADDR
            except:
                pass
        if is_ddt or is_disk_ddb:
            a03c_count += 1
            if is_disk_ddb:
                do_disk_read(a0)
            else:
                ddb_list = find_ddb_for_ddt()
                for ddb_ptr in ddb_list:
                    do_disk_read(ddb_ptr)
                if not ddb_list:
                    skip_hook = True
                    raw_write_long(DDT_STATUS, 0)
                    skip_hook = False
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            disk_a03c_just_skipped = True
            extra_count += 1
            continue

    # === $A03E handling ===
    if op == 0xA03E and _ini_data_pending:
        skip_hook = True
        raw_write_word(TCB_ADDR + 0x00, 0x0009)
        skip_hook = False
        _ini_data_pending = False
        cpu.pc = (pc + 2) & 0xFFFFFFFF
        extra_count += 1
        continue

    if op == 0xA03E and disk_a03c_just_skipped:
        cpu.pc = (pc + 2) & 0xFFFFFFFF
        extra_count += 1
        continue

    if disk_a03c_just_skipped and op != 0xA03E:
        disk_a03c_just_skipped = False

    # === $A064 bypass ===
    if op == 0xA064:
        cpu.sr = (cpu.sr & ~0x04) | 0x04
        cpu.pc = (pc + 2) & 0xFFFFFFFF
        extra_count += 1
        continue

    # === $A006 output — capture ===
    if op == 0xA006 and pc < 0x8000:
        d1 = cpu.d[1] & 0x7F
        _a006_output.append(d1)
        if d1 >= 0x20 or d1 in (0x0A, 0x0D):
            _banner_chars.append(d1)
        if d1 not in (0x2E, 0x0A, 0x0D) and d1 >= 0x20:
            _banner_detected = True
            print(f"\n  >> TERMINAL TEXT DETECTED: char=${d1:02X} ('{chr(d1)}') at PC=${pc:06X}")
            halt_trace("Observed terminal-visible text after TRMDEF")
        cpu.pc = (pc + 2) & 0xFFFFFFFF
        extra_count += 1
        continue

    # === $A008 TTYLIN — inject next AMOSL.INI line ===
    if op == 0xA008 and pc < 0x8000:
        if _stop_after_idx is not None and _ini_pos > _stop_after_idx:
            halt_trace(f"Processed AMOSL.INI through line {_stop_after_idx:02d} without terminal text")
            extra_count += 1
            continue
        if _ini_pos < len(_ini_lines):
            _current_line_idx = _ini_pos
            _current_line_text = _ini_lines[_ini_pos]
            _ini_pos += 1
            _line_injected_count += 1
            inject_tty_line(_current_line_text)
            print(f"\n  >> INI [{_current_line_idx:02d}/{len(_ini_lines)-1:02d}] "
                  f"'{_current_line_text}' at [{extra_count}] PC=${pc:06X}")

    # === $A072 TTYIN — serve from buffer ===
    if op == 0xA072 and pc < 0x8000:
        tcb_count = bus.read_word(TCB_ADDR + 0x12)
        if tcb_count > 0:
            rd_ptr = read_long(TCB_ADDR + 0x1E)
            ch = bus.read_byte(rd_ptr) if rd_ptr < 0x400000 else 0
            if _line_injected_count > 0 and len(_ttyin_chars) < 4096:
                _ttyin_chars.append(ch)
                _ttyin_line_buf.append(ch)
                printable = chr(ch) if 0x20 <= ch < 0x7F else f"<{ch:02X}>"
                print(f"  >> TTYIN[{len(_ttyin_chars):03d}] {printable} "
                      f"from ${rd_ptr:06X} remaining={tcb_count} line={_current_line_idx}")
                if ch == 0x0A and _current_line_idx is not None:
                    consumed = format_bytes(_ttyin_line_buf)
                    _completed_lines.append((_current_line_idx, consumed))
                    _last_completed_line_idx = _current_line_idx
                    print(f"  >> LINE COMPLETE [{_current_line_idx:02d}]: '{consumed}'")
                    _ttyin_line_buf = bytearray()
            skip_hook = True
            raw_write_long(TCB_ADDR + 0x1E, rd_ptr + 1)
            raw_write_word(TCB_ADDR + 0x12, tcb_count - 1)
            skip_hook = False
            cpu.d[1] = (cpu.d[1] & 0xFFFFFF00) | ch
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            extra_count += 1
            continue

    # === ACIA bypasses ===
    if pc == 0x006C3E:
        cpu.d[1] = 0x02
        cpu.d[2] = 0x28
        acia.write(0xFFFFC8, 1, 0x03)
        acia.write(0xFFFFC8, 1, 0x15)
        acia._echo_enabled = [False, False, False]
        cpu.pc = 0x006C5E
        extra_count += 1
        continue
    if pc == 0x006D80:
        a0 = cpu.a[0] & 0xFFFFFF
        skip_hook = True
        bus.write_byte(a0 + 0x0636, 0x02)
        skip_hook = False
        cpu.sr = (cpu.sr & 0xFF00) | 0x04
        sp = cpu.a[7] & 0xFFFFFF
        ret = read_long(sp)
        cpu.a[7] = (sp + 4) & 0xFFFFFFFF
        cpu.pc = ret & 0xFFFFFF
        extra_count += 1
        continue
    if pc == 0x006BDE:
        if _tracing_scnmod:
            log_trace_event("LOWMEM helper $006BDE", pc, op)
        cpu.d[7] = 0x16
        cpu.pc = 0x006BEC
        extra_count += 1
        continue
    if pc == 0x006D9C:
        if _tracing_scnmod:
            log_trace_event("LOWMEM helper $006D9C", pc, op)
        cpu.d[7] = 0x0E
        cpu.pc = 0x006DAA
        extra_count += 1
        continue
    if pc == 0x006B68:
        if _tracing_scnmod:
            _saw_lowmem = True
            log_trace_event("LOWMEM path $006B68", pc, op, force=True)
            halt_trace("Reached low-memory monitor path")
        cpu.pc = 0x006B70
        extra_count += 1
        continue
    if pc == 0x002AB0:
        cpu.pc = 0x002AB2
        extra_count += 1
        continue

    # === SCNMOD TRACE ===
    # Ensure JCB+$00 bit 7 is set right before SCNMOD executes
    if op == 0xA01C and pc == 0x3932 and _line_injected_count > 0:
        old_b = bus.read_byte(0x7038)
        if not (old_b & 0x80):
            skip_hook = True
            bus.write_byte(0x7038, old_b | 0x80)
            skip_hook = False
            print(f"  >> Fixed JCB+$00 bit 7: ${old_b:02X} -> ${old_b | 0x80:02X}")

    # When we see the $A01C opcode at $3932 (COMINT's SCNMOD call), start tracing
    if op == 0xA01C and pc == 0x3932 and _line_injected_count > 0:
        _tracing_scnmod = True
        _scnmod_trace_count = 0
        _scnmod_entry_count += 1
        _scnmod_returned = False
        _a03e_wait_count = 0
        print(f"\n{'='*60}")
        print(f"SCNMOD ENTRY #{_scnmod_entry_count} at PC=$3932 [{extra_count}]")
        if _current_line_idx is not None:
            print(f"  INI line [{_current_line_idx:02d}]: {_current_line_text}")
        print(f"  A0=${cpu.a[0]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X}"
              f" D0=${cpu.d[0]:08X} D6=${cpu.d[6]:08X}")
        print(f"  SP=${cpu.a[7]&0xFFFFFF:06X} SR=${cpu.sr:04X}")
        a6 = cpu.a[6] & 0xFFFFFF
        if 0 < a6 < 0x400000:
            cmd_bytes = bytearray()
            for j in range(32):
                b = bus.read_byte(a6 + j)
                cmd_bytes.append(b)
                if b == 0:
                    break
            cmd_str = format_bytes(cmd_bytes)
            print(f"  Command at (A6)=${a6:06X}: '{cmd_str}'")
        print(f"  JOBCUR=${read_long(0x041C):08X} SYSBAS=${read_long(0x0414):08X}")
        print(f"  JCB+$0C=${read_long(DDT_ADDR + 0x0C):08X} (module chain)")
        print(f"{'='*60}")

    # Event-oriented trace after SCNMOD starts.
    if _tracing_scnmod and _scnmod_trace_count < _scnmod_max_trace:
        _scnmod_trace_count += 1

        if pc == 0x06F6:
            log_trace_event("LINE-A handler entry", pc, op)
        elif pc == 0x70A:
            log_trace_event(f"LINE-A dispatch D7=${cpu.d[7]:08X}", pc, op)
        elif pc == _scnmod_handler:
            log_trace_event("SCNMOD handler start", pc, op)
        elif pc == 0x1C30:
            _saw_srch = True
            log_trace_event("SRCH entry", pc, op, force=True)
        elif pc == 0x1C58:
            log_trace_event(f"SRCH reads JOBCUR=${read_long(0x041C):08X}", pc, op)
        elif pc == 0x1C6E:
            log_trace_event("SRCH module loop", pc, op)
        elif pc == 0x1C60:
            z = "Z=1" if (cpu.sr & 0x04) else "Z=0"
            log_trace_event(f"SRCH JCB+$0C test {z}", pc, op)
        elif pc == 0x682E:
            log_trace_event("COMINT entry", pc, op)
        elif pc == 0x68AA:
            _saw_errmsg = True
            log_trace_event(f"COMINT ERRMSG D0=${cpu.d[0]:08X}", pc, op, force=True)
        elif pc == 0x3934 and _scnmod_trace_count > 10 and not _scnmod_returned:
            _scnmod_returned = True
            log_trace_event("SCNMOD returned to COMINT", pc, op, force=True)
        elif op == 0xA008:
            log_trace_event("TTYLIN request", pc, op, force=True)
            print(f"      SYSTEM=${read_long(0x0400):08X} SYSBAS=${read_long(0x0414):08X} "
                  f"JOBCUR=${read_long(0x041C):08X} JCB+$0C=${read_long(DDT_ADDR + 0x0C):08X}")
            print(f"      A6 text: '{dump_bytes(cpu.a[6] & 0xFFFFFF)}'")
            print(f"      A2 text: '{dump_bytes(cpu.a[2] & 0xFFFFFF)}'")
            print(f"      TCB text: '{dump_bytes(read_long(TCB_ADDR + 0x1E))}'")
        elif op == 0xA052:
            log_trace_event("LINE-A $A052", pc, op)
        elif op == 0xA03E and (cpu.d[6] & 0xFFFF) == 2:
            _a03e_wait_count += 1
            if _a03e_wait_count <= 5 or _a03e_wait_count in (10, 20, 50, 100):
                log_trace_event(f"A03E terminal wait #{_a03e_wait_count}", pc, op, force=True)

        if _scnmod_trace_count >= _scnmod_max_trace:
            halt_trace(f"Trace limit {_scnmod_max_trace} reached")

    # === Halt after processing ===
    if _tracing_scnmod and _scnmod_returned and _a03e_wait_count >= 100 and not (_saw_srch or _saw_errmsg or _saw_lowmem):
        halt_trace("Returned to COMINT but only reached repeated terminal waits")

    cpu.step()
    bus.tick(1)
    extra_count += 1

if extra_count >= max_extra:
    print(f"\n  >> Instruction limit ({max_extra}) reached")

print(f"\nSummary:")
print(f"  saw_srch={_saw_srch} saw_errmsg={_saw_errmsg} saw_lowmem={_saw_lowmem} returned_to_comint={_scnmod_returned}")
print(f"  a03e_terminal_waits={_a03e_wait_count}")
print(f"  stop_reason={_stop_reason or 'normal halt'}")
print(f"  ini_lines_loaded={len(_ini_lines)} injected={_line_injected_count} last_completed={_last_completed_line_idx}")
print(f"  first_trmdef_idx={_first_trmdef_idx} second_trmdef_idx={_second_trmdef_idx} ver_idx={_ver_idx}")
print(f"  banner_detected={_banner_detected}")
print(f"  ttyin_chars={len(_ttyin_chars)}")
print(f"\nOutput captured ({len(_a006_output)} chars):")
print(f"  '{format_bytes(_a006_output)}'")
if _banner_chars:
    print(f"\nPrintable terminal output ({len(_banner_chars)} chars):")
    print(f"  '{format_bytes(_banner_chars)}'")
print(f"\nTTYIN consumed ({len(_ttyin_chars)} chars):")
print(f"  '{format_bytes(_ttyin_chars)}'")
if _completed_lines:
    print(f"\nCompleted AMOSL.INI lines:")
    for idx, consumed in _completed_lines:
        print(f"  [{idx:02d}] '{consumed}'")
print(f"\nTotal instructions: {extra_count}")
