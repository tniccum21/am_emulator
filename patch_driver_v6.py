#!/usr/bin/env python3
"""Patch SCZ.DVR into AMOSL.MON v6 — complete boot with ACIA bypasses.

Combines:
  1. Disk: Copy first 1432 bytes of SCZ.DVR to $7AC2
  2. Boot: Run to $006C0E (port init, BEFORE scheduler — $041C still valid)
  3. RAM repair: Fix 10 bytes at +$058E-$0596 corrupted by boot init
  4. RAM inject: CANTMR (80 bytes) -> $9000, CLRCDB (18 bytes) -> $9050
  5. RAM patch: All 8 JSR(PC) displacements
  6. MEMBAS: Patch to $8800 (correct memory base)
  7. ACIA bypasses: Terminal detect, handshake, status checks, $A00A intercept
  8. JOBCUR protection: Prevent OS from clearing $041C
  9. Continue: Run OS init with driver active
"""
import sys
import shutil
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

DRIVER_RAM = 0x7AC2
MAX_COPY = 1432
AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510

# Relocation targets in high RAM (beyond AMOSL.MON's $8975)
CANTMR_DST = 0x9000   # 80 bytes — timer cancellation
CLRCDB_DST = 0x9050   # 18 bytes — CDB buffer clear

# Corruption range: boot init overwrites +$058E to +$0596
CORRUPT_START = 0x058E
CORRUPT_END = 0x0596   # inclusive word

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

# ─── Step 1: Create disk patch (1432 bytes, no JSR changes) ───
print("Step 1: Creating disk patch...")
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

# ─── Step 2: Boot to $006C0E (port init, before scheduler) ───
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

# Helper for raw writes
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

# Install MEMBAS patching hook (from debug_banner_boot.py)
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
            print(f"    MEMBAS hook fired: set ${CORRECT_MEMBAS:06X}")

bus._write_byte_physical = patching_write

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

# Verify driver in RAM
w2 = bus.read_word(DRIVER_RAM + 2)
print(f"  Driver at ${DRIVER_RAM:06X}+2: ${w2:04X} ({'present' if w2 != 0 else 'MISSING'})")

# ─── Step 3: Repair corrupted bytes ───
print(f"\nStep 3: Repairing boot-corrupted bytes at +${CORRUPT_START:04X}-+${CORRUPT_END:04X}...")

for off in range(CORRUPT_START, CORRUPT_END + 2, 2):
    ram_addr = DRIVER_RAM + off
    scz_w = read_word_le(scz_data, off)
    ram_w = bus.read_word(ram_addr)
    if scz_w != ram_w:
        bus.write_word(ram_addr, scz_w)
        verify = bus.read_word(ram_addr)
        print(f"  ${ram_addr:06X} (+${off:04X}): ${ram_w:04X} -> ${scz_w:04X} ({'ok' if verify == scz_w else 'FAIL'})")

# ─── Step 4: Inject overflow subroutines into RAM ───
print(f"\nStep 4: Injecting CANTMR and CLRCDB into RAM...")

# CANTMR: SCZ.DVR +$05C8 to +$0617 (80 bytes) → RAM $9000
CANTMR_SRC = 0x05C8
CANTMR_SIZE = 0x0616 - 0x05C8 + 2  # 80 bytes
cantmr_data = scz_data[CANTMR_SRC:CANTMR_SRC + CANTMR_SIZE]

for i in range(0, CANTMR_SIZE, 2):
    w = (cantmr_data[i+1] << 8) | cantmr_data[i]
    bus.write_word(CANTMR_DST + i, w)

w_verify = bus.read_word(CANTMR_DST)
print(f"  CANTMR: {CANTMR_SIZE} bytes at ${CANTMR_DST:06X}, first word=${w_verify:04X}")

# CLRCDB: SCZ.DVR +$0618 to +$0629 (18 bytes) → RAM $9050
CLRCDB_SRC = 0x0618
CLRCDB_SIZE = 0x0628 - 0x0618 + 2  # 18 bytes
clrcdb_data = scz_data[CLRCDB_SRC:CLRCDB_SRC + CLRCDB_SIZE]

for i in range(0, CLRCDB_SIZE, 2):
    w = (clrcdb_data[i+1] << 8) | clrcdb_data[i]
    bus.write_word(CLRCDB_DST + i, w)

w_verify = bus.read_word(CLRCDB_DST)
print(f"  CLRCDB: {CLRCDB_SIZE} bytes at ${CLRCDB_DST:06X}, first word=${w_verify:04X}")

# ─── Step 5: Patch JSR(PC) displacements in RAM ───
print(f"\nStep 5: Patching JSR(PC) displacements in RAM...")

