#!/usr/bin/env python3
"""Patch SCZ.DVR into AMOSL.MON v3 — with subroutine relocation.

Problem: Available space at $7AC2 is 1432 bytes, SCZ.DVR is 1631 effective bytes.
Solution: Copy first 1432 bytes to $7AC2, relocate overflow subroutines
to free space at $8800 (end of AMOSL.MON), patch JSR(PC) displacements.

Overflow subroutines:
  Sub1: +$05C8-$0616 (80 bytes) → relocate to $8800
  Sub2: +$0618-$0628 (18 bytes) → relocate to $8850
"""
import sys
import shutil
sys.path.insert(0, ".")
from pathlib import Path

src_path = Path("images/AMOS_1-3_Boot_OS.img")
dst_path = Path("images/AMOS_1-3_Boot_OS_patched.img")

DRIVER_RAM = 0x7AC2
MAX_COPY = 1432       # $805A - $7AC2
SUB1_SRC = 0x05C8     # offset in SCZ.DVR
SUB1_END = 0x0616     # inclusive (RTS)
SUB1_SIZE = SUB1_END - SUB1_SRC + 2  # 80 bytes
SUB2_SRC = 0x0618
SUB2_END = 0x0628     # inclusive (RTS)
SUB2_SIZE = SUB2_END - SUB2_SRC + 2  # 18 bytes
SUB1_DST = 0x8800     # relocation target in RAM
SUB2_DST = SUB1_DST + SUB1_SIZE  # $8850

AMOSL_START_BLOCK = 3257
BYTES_PER_BLOCK = 510

def read_word_le(data, offset):
    return (data[offset+1] << 8) | data[offset]

def write_word_le(data, offset, value):
    data[offset] = value & 0xFF
    data[offset+1] = (value >> 8) & 0xFF

# ─── Read disk image and extract SCZ.DVR ───
print("Reading SCZ.DVR...")
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

print(f"  SCZ.DVR: {len(scz_data)} bytes")

# ─── Build the block chain for AMOSL.MON ───
chain = []
blk = AMOSL_START_BLOCK
for i in range(69):
    lba = blk + 1
    off = lba * 512
    link = read_word_le(img, off)
    chain.append((blk, lba, off))
    if link == 0:
        break
    blk = link

print(f"  AMOSL.MON chain: {len(chain)} blocks")

# ─── Prepare patched driver data ───
print(f"\nPreparing driver with subroutine relocation...")

# Start with first 1432 bytes of SCZ.DVR
driver = bytearray(scz_data[:MAX_COPY])

# Define JSR(PC) patches: (scz_offset_of_displacement_word, old_disp, new_target_ram)
patches = [
    # Sub1 call sites → SUB1_DST ($8800)
    (0x0206, 0x03C2, SUB1_DST),
    # Sub2 call sites → SUB2_DST ($8850)
    (0x03B2, 0x0266, SUB2_DST),
    (0x040C, 0x020C, SUB2_DST),
    (0x043E, 0x01DA, SUB2_DST),
    (0x046E, 0x01AA, SUB2_DST),
    (0x0492, 0x0186, SUB2_DST),
    (0x057E, 0x009A, SUB2_DST),
    (0x0594, 0x0084, SUB2_DST),
]

print(f"\n  Patching {len(patches)} JSR(PC) displacements:")
for disp_off, old_disp, target_ram in patches:
    # Verify current displacement
    cur = read_word_le(driver, disp_off)
    if cur != old_disp:
        print(f"    ERROR: Expected ${old_disp:04X} at +${disp_off:04X}, got ${cur:04X}")
        sys.exit(1)

    # Calculate new displacement
    # JSR at SCZ offset (disp_off - 2), displacement word at disp_off
    # RAM address of displacement word = DRIVER_RAM + disp_off
    # Target = RAM_disp_addr + new_displacement
    ram_disp_addr = DRIVER_RAM + disp_off
    new_disp = target_ram - ram_disp_addr
    if new_disp < 0:
        new_disp += 0x10000  # Wrap to unsigned 16-bit

    write_word_le(driver, disp_off, new_disp)
    print(f"    +${disp_off:04X}: ${old_disp:04X} → ${new_disp:04X} (target ${target_ram:06X})")

