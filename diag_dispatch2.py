#!/usr/bin/env python3
"""Trace I/O dispatch — focus on ($0514) jump table and DD.XFR call site at $5150.

Key questions:
1. What's the jump table at ($0514)?
2. Code around $5150 — how does A1 get set to driver base?
3. What code path leads from $A03C queue → DD.XFR call?
4. Why doesn't the driver get called in v6?
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

# ─── Disk patch + boot (same as v6) ───
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

print(f"Boot: PC=${cpu.pc:06X} at instr {count}")

# ─── Examine ($0514) function table ───
print(f"\n{'='*60}")
print("($0514) Function Table")
print(f"{'='*60}")

ptr_0514 = read_long(0x0514)
print(f"  ($0514).L = ${ptr_0514:08X}")

if ptr_0514 != 0 and ptr_0514 < 0x400000:
    print(f"\n  Jump table at ${ptr_0514:06X}:")
    for off in range(0, 0x20, 2):
        addr = ptr_0514 + off
        w = bus.read_word(addr)
        print(f"    +${off:02X} (${addr:06X}): ${w:04X}")

# ─── Examine code around $5150 — DD.XFR call site ───
print(f"\n{'='*60}")
print("DD.XFR Call Site Analysis ($5100-$5180)")
print(f"{'='*60}")

for addr in range(0x50D0, 0x5180, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── Examine code around $5104 — DD.MNT call site ───
print(f"\n{'='*60}")
print("DD.MNT Context ($50F0-$5110)")
print(f"{'='*60}")

for addr in range(0x50F0, 0x5120, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── $A03C handler detailed flow analysis ───
print(f"\n{'='*60}")
print("$A03C Handler at $12F4 — Full Disassembly")
print(f"{'='*60}")

for addr in range(0x12F4, 0x13A0, 2):
    w = bus.read_word(addr)
    print(f"  ${addr:06X}: ${w:04X}")

# ─── Check DDT status and queue fields ───
print(f"\n{'='*60}")
print("DDT at $7038 — Status and Queue Fields")
print(f"{'='*60}")

ddt = 0x7038
for off, name in [(0x00, "DD.STS"), (0x02, "DD.FLG"), (0x06, "DD.DRV"),
                   (0x14, "DD.INT"), (0x78, "QueueLink"), (0x7C, "SavedA6"),
                   (0x80, "SavedSP"), (0x84, "PendingFlg"), (0x8E, "field_8E"),
                   (0x92, "field_92")]:
    addr = ddt + off
    l = read_long(addr)
    w = bus.read_word(addr)
    print(f"  +${off:02X} ({name}): W=${w:04X} L=${l:08X}")

# ─── Check ($0450) init flag ───
print(f"\n  ($0450).L = ${read_long(0x0450):08X}")
print(f"  ($0403).B = ${bus.read_byte(0x0403):02X}")
print(f"  ($0488).B = ${bus.read_byte(0x0488):02X}")

# ─── Now do a traced run with ACIA bypasses to see the dispatch path ───
print(f"\n{'='*60}")
print("TRACED RUN — Track $A03C/$A03E and dispatch calls")
print(f"{'='*60}")

# Install all patches first
CORRUPT_START = 0x058E
CORRUPT_END = 0x0596
for off in range(CORRUPT_START, CORRUPT_END + 2, 2):
    ram_addr = DRIVER_RAM + off
    scz_w = read_word_le(scz_data, off)
    bus.write_word(ram_addr, scz_w)

# CANTMR injection
CANTMR_SRC = 0x05C8
CANTMR_SIZE = 80
cantmr_data = scz_data[CANTMR_SRC:CANTMR_SRC + CANTMR_SIZE]
for i in range(0, CANTMR_SIZE, 2):
    w = (cantmr_data[i+1] << 8) | cantmr_data[i]
    bus.write_word(CANTMR_DST + i, w)

# CLRCDB injection
CLRCDB_SRC = 0x0618
CLRCDB_SIZE = 18
clrcdb_data = scz_data[CLRCDB_SRC:CLRCDB_SRC + CLRCDB_SIZE]
for i in range(0, CLRCDB_SIZE, 2):
    w = (clrcdb_data[i+1] << 8) | clrcdb_data[i]
    bus.write_word(CLRCDB_DST + i, w)

# JSR patches
jsr_patches = [
    (0x0204, CANTMR_DST), (0x03B0, CLRCDB_DST), (0x040A, CLRCDB_DST),
    (0x043C, CLRCDB_DST), (0x046C, CLRCDB_DST), (0x0490, CLRCDB_DST),
    (0x057C, CLRCDB_DST), (0x0592, CLRCDB_DST),
]
for drv_off, target in jsr_patches:
    ram_addr = DRIVER_RAM + drv_off
    disp_addr = ram_addr + 2
    opcode = bus.read_word(ram_addr)
    if opcode == 0x4EBA:
        new_disp = (target - disp_addr) & 0xFFFF
        bus.write_word(disp_addr, new_disp)

# System patches
skip_hook = True
raw_write_long(0x043C, 0x00000001)
skip_hook = False

# JOBCUR protection
job_port_addr = read_long(0x041C)
jobcur_restore_count = 0

def jobcur_patching_write(address, value):
    global skip_hook, jobcur_restore_count
    addr = address & 0xFFFFFF
    if skip_hook:
        orig_write_byte(address, value)
        return
    if addr in (0x041C, 0x041D, 0x041E, 0x041F):
        orig_write_byte(address, value)
        new_jobcur = read_long(0x041C)
        if new_jobcur == 0 and job_port_addr != 0:
            jobcur_restore_count += 1
            skip_hook = True
            raw_write_long(0x041C, job_port_addr)
            skip_hook = False
        return
    orig_write_byte(address, value)

bus._write_byte_physical = jobcur_patching_write

# Track key events
DRIVER_END = DRIVER_RAM + MAX_COPY
driver_entries = 0
a03c_calls = 0
a03e_calls = 0
dispatch_129c_calls = 0
fn_0514_calls = {}  # offset -> count
site_5150_calls = 0
site_5104_calls = 0

# Track the I/O queue chain
def show_io_queue():
    """Show the JOBCUR linked list"""
    ptr = read_long(0x041C)
    chain = []
    seen = set()
    while ptr != 0 and ptr not in seen and len(chain) < 10:
        seen.add(ptr)
        chain.append(ptr)
        try:
            ptr = read_long(ptr + 0x78)
        except:
            break
    return chain

extra_count = 0
max_extra = 10_000_000

while not cpu.halted and extra_count < max_extra:
    pc = cpu.pc

    # ACIA bypasses
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
        extra_count += 1
        continue
    if pc == 0x006BDE:
        cpu.d[7] = 0x16
        cpu.pc = 0x006BEC
        extra_count += 1
        continue
    if pc == 0x006D9C:
        cpu.d[7] = 0x0E
        cpu.pc = 0x006DAA
        extra_count += 1
        continue
    if pc == 0x006B68:
        if (acia._control[0] & 0x03) != 0x03:
            cpu.pc = 0x006B70
            extra_count += 1
            continue
    if pc == 0x002AB0:
        d1 = cpu.d[1] & 0x7F
        acia.write(0xFFFFC9, 1, d1)
        cpu.pc = 0x002AB2
        extra_count += 1
        continue

    # Track $A03C calls
    if pc == 0x12F4:
        a03c_calls += 1
        if a03c_calls <= 5:
            a0_val = cpu.a[0] & 0xFFFFFF
            d6_val = cpu.d[6]
            ddt_sts = bus.read_word(a0_val) if a0_val < 0x400000 else 0
            ddt_84 = read_long(a0_val + 0x84) if a0_val + 0x84 < 0x400000 else 0
            print(f"\n  $A03C #{a03c_calls}: A0=${a0_val:06X} D6=${d6_val:08X}")
            print(f"    DDT.STS=${ddt_sts:04X} DDT+$84=${ddt_84:08X}")
            print(f"    JOBCUR=${read_long(0x041C):08X} queue={show_io_queue()}")
            # Show the caller (return address on stack)
            sp = cpu.a[7] & 0xFFFFFF
            ret_word = bus.read_word(sp + 2)  # exception frame has SR then PC
            ret_hi = bus.read_word(sp + 2)
            ret_lo = bus.read_word(sp + 4)
            ret_pc = (ret_hi << 16) | ret_lo
            print(f"    Caller PC=${ret_pc:06X}")

    # Track scheduler dispatch at $129C
    if pc == 0x129C:
        dispatch_129c_calls += 1
        if dispatch_129c_calls <= 10:
            jobcur_val = read_long(0x041C)
            init_flag = read_long(0x0450)
            sts = bus.read_word(jobcur_val) if jobcur_val > 0 and jobcur_val < 0x400000 else 0
            print(f"\n  DISPATCH #{dispatch_129c_calls}: JOBCUR=${jobcur_val:08X} "
                  f"($0450)=${init_flag:08X} STS=${sts:04X}")
            if jobcur_val > 0 and jobcur_val < 0x400000:
                sp_val = read_long(jobcur_val + 0x80)
                a6_val = read_long(jobcur_val + 0x7C)
                print(f"    JCB+$7C(A6)=${a6_val:08X} JCB+$80(SP)=${sp_val:08X}")
                # Check if bit 13 ($2000) is set
                bit13 = (sts >> 13) & 1
                print(f"    Bit13(dispatchable)={bit13}")

    # Track calls through ($0514) table
    ptr_0514 = read_long(0x0514)
    if ptr_0514 > 0 and ptr_0514 < 0x400000:
        for fn_off in [0, 2, 4, 6, 8, 0x0A]:
            fn_addr = ptr_0514 + fn_off
            if pc == fn_addr:
                fn_0514_calls[fn_off] = fn_0514_calls.get(fn_off, 0) + 1
                if fn_0514_calls[fn_off] <= 3:
                    print(f"\n  ($0514)+${fn_off:02X} called (${pc:06X})")

    # Track DD.XFR call site at $5150
    if pc == 0x5150:
        site_5150_calls += 1
        a1_val = cpu.a[1] & 0xFFFFFF
        print(f"\n  !! DD.XFR CALL at $5150: A1=${a1_val:06X}")

    # Track DD.MNT call site at $5104
    if pc == 0x5104:
        site_5104_calls += 1
        a1_val = cpu.a[1] & 0xFFFFFF
        print(f"\n  !! DD.MNT CALL at $5104: A1=${a1_val:06X}")

    # Track driver entries
    if DRIVER_RAM <= pc < DRIVER_END or pc == CANTMR_DST or pc == CLRCDB_DST:
        driver_entries += 1
        if driver_entries <= 5:
            off = pc - DRIVER_RAM if pc >= DRIVER_RAM and pc < DRIVER_END else pc
            print(f"\n  !! DRIVER ENTRY at PC=${pc:06X}")

    cpu.step()
    bus.tick(1)
    extra_count += 1

print(f"\n{'='*60}")
print("RESULTS")
print(f"{'='*60}")
print(f"  Instructions: {count + extra_count}")
print(f"  $A03C calls: {a03c_calls}")
print(f"  $A03E calls: {a03e_calls}")
print(f"  Scheduler dispatch ($129C): {dispatch_129c_calls}")
print(f"  ($0514) fn calls: {fn_0514_calls}")
print(f"  DD.XFR call site ($5150): {site_5150_calls}")
print(f"  DD.MNT call site ($5104): {site_5104_calls}")
print(f"  Driver entries: {driver_entries}")
print(f"  JOBCUR restores: {jobcur_restore_count}")
print(f"  Final PC=${cpu.pc:06X}")
print(f"  JOBCUR=${read_long(0x041C):08X}")
print(f"  ($0450)=${read_long(0x0450):08X}")

print("\nDone!")
