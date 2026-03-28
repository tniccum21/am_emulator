#!/usr/bin/env python3
"""Diagnose mount failure: trace execution through mount code with register dumps.

Focus: What is A4 at $503C? What does (A4) contain? Why does mount fail?
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
DDT_ADDR = 0x7038
DDT_STATUS = DDT_ADDR + 0x84

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

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

# Apply patches
CORRUPT_START = 0x058E
CORRUPT_END = 0x0596
for off in range(CORRUPT_START, CORRUPT_END + 2, 2):
    ram_addr = DRIVER_RAM + off
    scz_w = read_word_le(scz_data, off)
    bus.write_word(ram_addr, scz_w)

TRAPDOOR_ADDR = 0x9100
bus.write_word(TRAPDOOR_ADDR, 0x4E71)
bus.write_word(TRAPDOOR_ADDR + 2, 0x4E75)
DDT_XFR = DDT_ADDR + 0x08
bus.write_word(DDT_XFR, 0x4EF9)
bus.write_word(DDT_XFR + 2, 0x0000)
bus.write_word(DDT_XFR + 4, 0x9100)

ddt84_val = read_long(DDT_STATUS)
if ddt84_val == 0xFFFFFFFF:
    skip_hook = True
    raw_write_long(DDT_STATUS, 0)
    skip_hook = False
    print("Cleared stale DDT+$84")

job_port_addr = read_long(0x041C)

# Disk I/O helpers
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
    partition_offset_raw = read_long(DRIVER_RAM + 0x14)
    partition_offset = partition_offset_raw & 0xFFFF if ddb_block > 0 else 0
    lba = ddb_block + partition_offset + 1
    byte_offset = lba * 512
    if 0 <= byte_offset and byte_offset + 512 <= len(disk_image):
        skip_hook = True
        for i in range(0, 512, 2):
            w = read_word_le(disk_image, byte_offset + i)
            bus.write_word(ddb_buffer + i, w)
        raw_write_long(DDT_STATUS, 0)
        skip_hook = False
        return (True, ddb_block, lba, ddb_buffer)
    else:
        return (False, ddb_block, lba, ddb_buffer)

# Main execution with detailed tracing
print(f"\n{'='*60}")
print("TRACED EXECUTION — every PC logged near mount code")
print(f"{'='*60}")

disk_io_enabled = False
a03c_count = 0
disk_a03c_just_skipped = False
extra = 0
max_extra = 2_000_000
total_logged = 0

# Key addresses to watch
MOUNT_RELATED = set()
for a in range(0x004E00, 0x004E60, 2):
    MOUNT_RELATED.add(a)
for a in range(0x005030, 0x005110, 2):
    MOUNT_RELATED.add(a)

while not cpu.halted and extra < max_extra:
    pc = cpu.pc

    # ACIA bypasses
    if pc == 0x006C3E:
        cpu.d[1] = 0x02
        cpu.d[2] = 0x28
        acia.write(0xFFFFC8, 1, 0x03)
        acia.write(0xFFFFC8, 1, 0x15)
        acia._echo_enabled = [False, False, False]
        cpu.pc = 0x006C5E
        if not disk_io_enabled:
            disk_io_enabled = True
            print(f"  [{extra}] ACIA bypass → disk I/O enabled")
        extra += 1
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
        extra += 1
        continue
    if pc == 0x006BDE:
        cpu.d[7] = 0x16
        cpu.pc = 0x006BEC
        extra += 1
        continue
    if pc == 0x006D9C:
        cpu.d[7] = 0x0E
        cpu.pc = 0x006DAA
        extra += 1
        continue

    # LINE-A intercepts
    if disk_io_enabled:
        try:
            op = bus.read_word(pc)
        except:
            op = 0

        # $A03C — queue I/O
        if op == 0xA03C and (cpu.a[0] & 0xFFFFFF) == DDT_ADDR:
            a03c_count += 1
            ddb_list = find_ddb_for_ddt()
            print(f"\n  [{extra}] $A03C #{a03c_count} at PC=${pc:06X}")
            print(f"    A0=${cpu.a[0]:08X} A1=${cpu.a[1]:08X} A4=${cpu.a[4]:08X}")
            print(f"    D0=${cpu.d[0]:08X} D6=${cpu.d[6]:08X}")
            print(f"    DDBs={[f'${d:06X}' for d in ddb_list]}")
            for ddb_ptr in ddb_list:
                ok, blk, lba, buf = do_disk_read(ddb_ptr)
                if ok:
                    preview = [f"${bus.read_word(buf + j):04X}" for j in range(0, 16, 2)]
                    print(f"    READ block={blk} LBA={lba} → ${buf:06X}")
                    print(f"    Data: {' '.join(preview)}")
                else:
                    print(f"    OUT OF RANGE block={blk} LBA={lba}")
            if not ddb_list:
                print(f"    MOUNT (no DDB)")
                skip_hook = True
                raw_write_long(DDT_STATUS, 0)
                skip_hook = False
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            disk_a03c_just_skipped = True
            extra += 1
            continue

        # $A03E — yield/wait
        if op == 0xA03E and disk_a03c_just_skipped:
            disk_a03c_just_skipped = False
            print(f"  [{extra}] $A03E skipped at ${pc:06X}")
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            extra += 1
            continue

    # DETAILED TRACING of mount-related addresses
    if disk_io_enabled and total_logged < 500:
        if pc in MOUNT_RELATED or pc == 0x682E or pc == 0x1C30:
            try:
                op = bus.read_word(pc)
            except:
                op = 0
            total_logged += 1
            reg = ""
            # Detailed regs at key mount code addresses
            if 0x5030 <= pc <= 0x5110:
                a4 = cpu.a[4] & 0xFFFFFF
                a4_byte = bus.read_byte(a4) if a4 < 0x400000 else -1
                a4_word = bus.read_word(a4) if a4 < 0x400000 else -1
                a4_1f = bus.read_byte(a4 + 0x1F) if a4 + 0x1F < 0x400000 else -1
                reg = (f" A4=${a4:06X} (A4)=${a4_byte:02X}h w=${a4_word:04X}"
                      f" A4+$1F=${a4_1f:02X}"
                      f" D0=${cpu.d[0]:08X} D5=${cpu.d[5]:08X} D7=${cpu.d[7]:08X}")
            elif pc == 0x682E:
                reg = f" COMINT entry! SP=${cpu.a[7]:08X} JOBCUR=${read_long(0x041C):08X}"
            elif 0x4E00 <= pc <= 0x4E60:
                reg = (f" A0=${cpu.a[0]:08X} A1=${cpu.a[1]:08X} A2=${cpu.a[2]:08X}"
                      f" A3=${cpu.a[3]:08X} D0=${cpu.d[0]:08X} D6=${cpu.d[6]:08X}")
            print(f"  [{extra:06d}] PC=${pc:06X} op=${op:04X}{reg}")
            if pc == 0x682E:
                # Dump full register state at COMINT
                for i in range(8):
                    print(f"    D{i}=${cpu.d[i]:08X}  A{i}=${cpu.a[i]:08X}")
                break

    cpu.step()
    bus.tick(1)
    extra += 1

    if extra % 500_000 == 0:
        print(f"  [{extra}] Progress: PC=${cpu.pc:06X}")

print(f"\nTotal instructions: {count + extra}")
print(f"$A03C count: {a03c_count}")

# Show what the DDB buffer contains
print(f"\n{'='*60}")
print("DDB CHAIN AND BUFFER CONTENTS")
print(f"{'='*60}")
ddb = read_long(0x0408) & 0xFFFFFF
n = 0
while ddb != 0 and ddb < 0x400000 and n < 10:
    n += 1
    ddb_ddt = read_long(ddb + 0x08) & 0xFFFFFF
    buf = read_long(ddb + 0x0C) & 0xFFFFFF
    blk = read_long(ddb + 0x10)
    print(f"\nDDB #{n} at ${ddb:06X}: DDT=${ddb_ddt:06X} buf=${buf:06X} blk={blk}")
    if ddb_ddt == DDT_ADDR:
        print(f"  Buffer at ${buf:06X} (first 32 words):")
        for row in range(0, 64, 16):
            words = [f"${bus.read_word(buf + row + i):04X}" for i in range(0, 16, 2)]
            print(f"    +${row:03X}: {' '.join(words)}")
    ddb = read_long(ddb) & 0xFFFFFF

print("\nDone!")