# ─── Write to disk image ───
print(f"\nWriting patched data to disk...")
shutil.copy2(src_path, dst_path)
with open(dst_path, "r+b") as f:
    patched = bytearray(f.read())

    def write_to_amosl(file_offset, data_bytes):
        """Write data to AMOSL.MON at given file offset, spanning blocks as needed."""
        blk_idx = file_offset // BYTES_PER_BLOCK
        data_off = file_offset % BYTES_PER_BLOCK
        pos = 0
        remaining = len(data_bytes)
        blocks = 0

        while remaining > 0 and blk_idx < len(chain):
            _, lba, disk_off = chain[blk_idx]
            space = BYTES_PER_BLOCK - data_off
            count = min(remaining, space)
            disk_pos = disk_off + 2 + data_off  # +2 for link word

            patched[disk_pos:disk_pos + count] = data_bytes[pos:pos + count]
            pos += count
            remaining -= count
            blocks += 1
            blk_idx += 1
            data_off = 0

        return blocks

    # Write truncated driver code at $7AC2
    n = write_to_amosl(DRIVER_RAM, driver)
    print(f"  Driver code: {len(driver)} bytes at ${DRIVER_RAM:06X} ({n} blocks)")

    # Write relocated subroutine 1 at $8800
    sub1_data = scz_data[SUB1_SRC:SUB1_SRC + SUB1_SIZE]
    n = write_to_amosl(SUB1_DST, sub1_data)
    print(f"  Subroutine 1: {len(sub1_data)} bytes at ${SUB1_DST:06X} ({n} blocks)")

    # Write relocated subroutine 2 at SUB2_DST
    sub2_data = scz_data[SUB2_SRC:SUB2_SRC + SUB2_SIZE]
    n = write_to_amosl(SUB2_DST, sub2_data)
    print(f"  Subroutine 2: {len(sub2_data)} bytes at ${SUB2_DST:06X} ({n} blocks)")

    f.seek(0)
    f.write(patched)

print(f"  Saved: {dst_path}")

# ─── Boot and verify ───
print(f"\n{'='*60}")
print("BOOT VERIFICATION")
print(f"{'='*60}")

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

print(f"  Boot: {'scheduler' if cpu.pc == 0x1250 else 'stuck'} at instr {count}, PC=${cpu.pc:06X}")

# Verify driver code
w = bus.read_word(DRIVER_RAM)
print(f"  Driver at ${DRIVER_RAM:06X}: ${w:04X} ({'present' if w != 0 else 'MISSING'})")

# Verify relocated subroutines
w1 = bus.read_word(SUB1_DST)
w2 = bus.read_word(SUB2_DST)
print(f"  Sub1 at ${SUB1_DST:06X}: ${w1:04X} ({'present' if w1 != 0 else 'MISSING'})")
print(f"  Sub2 at ${SUB2_DST:06X}: ${w2:04X} ({'present' if w2 != 0 else 'MISSING'})")

# Verify $805A preserved
w_805a = bus.read_word(0x805A)
print(f"  A-line handler at $805A: ${w_805a:04X} ({'intact' if w_805a != 0 else 'GONE'})")

# Verify JSR targets resolve correctly
print(f"\n  JSR(PC) verification (first call → sub1):")
jsr_addr = DRIVER_RAM + 0x0204
jsr_w = bus.read_word(jsr_addr)
disp_w = bus.read_word(jsr_addr + 2)
target = (jsr_addr + 2 + disp_w) & 0xFFFFFF
target_w = bus.read_word(target)
print(f"    JSR at ${jsr_addr:06X}: opcode=${jsr_w:04X} disp=${disp_w:04X} → ${target:06X} = ${target_w:04X}")
expected_w = read_word_le(scz_data, SUB1_SRC)
expected_w = (scz_data[SUB1_SRC+1] << 8) | scz_data[SUB1_SRC]
print(f"    Expected first word of sub1: ${expected_w:04X} {'✓' if target_w == expected_w else '✗'}")

# DDT
print(f"\n  DDT at $7038:")
for off in [0x00, 0x06, 0x0C, 0x34]:
    addr = 0x7038 + off
    w = bus.read_word(addr)
    labels = {0: "status", 6: "drv ptr", 0xC: "dev data", 0x34: "name"}
    print(f"    +${off:02X}: ${w:04X} ({labels.get(off, '')})")

if acia_output:
    text = ''.join(acia_output)
    print(f"\n  ACIA output: {repr(text[:200])}")

print(f"\nDone!")