jsr_patches = [
    # (driver_offset, target_ram_addr, description)
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
        print(f"  ERROR: Expected $4EBA at ${ram_addr:06X}, got ${opcode:04X} — {desc}")
        continue

    old_disp = bus.read_word(disp_addr)
    new_disp = (target - disp_addr) & 0xFFFF
    bus.write_word(disp_addr, new_disp)

    verify = bus.read_word(disp_addr)
    print(f"  ${ram_addr:06X} (+${drv_off:04X}): ${old_disp:04X} -> ${new_disp:04X} -> ${target:06X} {desc} ({'ok' if verify == new_disp else 'FAIL'})")

# ─── Step 6: Set output enable flag and install JOBCUR protection ───
print(f"\nStep 6: Setting output flag and JOBCUR protection...")

skip_hook = True
raw_write_long(0x043C, 0x00000001)
skip_hook = False
print(f"  $043C = ${read_long(0x043C):08X}")

# Install JOBCUR protection hook (prevents OS from clearing $041C)
job_port_addr = read_long(0x041C)
print(f"  JOBCUR port at $041C = ${job_port_addr:08X}")

def jobcur_patching_write(address, value):
    global skip_hook
    addr = address & 0xFFFFFF
    if skip_hook:
        orig_write_byte(address, value)
        return
    if addr in (0x041C, 0x041D, 0x041E, 0x041F):
        orig_write_byte(address, value)
        new_jobcur = read_long(0x041C)
        if new_jobcur == 0 and job_port_addr != 0:
            skip_hook = True
            raw_write_long(0x041C, job_port_addr)
            skip_hook = False
        return
    orig_write_byte(address, value)

bus._write_byte_physical = jobcur_patching_write

# ─── Step 7: Continue with ACIA bypasses ───
print(f"\n{'='*60}")
print("Step 7: RUNNING WITH ACIA BYPASSES")
print(f"{'='*60}")

# Track bypasses
bypass_counts = {
    0x006C3E: 0,  # Terminal detect
    0x006D80: 0,  # Hardware handshake
    0x006BDE: 0,  # Status check ($16)
    0x006D9C: 0,  # Status check ($0E)
    0x006B68: 0,  # ACIA control check
}
a00a_intercepts = 0

DRIVER_END = DRIVER_RAM + MAX_COPY
driver_entries = 0
last_driver_pc = None

extra_count = 0
max_extra = 30_000_000

while not cpu.halted and extra_count < max_extra:
    pc = cpu.pc

    # === ACIA Bypasses ===

    # Terminal detect ($006C3E) — skip polling, set terminal type
    if pc == 0x006C3E:
        bypass_counts[0x006C3E] += 1
        cpu.d[1] = 0x02   # Terminal type
        cpu.d[2] = 0x28   # Baud rate divisor
        acia.write(0xFFFFC8, 1, 0x03)  # Master reset
        acia.write(0xFFFFC8, 1, 0x15)  # 8N1, div16, RTS low
        acia._echo_enabled = [False, False, False]
        cpu.pc = 0x006C5E
        if bypass_counts[0x006C3E] <= 3:
            print(f"  BYPASS terminal detect #{bypass_counts[0x006C3E]} at instr {count + extra_count}")
        extra_count += 1
        continue

    # Hardware handshake ($006D80) — skip ACIA polling, return
    if pc == 0x006D80:
        bypass_counts[0x006D80] += 1
        a0 = cpu.a[0] & 0xFFFFFF
        cpu.a[6] = (a0 + 0x0634) & 0xFFFFFFFF
        for off in range(10):
            bus.write_byte(a0 + 0x062A + off, 0)
        bus.write_byte(a0 + 0x0634, 0x03)
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
        if bypass_counts[0x006D80] <= 3:
            print(f"  BYPASS handshake #{bypass_counts[0x006D80]} -> ${ret_addr:06X}")
        extra_count += 1
        continue

    # Status check at $006BDE — wants $16
    if pc == 0x006BDE:
        bypass_counts[0x006BDE] += 1
        cpu.d[7] = 0x16
        cpu.pc = 0x006BEC
        extra_count += 1
        continue

    # Status check at $006D9C — wants $0E
    if pc == 0x006D9C:
        bypass_counts[0x006D9C] += 1
        cpu.d[7] = 0x0E
        cpu.pc = 0x006DAA
        extra_count += 1
        continue

    # ACIA control check ($006B68)
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
        acia.write(0xFFFFC9, 1, d1)
        banner_chars.append(d1)
        cpu.pc = 0x002AB2
        extra_count += 1
        continue

    # === Driver tracking ===
    if DRIVER_RAM <= pc < DRIVER_END or pc == CANTMR_DST or pc == CLRCDB_DST:
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

    if extra_count % 5_000_000 == 0:
        print(f"\n  Progress: {count + extra_count} instrs, PC=${cpu.pc:06X}, "
              f"driver={driver_entries}, banner={len(banner_chars)} chars")

# ─── Results ───
print(f"\n{'='*60}")
print("RESULTS")
print(f"{'='*60}")
print(f"  Total: {count + extra_count} instructions")
print(f"  Driver entries: {driver_entries}")
print(f"  $A00A intercepts: {a00a_intercepts}")
print(f"  Bypasses: C3E={bypass_counts[0x006C3E]} D80={bypass_counts[0x006D80]} "
      f"BDE={bypass_counts[0x006BDE]} D9C={bypass_counts[0x006D9C]} B68={bypass_counts[0x006B68]}")

if cpu.halted:
    print(f"  CPU HALTED at PC=${cpu.pc:06X}")
else:
    print(f"  Final PC=${cpu.pc:06X}")

if banner_chars:
    text = bytes(banner_chars).decode('ascii', errors='replace')
    printable = ''.join(c if (0x20 <= ord(c) < 0x7F or c in '\r\n') else '.' for c in text)
    print(f"\n  Banner text ({len(banner_chars)} chars):")
    for line in printable.split('\n')[:30]:
        if line.strip():
            print(f"    {line.rstrip()}")

print(f"\nDone!")
