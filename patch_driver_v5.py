#!/usr/bin/env python3
"""Patch SCZ.DVR into AMOSL.MON v5 — disk patch + RAM injection.

Strategy:
  1. Disk: Copy first 1432 bytes of SCZ.DVR to $7AC2 (unchanged JSR disps)
  2. Boot: Load the patched image, run to scheduler
  3. RAM: Inject sub1 + sub2 to $9000+ and patch JSR displacements in RAM
  4. Continue: Run past scheduler and trace driver execution
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

# Relocation targets in high RAM (well beyond AMOSL.MON's $8975)
SUB1_DST = 0x9000   # 80 bytes
SUB2_DST = 0x9050   # 18 bytes

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

# ─── Step 2: Boot to scheduler ───
print(f"\nStep 2: Booting to scheduler...")

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
acia.tx_callback = lambda port, val: acia_output.append(chr(val) if 0x20 <= val < 0x7F or val in (0x0D, 0x0A) else f'<{val:02X}>')

cpu.reset()

count = 0
while not cpu.halted and count < config.max_instructions:
    if cpu.pc == 0x1250:
        break
    cpu.step()
    bus.tick(1)
    count += 1

print(f"  Scheduler at instr {count}, PC=${cpu.pc:06X}")

# Verify driver in RAM
w2 = bus.read_word(DRIVER_RAM + 2)  # 2nd word (1st is $0000)
print(f"  Driver at ${DRIVER_RAM:06X}+2: ${w2:04X} ({'present' if w2 != 0 else 'MISSING'})")

# ─── Step 3: Inject overflow subroutines into RAM ───
print(f"\nStep 3: Injecting overflow subroutines into RAM...")

# Sub1: SCZ.DVR +$05C8 to +$0617 (80 bytes) → RAM $9000
SUB1_SRC = 0x05C8
SUB1_SIZE = 0x0616 - 0x05C8 + 2  # 80 bytes
sub1_data = scz_data[SUB1_SRC:SUB1_SRC + SUB1_SIZE]

for i in range(0, SUB1_SIZE, 2):
    w = (sub1_data[i+1] << 8) | sub1_data[i]
    bus.write_word(SUB1_DST + i, w)

w_verify = bus.read_word(SUB1_DST)
print(f"  Sub1: {SUB1_SIZE} bytes at ${SUB1_DST:06X}, first word=${w_verify:04X}")

# Sub2: SCZ.DVR +$0618 to +$0629 (18 bytes) → RAM $9050
SUB2_SRC = 0x0618
SUB2_SIZE = 0x0628 - 0x0618 + 2  # 18 bytes
sub2_data = scz_data[SUB2_SRC:SUB2_SRC + SUB2_SIZE]

for i in range(0, SUB2_SIZE, 2):
    w = (sub2_data[i+1] << 8) | sub2_data[i]
    bus.write_word(SUB2_DST + i, w)

w_verify = bus.read_word(SUB2_DST)
print(f"  Sub2: {SUB2_SIZE} bytes at ${SUB2_DST:06X}, first word=${w_verify:04X}")

# ─── Step 4: Patch JSR(PC) displacements in RAM ───
print(f"\nStep 4: Patching JSR(PC) displacements in RAM...")

# JSR(PC) format: $4EBA at instr_addr, displacement word at instr_addr+2
# Target = (instr_addr + 2) + displacement
# New displacement = target_addr - (instr_addr + 2)

jsr_patches = [
    # (driver_offset, target_ram_addr)
    (0x0204, SUB1_DST),   # Sub1 call
    (0x03B0, SUB2_DST),   # Sub2 calls (7)
    (0x040A, SUB2_DST),
    (0x043C, SUB2_DST),
    (0x046C, SUB2_DST),
    (0x0490, SUB2_DST),
    (0x057C, SUB2_DST),
    (0x0592, SUB2_DST),
]

for drv_off, target in jsr_patches:
    ram_addr = DRIVER_RAM + drv_off  # Address of JSR instruction
    disp_addr = ram_addr + 2         # Address of displacement word

    # Verify opcode is $4EBA (JSR d16,PC)
    opcode = bus.read_word(ram_addr)
    if opcode != 0x4EBA:
        print(f"  ERROR: Expected $4EBA at ${ram_addr:06X}, got ${opcode:04X}")
        continue

    old_disp = bus.read_word(disp_addr)
    new_disp = (target - disp_addr) & 0xFFFF
    bus.write_word(disp_addr, new_disp)

    # Verify
    verify = bus.read_word(disp_addr)
    print(f"  ${ram_addr:06X} (+${drv_off:04X}): disp ${old_disp:04X} → ${new_disp:04X} "
          f"→ ${target:06X} ({'✓' if verify == new_disp else '✗'})")

# ─── Step 5: Continue past scheduler, trace driver ───
print(f"\n{'='*60}")
print("Step 5: RUNNING PAST SCHEDULER")
print(f"{'='*60}")

DRIVER_END = DRIVER_RAM + MAX_COPY
driver_entries = 0
driver_stuck = None
last_driver_pc = None
pc_repeat = 0

# Also track if driver calls reach relocated subs
sub_hits = {SUB1_DST: 0, SUB2_DST: 0}

extra_count = 0
max_extra = 20_000_000

while not cpu.halted and extra_count < max_extra:
    pc = cpu.pc

    # Track driver execution
    if DRIVER_RAM <= pc < DRIVER_END or pc == SUB1_DST or pc == SUB2_DST:
        if DRIVER_RAM <= pc < DRIVER_END and (last_driver_pc is None or
            not (DRIVER_RAM <= last_driver_pc < DRIVER_END)):
            driver_entries += 1
            if driver_entries <= 5:
                off = pc - DRIVER_RAM
                print(f"\n  Driver entry #{driver_entries} at PC=${pc:06X} (+${off:04X})")
                print(f"    D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} A0=${cpu.a[0]:08X}")
                print(f"    A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X} SR=${cpu.sr:04X}")

        if pc == SUB1_DST:
            sub_hits[SUB1_DST] += 1
            if sub_hits[SUB1_DST] <= 3:
                print(f"  → Sub1 called (${SUB1_DST:06X})")

        if pc == SUB2_DST:
            sub_hits[SUB2_DST] += 1
            if sub_hits[SUB2_DST] <= 3:
                print(f"  → Sub2 called (${SUB2_DST:06X})")

        # Detect stuck loop
        if pc == last_driver_pc:
            pc_repeat += 1
            if pc_repeat >= 500:
                driver_stuck = pc
                off = pc - DRIVER_RAM if DRIVER_RAM <= pc < DRIVER_END else pc - SUB1_DST
                print(f"\n  STUCK at PC=${pc:06X} at instr {count + extra_count}")
                print(f"    D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D7=${cpu.d[7]:08X}")
                print(f"    A0=${cpu.a[0]:08X} A4=${cpu.a[4]:08X} A5=${cpu.a[5]:08X}")
                # Show surrounding code
                for a in range(max(0, pc - 8), pc + 12, 2):
                    w = bus.read_word(a)
                    m = " <--" if a == pc else ""
                    print(f"    ${a:06X}: ${w:04X}{m}")
                break
        else:
            pc_repeat = 0
        last_driver_pc = pc
    else:
        last_driver_pc = pc

    cpu.step()
    bus.tick(1)
    extra_count += 1

print(f"\n  Total: {count + extra_count} instructions")
print(f"  Driver entries: {driver_entries}")
print(f"  Sub1 calls: {sub_hits[SUB1_DST]}")
print(f"  Sub2 calls: {sub_hits[SUB2_DST]}")
if driver_stuck:
    print(f"  STUCK at: ${driver_stuck:06X}")
elif cpu.halted:
    print(f"  CPU HALTED at PC=${cpu.pc:06X}")
else:
    print(f"  Instruction limit reached, PC=${cpu.pc:06X}")

if acia_output:
    text = ''.join(acia_output)
    print(f"\n  ACIA output ({len(acia_output)} chars):")
    for line in text.split('\n')[:20]:
        if line.strip():
            print(f"    {line}")

print(f"\nDone!")
