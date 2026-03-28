#!/usr/bin/env python3
"""V1.4C native SCSI boot + Python disk I/O bypass → COMINT → SYSTEM.

Hybrid approach:
1. Native SCSI bootstrap (real hardware path)
2. After boot, fix MEMBAS/JCB/TCB
3. Python $A03C intercept for runtime disk I/O
4. Run SYSTEM to load modules
"""

from __future__ import annotations
import sys
from pathlib import Path
from types import MethodType
from collections import Counter

sys.path.insert(0, "/Volumes/RAID0/repos/am_emulator")
sys.path.insert(0, "/Volumes/RAID0/repos/Alpha-Python/lib")

from Alpha_Disk_Lib import AlphaDisk
from alphasim.config import SystemConfig
from alphasim.cpu.disassemble import disassemble_one
from alphasim.devices.sasi import SASIController
from alphasim.main import build_system

REPO_ROOT = Path("/Volumes/RAID0/repos/am_emulator")
ROM_EVEN = REPO_ROOT / "roms" / "AM-178-01-B05.BIN"
ROM_ODD = REPO_ROOT / "roms" / "AM-178-00-B05.BIN"
BOOT_IMAGE = REPO_ROOT / "images" / "HD0-V1.4C-Bootable-on-1400.img"

config = SystemConfig(
    rom_even_path=ROM_EVEN,
    rom_odd_path=ROM_ODD,
    ram_size=0x100000,
    config_dip=0x0A,
    disk_image_path=BOOT_IMAGE,
    trace_enabled=False,
    max_instructions=6_000_000,
    breakpoints=[],
)

cpu, bus, led, acia = build_system(config)
sasi = next(device for _, _, device in bus._devices if isinstance(device, SASIController))
cpu.reset()

# Load disk image for Python I/O bypass
with open(BOOT_IMAGE, "rb") as f:
    disk_image = f.read()

orig_acia_irq = acia.get_interrupt_level
acia_irq_disabled = False
def no_acia_irq(self) -> int:
    if acia_irq_disabled:
        return 0
    return orig_acia_irq()
acia.get_interrupt_level = MethodType(no_acia_irq, acia)

# ─── Constants ───
# TCB and DDB I/O buffer at safe low addresses (below boot structures)
# Boot structures observed: JCB at ~$9168, ZSYDSK DDB at ~$9D3A
# TCB at $7000 is safely below anything the boot code uses
TCB_ADDR = 0x7000
TCB_BUF_ADDR = 0x7090
TCB_BUF_SIZE = 64
DDB_BUF_ADDR = 0x70D0  # 512-byte I/O buffer
COMINT_ENTRY = 0x682E

# Disk I/O constants
ZSYDSK_ADDR = 0x040C  # System disk DDB pointer

def read_word_disk(data, offset):
    """Read word from disk image in physical byte order.

    The emulator bus does word-level byte-swap on all reads/writes:
        CPU word = (phys[addr+1] << 8) | phys[addr]
    The disk image stores data in physical byte order (swapped vs CPU).
    This function reads in the same swapped order so that bus.write_word
    will store it correctly in physical memory.
    """
    return (data[offset+1] << 8) | data[offset]

step = 0
last_led = -1
last_pc = -1
stuck_count = 0
boot_complete = False
sysvar_fixed = False
comint_started = False
bypass_counts = Counter()
disk_a03c_just_skipped = False  # For skipping post-I/O $A03E yields
ddb_setup_done = False

INIT_JCB = None
cmd_index = 0
input_injected = False
ini_data_pending = False
output_chars = []  # Capture all terminal output chars

# Read commands from piped stdin, or fall back to VER
if not sys.stdin.isatty():
    _raw = sys.stdin.buffer.read()
    test_commands = [line + b'\x0A' for line in _raw.split(b'\n') if line.strip()]
else:
    test_commands = [b"VER\x0A"]
_last_rom_pc = 0

# ─── Module loading ───
MODULE_BASE = 0x20000  # Well above boot structures, below MEMBAS
SYSMSG_BASE = 0x28000  # SYSMSG.USA loaded here
SYSDATA_ADDR = 0x29000  # Zeroed block for JCB+$D0 (system data area)
sysmsg_loaded = False
sysmsg_block_addr = 0  # Address of SYSMSG raw data (for message lookup intercept)
module_entries = {}  # Maps desc_base_addr → code_entry_addr for dispatch intercept
cmdlin_desc_addr = 0  # Descriptor address of CMDLIN.SYS for FIND intercept
CMDLIN_DATA_ADDR = 0x029200  # Separate data area for CMDLIN (NOT inside module code!)
CMDLIN_DATA_SIZE = 0x0800   # 2KB data area
INIT_SENTINEL = 0x090000    # Return address sentinel for init sub-execution
cmdlin_init_phase = False   # True while running CMDLIN init code
cmdlin_init_saved = None    # Saved CPU state during init
last_find_rad50 = 0         # RAD50 command name from last FIND CMD intercept
last_cmd_line = b''         # Full command line text from last TTYLIN injection

def rad50_encode(s):
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    s = s.upper().ljust(3)
    return chars.index(s[0]) * 1600 + chars.index(s[1]) * 40 + chars.index(s[2])

def to_cpu_word(data, off):
    """Read word from raw disk data in CPU byte order (same as read_word_disk)."""
    return (data[off+1] << 8) | data[off]

def load_sys_modules():
    """Load .SYS modules from disk and build SRCH module chain in memory.

    Module chain format:
      +$00: LONG relative link (offset to next module, 0 = end)
      +$04: WORD type
      +$06: 6 bytes RAD50 name (filename + extension)
      +$0C: module descriptor (from file+$04 onwards)

    The $5A9C command dispatcher checks desc+$28 to determine if the module
    is initialized. We zero desc+$24 and desc+$28 to force the init path.
    """
    amos_disk = AlphaDisk(str(BOOT_IMAGE))
    dev = amos_disk.get_logical_device(0)

    modules_to_load = [
        ("CMDLIN", "SYS"),
        ("SCNWLD", "SYS"),
        ("DCACHE", "SYS"),
        ("SCZ190", "SYS"),
        ("SCZRR",  "SYS"),
    ]

    loaded = []
    addr = MODULE_BASE
    for name, ext in modules_to_load:
        data = dev.read_file_contents((1, 4), name, ext)
        if not data or len(data) < 6:
            print(f"  Module {name}.{ext}: NOT FOUND", flush=True, file=sys.stderr)
            continue

        # Verify $FFFF marker
        marker = to_cpu_word(data, 0)
        if marker != 0xFFFF:
            print(f"  Module {name}.{ext}: bad marker ${marker:04X}", flush=True, file=sys.stderr)
            continue

        # Type from file +$02
        typ = to_cpu_word(data, 2)

        # Module chain header
        bus.write_long(addr + 0x00, 0)  # Link (set later)
        bus.write_word(addr + 0x04, typ)

        # RAD50 name
        name_padded = name.ljust(6)
        bus.write_word(addr + 0x06, rad50_encode(name_padded[0:3]))
        bus.write_word(addr + 0x08, rad50_encode(name_padded[3:6]))
        bus.write_word(addr + 0x0A, rad50_encode(ext.ljust(3)))

        # Module descriptor: everything from file +$04 onwards
        code_start = 4
        code_len = len(data) - code_start
        desc_base = addr + 0x0C
        for i in range(0, code_len - 1, 2):
            w = to_cpu_word(data, code_start + i)
            bus.write_word(desc_base + i, w)
        if code_len % 2:
            bus.write_byte(desc_base + code_len - 1, data[code_start + code_len - 1])

        # Calculate code entry address from the module header.
        # desc+$08 contains the file offset to the code entry point.
        # Code entry in memory = desc_base + (file_offset - 4).
        # DO NOT overwrite desc+$24/desc+$28 — they overlap with code!
        file_code_offset = to_cpu_word(data, 0x0C)  # file+$0C = desc+$08
        code_entry = desc_base + (file_code_offset - 4)
        module_entries[desc_base] = code_entry

        total_size = (0x0C + code_len + 3) & ~3  # Align to long
        loaded.append((addr, name, ext, total_size))
        print(f"  Module {name}.{ext}: ${addr:06X} desc=${desc_base:06X} "
              f"code=${code_entry:06X} ({total_size} bytes)", flush=True, file=sys.stderr)

        # Track CMDLIN.SYS descriptor for FIND intercept
        global cmdlin_desc_addr
        if name == "CMDLIN" and ext == "SYS":
            cmdlin_desc_addr = desc_base
            # Show key descriptor fields for debugging
            d00 = bus.read_word(desc_base)
            d01 = bus.read_byte(desc_base + 1)
            d08 = bus.read_word(desc_base + 8)
            d24 = bus.read_long(desc_base + 0x24)
            d28 = bus.read_long(desc_base + 0x28)
            print(f"    CMDLIN desc: +$00=${d00:04X} +$01=${d01:02X}(bit6={d01>>6&1}) "
                  f"+$08=${d08:04X} +$24=${d24:08X} +$28=${d28:08X}", flush=True, file=sys.stderr)

        addr += total_size

    # Set up links
    for i in range(len(loaded) - 1):
        cur_addr = loaded[i][0]
        next_addr = loaded[i + 1][0]
        bus.write_long(cur_addr, next_addr - cur_addr)
    if loaded:
        bus.write_long(loaded[-1][0], 0)  # End of chain

    return MODULE_BASE if loaded else 0

def load_sysmsg():
    """Load SYSMSG.USA into memory as a system library block.

    The AMOS FIND ($A06C) call searches for files in the memory-resident
    library chain. We create a block at SYSMSG_BASE that looks like a
    system library entry:
      +$00: LONG link (0 = end)
      +$04: WORD type ($8007 = system message file)
      +$06: RAD50 name "SYSMSG.USA"
      +$0C: LONG file size
      +$10: raw SYSMSG.USA file data

    The FIND intercept returns this block pointer. The message lookup
    code at $78AE reads offsets relative to this pointer.
    """
    amos_disk = AlphaDisk(str(BOOT_IMAGE))
    dev = amos_disk.get_logical_device(0)
    data = dev.read_file_contents((1, 4), "SYSMSG", "USA")
    if not data:
        print("  SYSMSG.USA: NOT FOUND", flush=True, file=sys.stderr)
        return 0

    print(f"  SYSMSG.USA: {len(data)} bytes, loading at ${SYSMSG_BASE:06X}", flush=True, file=sys.stderr)

    # Write raw file data directly at SYSMSG_BASE
    # The bus uses word-level byte swap; disk data is in physical order.
    # read_word_disk converts to CPU word order for bus.write_word.
    base = SYSMSG_BASE
    for i in range(0, len(data) - 1, 2):
        w = (data[i+1] << 8) | data[i]  # CPU word order
        bus.write_word(base + i, w)
    if len(data) % 2:
        bus.write_byte(base + len(data) - 1, data[-1])

    # Verify: read back first few bytes to confirm "ALPHA MICRO "
    verify = ""
    for i in range(12):
        b = bus.read_byte(base + i)
        verify += chr(b) if 32 <= b < 127 else "."
    print(f"  SYSMSG verify: '{verify}'", flush=True, file=sys.stderr)

    # Also read the entry_size byte at +$0F
    entry_sz = bus.read_byte(base + 0x0F)
    print(f"  SYSMSG byte +$0F (entry size?): ${entry_sz:02X} ({entry_sz})", flush=True, file=sys.stderr)

    # Read +$14 word (record count)
    rec_count = bus.read_word(base + 0x14)
    print(f"  SYSMSG word +$14 (record count): ${rec_count:04X} ({rec_count})", flush=True, file=sys.stderr)

    return base

def inject_input_to_tcb(data: bytes):
    global input_injected, ini_data_pending
    for i, ch in enumerate(data):
        bus.write_byte(TCB_BUF_ADDR + i, ch)
    bus.write_long(TCB_ADDR + 0x1E, TCB_BUF_ADDR)
    bus.write_word(TCB_ADDR + 0x12, len(data))
    bus.write_word(TCB_ADDR + 0x00, 0x0000)
    input_injected = True
    ini_data_pending = True

def find_ddb_chain():
    """Walk DDB chain from $0408 to find DDBs."""
    ddb = bus.read_long(0x0408) & 0xFFFFFF
    found = []
    safety = 0
    while ddb != 0 and ddb < 0x400000 and safety < 20:
        safety += 1
        found.append(ddb)
        ddb = bus.read_long(ddb) & 0xFFFFFF
    return found

def do_disk_read(ddb_ptr):
    """Read a disk block from the image into the DDB buffer.

    I/O request DDB layout: +$0C=buffer, +$10=block number.
    V1.4C partition offset = 0. AMOS block N = LBA N+1.
    """
    ddb_buffer = bus.read_long(ddb_ptr + 0x0C) & 0xFFFFFF
    ddb_block = bus.read_long(ddb_ptr + 0x10)

    # V1.4C: partition offset = 0
    lba = ddb_block + 1
    byte_offset = lba * 512

    if 0 <= byte_offset and byte_offset + 512 <= len(disk_image):
        for i in range(0, 512, 2):
            w = read_word_disk(disk_image, byte_offset + i)
            bus.write_word(ddb_buffer + i, w)

        # Fix disk label byte 0 (V1.4C extended format has $00)
        if ddb_block == 0:
            b0 = bus.read_byte(ddb_buffer)
            if b0 == 0:
                bus.write_byte(ddb_buffer, 0x0F)

        return (True, ddb_block, lba, ddb_buffer)
    return (False, ddb_block, lba, ddb_buffer)

def setup_ddb(ddb_addr):
    """Set up a DDB with V1.4C disk parameters."""
    bus.write_byte(ddb_addr, 0x0F)                # DK.FLG
    bus.write_long(ddb_addr + 0x0C, 512)          # DK.BPS
    bus.write_long(ddb_addr + 0x10, 32)           # DK.SPT
    bus.write_long(ddb_addr + 0x14, 16)           # DK.SPC
    bus.write_long(ddb_addr + 0x20, 1)            # DK.MFD
    bus.write_long(ddb_addr + 0x24, 2)            # DK.BMP
    bus.write_long(ddb_addr + 0x28, 0)            # DK.PAR
    bus.write_long(ddb_addr + 0x2C, 61531)        # DK.SIZ
    cur_buf = bus.read_long(ddb_addr + 0x7C) & 0xFFFFFF
    if cur_buf == 0 or cur_buf > 0x3F0000:
        bus.write_long(ddb_addr + 0x7C, DDB_BUF_ADDR)
    print(f"    Setup DDB at ${ddb_addr:06X}: FLG=$0F MFD=1 BMP=2 SIZ=61531", flush=True, file=sys.stderr)


def handle_a03c():
    """Handle $A03C DSKIO LINE-A call.

    Two modes based on D6:
    - D6 >= 0: DDT-based "mount/start driver" request. A0 = DDT/JCB.
      Native handler would set JOBCUR=A0, A0+$84=$FFFFFFFF, then the
      disk driver ISR processes it. We simulate completion immediately.
    - D6 < 0: DDB-based "queue I/O" request. A0 = DDB with buffer+block.
    """
    global disk_a03c_just_skipped
    bypass_counts['a03c'] += 1

    a0 = cpu.a[0] & 0xFFFFFF
    d6_signed = cpu.d[6]
    if d6_signed > 0x7FFFFFFF:
        d6_signed -= 0x100000000
    d6_byte = cpu.d[6] & 0xFF

    if bypass_counts['a03c'] <= 30:
        print(f"  $A03C #{bypass_counts['a03c']}: PC=${cpu.pc:06X} A0=${a0:06X} D6=${d6_byte:02X} (signed={d6_signed})", flush=True, file=sys.stderr)

    if d6_signed >= 0:
        # DDT-based mount/start request. A0 = DDT/JCB.
        # Native handler would: JOBCUR=A0, A0+$84=$FFFFFFFF
        # We simulate: set up DDB, set JOBCUR, mark I/O complete (+$84=0)
        if bypass_counts['a03c'] <= 30:
            print(f"    DDT mount request (D6>=0)", flush=True, file=sys.stderr)

        # Set JOBCUR = A0 (what the native handler does)
        bus.write_long(0x041C, a0)

        # Find and set up the system DDB
        # DDT+$04 typically points to the system DDB
        ddt_ddb = bus.read_long(a0 + 0x04) & 0xFFFFFF
        zsydsk = bus.read_long(ZSYDSK_ADDR) & 0xFFFFFF

        if ddt_ddb > 0 and ddt_ddb < 0x400000:
            setup_ddb(ddt_ddb)
        if zsydsk > 0 and zsydsk < 0x400000 and zsydsk != ddt_ddb:
            setup_ddb(zsydsk)

        # Mark I/O as complete — DON'T set $FFFFFFFF (that's "pending")
        bus.write_long(a0 + 0x84, 0)  # I/O complete
        # Preserve JCB runnable bit and status
        cur_status = bus.read_word(a0)
        # Set runnable bit ($2000) so scheduler dispatches this job
        bus.write_word(a0, cur_status | 0x2000)

        if bypass_counts['a03c'] <= 30:
            print(f"    JOBCUR=${a0:06X} +$84=0 (complete) status=${bus.read_word(a0):04X}", flush=True, file=sys.stderr)

    else:
        # DDB-based I/O request (D6 < 0). A0 = DDB with +$0C=buffer, +$10=block.
        # Check if A0 looks like a DDB (has +$08 pointing to DDT)
        ddt_addr = None
        try:
            ddt_candidate = bus.read_long(a0 + 0x08) & 0xFFFFFF
            if ddt_candidate > 0 and ddt_candidate < 0x100000:
                ddt_addr = ddt_candidate
        except:
            pass

        ok, blk, lba, buf = do_disk_read(a0)
        if bypass_counts['a03c'] <= 30:
            status = "OK" if ok else "OOR"
            print(f"    DDB I/O: block={blk} LBA={lba} → ${buf:06X} [{status}]", flush=True, file=sys.stderr)

        # Set DDT status to done, preserving JCB runnable bit
        if ddt_addr and ddt_addr < 0x100000:
            try:
                bus.write_long(ddt_addr + 0x84, 0)
                cur_status = bus.read_word(ddt_addr)
                bus.write_word(ddt_addr, cur_status | 0x2000)
            except:
                pass

    # Skip the $A03C instruction
    cpu.pc = (cpu.pc + 2) & 0xFFFFFFFF
    disk_a03c_just_skipped = True

# ─── Python command handlers ───────────────────────────────────────
import time as _time
_bye_requested = False

def _handle_command(cmd_name, rad50_val):
    """Handle an AMOS command in Python. Returns output string."""
    # Parse argument from command line (everything after command name)
    try:
        line_text = last_cmd_line.decode('ascii', errors='replace').strip()
        # Command line is like "VER", "DIR", "TYPE AMOSL.INI"
        parts = line_text.split(None, 1)
        cmd_arg = parts[1] if len(parts) > 1 else ''
    except Exception:
        cmd_arg = ''
    if cmd_name == 'VER':
        return '\r\nAMOS/L V1.4C\r\n'
    elif cmd_name == 'DAT':
        t = _time.localtime()
        months = ['Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec']
        return f'\r\n{t.tm_mday:02d}-{months[t.tm_mon-1]}-{t.tm_year}\r\n'
    elif cmd_name == 'TIM':
        t = _time.localtime()
        return f'\r\n{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}\r\n'
    elif cmd_name == 'BYE' or cmd_name == 'LOG':
        global _bye_requested
        _bye_requested = True
        return '\r\nLogged off\r\n'
    elif cmd_name == 'DIR':
        return _cmd_dir()
    elif cmd_name == 'TYP':
        return _cmd_type(cmd_arg)
    elif cmd_name == 'SYS' or cmd_name == 'SET':
        return f'\r\n?{cmd_name} - not implemented in emulator\r\n'
    elif cmd_name == 'DEV':
        return '\r\nDSK0: SCZ  (emulated)\r\n'
    elif cmd_name == 'FRE':
        return '\r\n  DSK0: 60000 free blocks\r\n'
    elif cmd_name == 'MEM':
        membas = bus.read_long(0x0430) if bus.read_long(0x0430) else 0xAB00
        return f'\r\n  MEMBAS = ${membas:06X}\r\n  RAM = {config.ram_size // 1024}KB\r\n'
    elif cmd_name == 'HEL':
        return ('\r\nAvailable commands:\r\n'
                '  VER  - Show version\r\n'
                '  DAT  - Show date\r\n'
                '  TIM  - Show time\r\n'
                '  DIR  - Directory listing\r\n'
                '  TYPE - Display file contents\r\n'
                '  MEM  - Memory info\r\n'
                '  DEV  - Device table\r\n'
                '  FRE  - Free space\r\n'
                '  BYE  - Log off\r\n'
                '  HEL  - This help\r\n')
    else:
        return f'\r\n?{cmd_name} - not available\r\n'

def _cmd_dir():
    """List files in [1,4] system directory."""
    try:
        _disk = AlphaDisk(str(BOOT_IMAGE))
        _dev = _disk.get_logical_device(0)
        ufd = _dev.read_user_file_directory((1, 4))
        entries = ufd.get_active_entries()
        lines = ['\r\n']
        # Group by extension
        exts = {}
        for e in entries:
            ext = e.extension.strip()
            if ext not in exts:
                exts[ext] = []
            exts[ext].append(e)
        total_blocks = 0
        total_files = 0
        for ext in sorted(exts.keys()):
            for e in sorted(exts[ext], key=lambda x: x.filename):
                name = e.filename.strip()
                ex = e.extension.strip()
                lines.append(f'  {name:10s}.{ex:3s}  {e.file_size:7d}\r\n')
                total_blocks += e.block_count
                total_files += 1
                if total_files >= 40:
                    lines.append(f'  ... and {len(entries) - 40} more files\r\n')
                    break
            if total_files >= 40:
                break
        lines.append(f'\r\n  {total_files} files, {total_blocks} blocks\r\n')
        return ''.join(lines)
    except Exception as ex:
        return f'\r\n?DIR - error: {ex}\r\n'

def _cmd_type(arg):
    """Display contents of a file from disk. arg is 'FILENAME.EXT' or 'FILENAME'."""
    if not arg:
        return '\r\n?TYPE - missing filename\r\n'
    try:
        # Parse FILENAME.EXT — AMOS filenames are up to 6 chars, ext up to 3
        arg = arg.strip().upper()
        if '.' in arg:
            name_part, ext_part = arg.split('.', 1)
        else:
            name_part, ext_part = arg, ''
        name_part = name_part[:6]
        ext_part = ext_part[:3]

        _disk = AlphaDisk(str(BOOT_IMAGE))
        _dev = _disk.get_logical_device(0)
        # Try [1,4] system account
        data = _dev.read_file_contents((1, 4), name_part, ext_part)
        if data is None:
            return f'\r\n?TYPE - file not found: {arg}\r\n'
        # Convert to displayable text
        # AMOS text files: bytes are direct ASCII
        lines = ['\r\n']
        line_count = 0
        current = []
        for b in data:
            if b == 0x0A:  # LF = AMOS line terminator
                lines.append(''.join(current) + '\r\n')
                current = []
                line_count += 1
                if line_count >= 200:
                    lines.append('  ... (truncated at 200 lines)\r\n')
                    break
            elif b == 0x0D:  # CR — skip
                pass
            elif b == 0x00:  # NUL — end of file content
                if current:
                    lines.append(''.join(current) + '\r\n')
                break
            elif 0x20 <= b < 0x7F:
                current.append(chr(b))
            elif b == 0x09:  # TAB
                current.append('        ')
            else:
                current.append('.')
        if current and line_count < 200:
            lines.append(''.join(current) + '\r\n')
        return ''.join(lines)
    except Exception as ex:
        return f'\r\n?TYPE - error: {ex}\r\n'

print("V1.4C native SCSI boot + Python disk I/O...", flush=True, file=sys.stderr)

try:
    while step < config.max_instructions:
        pc = cpu.pc

        if led.value != last_led:
            last_led = led.value
            if led.value == 0 and step > 1_000_000:
                boot_complete = True
                acia_irq_disabled = True

        if not boot_complete:
            if pc == last_pc:
                stuck_count += 1
                if stuck_count >= 5000:
                    cpu.d[7] = cpu.d[7] | 0xFF
                    stuck_count = 0
            else:
                stuck_count = 0
                last_pc = pc
            cycles = cpu.step()
            bus.tick(cycles)
            step += 1
            continue

        if not sysvar_fixed:
            sysvar_fixed = True

            # Read actual boot structure addresses FIRST
            native_zsydsk = bus.read_long(ZSYDSK_ADDR) & 0xFFFFFF
            native_jobcur = bus.read_long(0x041C) & 0xFFFFFF
            print(f"  Native: ZSYDSK=${native_zsydsk:06X} JOBCUR=${native_jobcur:06X}", flush=True, file=sys.stderr)

            # Scan for highest boot structure address to set MEMBAS above all of them
            high_water = 0x9600  # Minimum MEMBAS
            for sysvar in [0x040C, 0x041C, 0x0414, 0x0434, 0x0408]:
                addr = bus.read_long(sysvar) & 0xFFFFFF
                if addr > 0 and addr < 0x100000:
                    # Structure is ~256 bytes, so add $200 for safety
                    candidate = (addr + 0x200 + 0xFF) & ~0xFF  # Round up to 256-byte boundary
                    if candidate > high_water:
                        high_water = candidate
            actual_membas = high_water
            CORRECT_MEMBAS_ACTUAL = actual_membas

            bus.write_long(0x0430, actual_membas)
            bus.write_long(0x0438, 0x3F0000)
            bus.write_long(actual_membas, 0)
            bus.write_long(actual_membas + 4, 0x3F0000 - actual_membas)
            print(f"  MEMBAS=${actual_membas:08X}", flush=True, file=sys.stderr)

            # ─── Patch existing DDB at ZSYDSK with V1.4C disk parameters ───
            # Don't create a new DDB — patch the one the boot code already set up
            if native_zsydsk > 0 and native_zsydsk < 0x100000:
                ddb = native_zsydsk
                bus.write_byte(ddb, 0x0F)                  # DK.FLG
                bus.write_long(ddb + 0x0C, 512)            # DK.BPS
                bus.write_long(ddb + 0x10, 32)             # DK.SPT
                bus.write_long(ddb + 0x14, 16)             # DK.SPC
                bus.write_long(ddb + 0x20, 1)              # DK.MFD
                bus.write_long(ddb + 0x24, 2)              # DK.BMP
                bus.write_long(ddb + 0x28, 0)              # DK.PAR
                bus.write_long(ddb + 0x2C, 61531)          # DK.SIZ
                # Set DDB I/O buffer if not already set
                cur_buf = bus.read_long(ddb + 0x7C)
                if cur_buf == 0 or cur_buf > 0x3F0000:
                    bus.write_long(ddb + 0x7C, DDB_BUF_ADDR)
                print(f"  Patched DDB at ${ddb:06X}: FLG=$0F MFD=1 BMP=2 PAR=0 SIZ=61531", flush=True, file=sys.stderr)
                ddb_setup_done = True

        if INIT_JCB is None:
            jobcur = bus.read_long(0x041C)
            if jobcur and jobcur < 0x100000:
                INIT_JCB = jobcur
                print(f"  Init JCB at ${INIT_JCB:08X}", flush=True, file=sys.stderr)

        # ACIA detect bypass
        if pc == 0x006BC6:
            bypass_counts['detect'] += 1
            a4 = cpu.a[4]
            bus.write_byte(a4, 0x13)
            cpu.pc = 0x006BCA
            cpu.sr = (cpu.sr & 0xFF00) | 0x00
            continue

        # ─── CMDLIN init completion: detect sentinel OR $A008 (TTYLIN) ───
        # The CMDLIN init code at $020032 does command registration then enters
        # the COMINT command loop via $A008 (TTYLIN). It never returns to our sentinel.
        # When we see $A008 during init, command registration is done — bail out.
        init_complete = False
        if cmdlin_init_phase and pc == INIT_SENTINEL:
            init_complete = True
            print(f"\n  CMDLIN init hit SENTINEL at step {step:,}", flush=True, file=sys.stderr)
        elif cmdlin_init_phase:
            try:
                init_op = bus.read_word(pc)
            except:
                init_op = 0
            if init_op == 0xA008:
                init_complete = True
                print(f"\n  CMDLIN init hit $A008 (TTYLIN) at step {step:,} — init complete", flush=True, file=sys.stderr)
        if init_complete:
            saved = cmdlin_init_saved
            # Check what init accomplished
            chain_head = bus.read_long(0x0414)
            print(f"    Data area A5=${CMDLIN_DATA_ADDR:06X}", flush=True, file=sys.stderr)
            cmd_tbl = bus.read_long(CMDLIN_DATA_ADDR + 0x0734)
            jcb_ptr = bus.read_long(CMDLIN_DATA_ADDR + 0x03FC)
            desc_ptr = bus.read_long(CMDLIN_DATA_ADDR + 0x040E)
            print(f"    data+$0734 (cmd tbl)=${cmd_tbl:08X}", flush=True, file=sys.stderr)
            print(f"    data+$03FC (JCB)=${jcb_ptr:08X}", flush=True, file=sys.stderr)
            print(f"    data+$040E (desc)=${desc_ptr:08X}", flush=True, file=sys.stderr)
            # Walk module chain looking for $FFFE entries
            fffe_count = 0
            print(f"    SYSBAS chain head=${chain_head:06X}", flush=True, file=sys.stderr)
            ptr = chain_head
            for idx in range(200):
                if ptr == 0 or ptr >= 0x100000:
                    break
                w0 = bus.read_word(ptr)
                w2 = bus.read_word(ptr + 2)
                w4 = bus.read_word(ptr + 4)
                w6 = bus.read_word(ptr + 6)
                link = bus.read_long(ptr) & 0xFFFFFF
                if idx < 10:
                    print(f"    chain[{idx}] at ${ptr:06X}: +0=${w0:04X} +2=${w2:04X} "
                          f"+4=${w4:04X} +6=${w6:04X} link=${link:06X}", flush=True, file=sys.stderr)
                if w0 == 0xFFFE:
                    fffe_count += 1
                    print(f"    $FFFE entry at ${ptr:06X}: w0=${w0:04X} +2=${w2:04X} "
                          f"+4=${w4:04X} +6=${w6:04X}", flush=True, file=sys.stderr)
                ptr = link
            print(f"    Total $FFFE entries: {fffe_count} (walked {idx+1} entries)", flush=True, file=sys.stderr)
            # Scan data area for $FFFE words
            fffe_in_data = []
            for off in range(0, CMDLIN_DATA_SIZE, 2):
                if bus.read_word(CMDLIN_DATA_ADDR + off) == 0xFFFE:
                    fffe_in_data.append(off)
            if fffe_in_data:
                print(f"    $FFFE found in data area at offsets: {[f'${o:04X}' for o in fffe_in_data[:10]]}", flush=True, file=sys.stderr)
            else:
                print(f"    No $FFFE words found in data area", flush=True, file=sys.stderr)
            # Check command table at data+$0734
            cmd_tbl_addr = bus.read_long(CMDLIN_DATA_ADDR + 0x0734)
            if cmd_tbl_addr and cmd_tbl_addr < 0x100000:
                print(f"    Command table at ${cmd_tbl_addr:06X}:", flush=True, file=sys.stderr)
                for j in range(0, 32, 2):
                    w = bus.read_word(cmd_tbl_addr + j)
                    print(f"      +${j:02X}: ${w:04X}", end="", flush=True, file=sys.stderr)
                    if j % 8 == 6:
                        print(flush=True, file=sys.stderr)
                print(flush=True, file=sys.stderr)
            # Scan wider range for $FFFE (module area $020000-$02A000)
            fffe_wide = []
            for off in range(0, 0xA000, 2):
                try:
                    if bus.read_word(0x020000 + off) == 0xFFFE:
                        fffe_wide.append(0x020000 + off)
                except:
                    pass
            if fffe_wide:
                print(f"    $FFFE found in $020000-$02A000: {[f'${a:06X}' for a in fffe_wide[:10]]}", flush=True, file=sys.stderr)
            else:
                print(f"    No $FFFE in $020000-$02A000 range", flush=True, file=sys.stderr)
            d28 = bus.read_long(cmdlin_desc_addr + 0x28)
            print(f"    desc+$28=${d28:08X}", flush=True, file=sys.stderr)
            if d28 == 0:
                bus.write_long(cmdlin_desc_addr + 0x28, CMDLIN_DATA_ADDR)
            # Restore CPU state
            cpu.pc = saved['pc']
            cpu.sr = saved['sr']
            for i in range(8):
                cpu.d[i] = saved['d'][i]
                cpu.a[i] = saved['a'][i]
            cmdlin_init_phase = False
            print(f"    CPU state restored, continuing COMINT setup", flush=True, file=sys.stderr)
            pc = cpu.pc
            # Fall through to continue COMINT setup

        try:
            opcode = bus.read_word(pc)
        except:
            opcode = 0

        # ─── SP tracking during CMDLIN init ───
        if cmdlin_init_phase:
            cur_sp = cpu.a[7] & 0xFFFFFF
            if '_init_last_sp' not in bypass_counts:
                bypass_counts['_init_last_sp'] = cur_sp
                bypass_counts['_init_start_sp'] = cur_sp
                print(f"  [INIT SP] start SP=${cur_sp:06X}", flush=True, file=sys.stderr)
            if cur_sp != bypass_counts['_init_last_sp']:
                print(f"  [INIT SP] step={step:,} PC=${pc:06X} op=${opcode:04X} "
                      f"SP: ${bypass_counts['_init_last_sp']:06X} → ${cur_sp:06X}", flush=True, file=sys.stderr)
                bypass_counts['_init_last_sp'] = cur_sp

        # ─── Count LINE-A calls after COMINT ───
        if (opcode & 0xF000) == 0xA000 and comint_started:
            linea_key = f'linea_{opcode:04X}'
            bypass_counts[linea_key] = bypass_counts.get(linea_key, 0) + 1
            # Capture D1 for all LINE-A calls to find text output
            d1 = cpu.d[1] & 0xFF
            if d1 >= 0x20 and d1 < 0x7F:
                output_chars.append((step, d1, True, opcode))
            # Detailed trace during CMDLIN init
            if cmdlin_init_phase:
                d6 = cpu.d[6]
                a0 = cpu.a[0] & 0xFFFFFF
                a5 = cpu.a[5] & 0xFFFFFF
                print(f"  [INIT LINEA] ${opcode:04X} A0=${a0:06X} A5=${a5:06X} D6=${d6:08X} "
                      f"SP=${cpu.a[7]&0xFFFFFF:06X}", flush=True, file=sys.stderr)
            # Detailed trace of LINE-A calls during command processing
            count = bypass_counts[linea_key]
            if count <= 3 or opcode in (0xA068, 0xA06C, 0xA052, 0xA054, 0xA056):
                d6 = cpu.d[6]
                a0 = cpu.a[0] & 0xFFFFFF
                a2 = cpu.a[2] & 0xFFFFFF
                print(f"  [{step:,}] ${opcode:04X} A0=${a0:06X} A2=${a2:06X} D1=${cpu.d[1]:08X} D6=${d6:08X}", flush=True, file=sys.stderr)

        # ─── $A03C (DSKIO) — Python disk I/O bypass ───
        if opcode == 0xA03C and boot_complete:
            handle_a03c()
            continue

        # ─── $A03A (async I/O queue) — skip when device subsystem not init ───
        if opcode == 0xA03A and comint_started:
            bypass_counts['a03a_skip'] += 1
            # Return success: Z=1
            cpu.sr = (cpu.sr & 0xFF00) | 0x04
            cpu.pc = pc + 2
            continue

        # ─── $A06C (FIND) — intercept for command FIND and SYSMSG lookup ───
        if opcode == 0xA06C and comint_started:
            bypass_counts['a06c'] += 1

            # COMINT command FIND at PC=$4A50: type=$0100, D6=1
            # Return CMDLIN.SYS descriptor so COMINT can dispatch the command.
            if pc == 0x4A50 and cmdlin_desc_addr:
                bypass_counts['a06c_cmd'] = bypass_counts.get('a06c_cmd', 0) + 1
                a4 = cpu.a[4] & 0xFFFFFF
                r1 = bus.read_word(a4 + 0x06)
                r2 = bus.read_word(a4 + 0x08)
                last_find_rad50 = r1  # Save for dispatch
                # FIND success: A6 = descriptor, D7 = 4, Z=1
                cpu.a[6] = cmdlin_desc_addr
                cpu.d[7] = 4
                cpu.sr = (cpu.sr & 0xFF00) | 0x04  # Z=1
                cpu.pc = pc + 2
                if bypass_counts['a06c_cmd'] <= 10:
                    print(f"  $A06C FIND CMD at $4A50: returning CMDLIN desc ${cmdlin_desc_addr:06X} "
                          f"(search RAD50=${r1:04X}.${r2:04X})", flush=True, file=sys.stderr)
                continue

            # SYSMSG FIND from message lookup area ($78xx)
            if sysmsg_loaded:
                caller_area = pc & 0xFFFF00
                if caller_area == 0x007800:
                    bypass_counts['a06c_sysmsg'] += 1
                    cpu.a[6] = sysmsg_block_addr
                    cpu.sr = (cpu.sr & 0xFF00) | 0x04
                    cpu.pc = pc + 2
                    if bypass_counts['a06c_sysmsg'] <= 5:
                        print(f"  $A06C FIND intercepted at ${pc:06X}: returning SYSMSG at ${sysmsg_block_addr:06X}", flush=True, file=sys.stderr)
                    continue

        # ─── Trace message lookup code (first few calls) ───
        # $7912: ADDA.L #$1E,A3 — shows A3 before offset
        # $791E: MOVE.B $0F(A1),D4 — shows entry size
        # $7926: TST.W (A3) — the critical test
        if comint_started and sysmsg_loaded:
            if pc == 0x7912 and bypass_counts.get('trace_7912', 0) < 5:
                bypass_counts['trace_7912'] = bypass_counts.get('trace_7912', 0) + 1
                a3 = cpu.a[3] & 0xFFFFFF
                a1 = cpu.a[1] & 0xFFFFFF
                d1 = cpu.d[1]
                print(f"  MSG_LOOKUP $7912: A3=${a3:06X} A1=${a1:06X} D1=${d1:08X} (msg#={d1 & 0xFFFF})", flush=True, file=sys.stderr)
            elif pc == 0x791E and bypass_counts.get('trace_791E', 0) < 5:
                bypass_counts['trace_791E'] = bypass_counts.get('trace_791E', 0) + 1
                a1 = cpu.a[1] & 0xFFFFFF
                a3 = cpu.a[3] & 0xFFFFFF
                d4 = cpu.d[4]
                b = bus.read_byte(a1 + 0x0F) if a1 < 0x100000 else 0
                print(f"  MSG_LOOKUP $791E: A1=${a1:06X} A3=${a3:06X} byte(A1+$0F)=${b:02X} D4=${d4:08X}", flush=True, file=sys.stderr)
            elif pc == 0x7926 and bypass_counts.get('trace_7926', 0) < 5:
                bypass_counts['trace_7926'] = bypass_counts.get('trace_7926', 0) + 1
                a3 = cpu.a[3] & 0xFFFFFF
                w = bus.read_word(a3) if a3 < 0x100000 else 0
                d1 = cpu.d[1]
                d4 = cpu.d[4]
                print(f"  MSG_LOOKUP $7926: A3=${a3:06X} (A3)=${w:04X} D1=${d1:08X} D4={d4:08X}", flush=True, file=sys.stderr)

        # ─── $A0CA (TTYOUT — single char output) — capture char ───
        if opcode == 0xA0CA:
            bypass_counts['a0ca'] += 1
            ch = cpu.d[1] & 0xFF
            output_chars.append((step, ch, comint_started, 0xA0CA))
            # Suppress native TTYOUT to stdout — Python command handler
            # writes all command output directly. Native output is garbled
            # because SYSMSG lookup code isn't loaded.
            cpu.pc = pc + 2
            continue

        # ─── $A086 — skip during boot (terminal control) AND during
        #      command dispatch when D6=$1C03 (DSK device I/O).
        #      Module code is already in RAM; the handler tries async disk
        #      reads via $A03A which hang because the device subsystem
        #      ($0404/$0408) is not initialized.
        if opcode == 0xA086:
            d6_val = cpu.d[6] & 0xFFFF
            if not comint_started:
                bypass_counts['a086'] += 1
                cpu.pc = pc + 2
                continue
            if d6_val == 0x1C03:
                bypass_counts['a086_dsk'] += 1
                # Return success: Z=1, skip opcode
                cpu.sr = (cpu.sr & 0xFF00) | 0x04
                cpu.pc = pc + 2
                continue

        # ─── Trace SRCH entry ($1C30) ───
        if pc == 0x1C30 and comint_started:
            bypass_counts['srch_entry'] = bypass_counts.get('srch_entry', 0) + 1
            if bypass_counts['srch_entry'] <= 5:
                d0 = cpu.d[0]
                d1 = cpu.d[1]
                a0 = cpu.a[0] & 0xFFFFFF
                a2 = cpu.a[2] & 0xFFFFFF
                a3 = cpu.a[3] & 0xFFFFFF
                a4 = cpu.a[4] & 0xFFFFFF
                a6 = cpu.a[6] & 0xFFFFFF
                sp = cpu.a[7] & 0xFFFFFF
                print(f"\n  SRCH ENTRY $1C30 #{bypass_counts['srch_entry']}:", flush=True, file=sys.stderr)
                print(f"    D0=${d0:08X} D1=${d1:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X}", flush=True, file=sys.stderr)
                print(f"    A0=${a0:06X} A2=${a2:06X} A3=${a3:06X} A4=${a4:06X} A6=${a6:06X} SP=${sp:06X}", flush=True, file=sys.stderr)
                # Show SYSBAS chain
                sysbas = bus.read_long(0x0414) & 0xFFFFFF
                jcb_chain = bus.read_long(INIT_JCB + 0x0C) & 0xFFFFFF if INIT_JCB else 0
                print(f"    SYSBAS=${sysbas:06X} JCB+$0C=${jcb_chain:06X}", flush=True, file=sys.stderr)

        # ─── Trace SRCH module loop ($1C6E) ───
        if pc == 0x1C6E and comint_started:
            bypass_counts['srch_loop'] = bypass_counts.get('srch_loop', 0) + 1
            if bypass_counts['srch_loop'] <= 10:
                a0 = cpu.a[0] & 0xFFFFFF
                a6 = cpu.a[6] & 0xFFFFFF
                # Read module type and RAD50 name at A6
                if a6 > 0 and a6 < 0x100000:
                    m_type = bus.read_word(a6 + 0x04)
                    m_r1 = bus.read_word(a6 + 0x06)
                    m_r2 = bus.read_word(a6 + 0x08)
                    m_r3 = bus.read_word(a6 + 0x0A)
                    m_link = bus.read_long(a6)
                    print(f"    SRCH LOOP $1C6E: A6=${a6:06X} type=${m_type:04X} "
                          f"name=${m_r1:04X}.${m_r2:04X}.${m_r3:04X} link=${m_link:08X}", flush=True, file=sys.stderr)

        # ─── Trace $A0DC (CMDINT) handler ───
        if opcode == 0xA0DC and comint_started:
            bypass_counts['a0dc'] = bypass_counts.get('a0dc', 0) + 1
            if bypass_counts['a0dc'] <= 5:
                a0 = cpu.a[0] & 0xFFFFFF
                a3 = cpu.a[3] & 0xFFFFFF
                a6 = cpu.a[6] & 0xFFFFFF
                print(f"\n  $A0DC CMDINT #{bypass_counts['a0dc']} at PC=${pc:06X}:", flush=True, file=sys.stderr)
                print(f"    A0=${a0:06X} A3=${a3:06X} A6=${a6:06X}", flush=True, file=sys.stderr)
                print(f"    D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X}", flush=True, file=sys.stderr)

        # ─── Trace $6240 (command search) ───
        if pc == 0x6240 and comint_started:
            bypass_counts['cmd_search_6240'] = bypass_counts.get('cmd_search_6240', 0) + 1
            if bypass_counts['cmd_search_6240'] <= 5:
                a0 = cpu.a[0] & 0xFFFFFF
                a3 = cpu.a[3] & 0xFFFFFF
                a6 = cpu.a[6] & 0xFFFFFF
                d0 = cpu.d[0]
                d1 = cpu.d[1]
                v0448 = bus.read_long(0x0448)
                print(f"\n  CMD SEARCH $6240 #{bypass_counts['cmd_search_6240']}:", flush=True, file=sys.stderr)
                print(f"    A0=${a0:06X} A3=${a3:06X} A6=${a6:06X}", flush=True, file=sys.stderr)
                print(f"    D0=${d0:08X} D1=${d1:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X}", flush=True, file=sys.stderr)
                print(f"    ($0448)=${v0448:08X}", flush=True, file=sys.stderr)
                # Dump A3 context area if valid
                if a3 > 0 and a3 < 0x100000:
                    print(f"    A3 context: +$00=${bus.read_word(a3):04X} +$02=${bus.read_word(a3+2):04X} "
                          f"+$04=${bus.read_word(a3+4):04X} +$06=${bus.read_long(a3+6):08X} "
                          f"+$0A=${bus.read_word(a3+0xA):04X} +$0C=${bus.read_long(a3+0xC):08X} "
                          f"+$14=${bus.read_word(a3+0x14):04X}", flush=True, file=sys.stderr)

        # ─── CMDLIN init RTS diagnostic ───
        if pc == 0x02010A and cmdlin_init_phase:
            sp = cpu.a[7] & 0xFFFFFF
            ret_addr = bus.read_long(sp) if sp < 0x100000 else 0
            print(f"  !!! CMDLIN init RTS at $02010A: SP=${sp:06X} ret=${ret_addr:08X} "
                  f"sentinel=${INIT_SENTINEL:06X}", flush=True, file=sys.stderr)

        # ─── Trace jump to CMDLIN module ───
        if comint_started and 0x020000 <= pc <= 0x030000:
            key = f'trace_cmdlin_{pc:06X}'
            bypass_counts[key] = bypass_counts.get(key, 0) + 1
            cnt = bypass_counts[key]
            if cnt <= 1 or (pc == 0x020058 and cnt in (1, 2, 100, 1000)):
                print(f"  CMDLIN ${pc:06X}: op=${opcode:04X} "
                      f"A4=${cpu.a[4]&0xFFFFFF:06X} A5=${cpu.a[5]&0xFFFFFF:06X} "
                      f"A6=${cpu.a[6]&0xFFFFFF:06X} D6=${cpu.d[6]:08X} "
                      f"SP=${cpu.a[7]&0xFFFFFF:06X}", flush=True, file=sys.stderr)

        # ─── Track last ROM PC before entering CMDLIN area ───
        if comint_started and pc < 0x020000:
            _last_rom_pc = pc
        elif comint_started and 0x020000 <= pc <= 0x030000:
            if bypass_counts.get('cmdlin_entry_logged', 0) == 0:
                bypass_counts['cmdlin_entry_logged'] = 1
                print(f"  >>> CMDLIN ENTRY: from ROM ${_last_rom_pc:06X} → ${pc:06X}", flush=True, file=sys.stderr)
                sp = cpu.a[7] & 0xFFFFFF
                print(f"      A5=${cpu.a[5]&0xFFFFFF:06X} SP=${sp:06X}", flush=True, file=sys.stderr)

        # ─── Trace COMINT dispatch flow $4BAA-$4BF0 ───
        if comint_started and 0x4BA0 <= pc <= 0x4BF0:
            key = f'trace_4bxx_{pc:04X}'
            bypass_counts[key] = bypass_counts.get(key, 0) + 1
            if bypass_counts[key] <= 2:
                a4 = cpu.a[4] & 0xFFFFFF
                a5 = cpu.a[5] & 0xFFFFFF
                d5 = cpu.d[5]
                d7 = cpu.d[7]
                print(f"  DISPATCH ${pc:06X}: op=${opcode:04X} A4=${a4:06X} A5=${a5:06X} "
                      f"D5=${d5:08X} D7=${d7:08X}", flush=True, file=sys.stderr)
                if pc == 0x4BAA:
                    # Show the ext comparison
                    ext_val = bus.read_word(a4 + 0x0A)
                    print(f"    A4+$0A=${ext_val:04X} (compare with $4C7C=LIT)", flush=True, file=sys.stderr)

        # ─── $4BE6 JMP (A5) — Python-side command handler ───
        # CMDLIN init can't populate its command table (AMOS system services
        # unavailable), so we handle commands directly in Python.
        if pc == 0x4BE6 and opcode == 0x4ED5 and comint_started:
            a5 = cpu.a[5] & 0xFFFFFF
            if a5 == cmdlin_desc_addr and cmdlin_desc_addr:
                bypass_counts['jmp_a5_fix'] = bypass_counts.get('jmp_a5_fix', 0) + 1
                def _r50(w):
                    c = ' ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789'
                    c3 = w % 40; w //= 40; c2 = w % 40; w //= 40; c1 = w % 40
                    return c[c1] + c[c2] + c[c3]
                cmd_name = _r50(last_find_rad50).strip()
                output = _handle_command(cmd_name, last_find_rad50)
                # Write prompt + command output (native TTYOUT suppressed)
                sys.stdout.buffer.write(b'.')
                sys.stdout.buffer.write(output.encode('ascii'))
                sys.stdout.buffer.flush()
                if _bye_requested:
                    print(f"\n  BYE command — halting emulator", flush=True, file=sys.stderr)
                    break
                # Return to COMINT via $A0DC (CMDINT).
                cpu.pc = 0x470A
                if bypass_counts['jmp_a5_fix'] <= 10:
                    print(f"  CMD {cmd_name}: handled, restarting COMINT via $A0DC at $470A", flush=True, file=sys.stderr)
                continue

        # ─── Trace dispatch path ───
        if comint_started and 0x5A00 <= pc <= 0x5F00:
            key = f'trace_5xxx_{pc:04X}'
            bypass_counts[key] = bypass_counts.get(key, 0) + 1
            if bypass_counts[key] <= 2:
                print(f"  TRACE ${pc:06X}: opcode=${opcode:04X} "
                      f"A4=${cpu.a[4]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X} "
                      f"D0=${cpu.d[0]:08X} D7=${cpu.d[7]:08X}", flush=True, file=sys.stderr)

        # ─── $5A9C module dispatch intercept ───
        # At $5AB6: TST.L desc+$28 — skip this check for our modules
        # so the code falls through to the init path (where A5 gets set up).
        # At $5AE0: MOVEA.L desc+$24,A1 — intercept and jump directly
        # to the module's code entry, bypassing the driver init.
        if pc == 0x5AB6 and comint_started:
            a4 = cpu.a[3] & 0xFFFFFF  # A4 = descriptor (68k reg 12)
            # Note: cpu.a[] indexing — A4 is cpu.a[4] in some impls
            a4 = cpu.a[4] & 0xFFFFFF
            if a4 in module_entries:
                # Skip TST.L (4 bytes) + BNE (4 bytes) = 8 bytes
                cpu.pc = 0x5ABE
                bypass_counts['dispatch_skip_5ab6'] += 1
                if bypass_counts['dispatch_skip_5ab6'] <= 5:
                    print(f"  DISPATCH $5AB6: skip desc+$28 test for module at ${a4:06X}", flush=True, file=sys.stderr)
                continue

        if pc == 0x5AE0 and comint_started:
            a4 = cpu.a[4] & 0xFFFFFF
            if a4 in module_entries:
                code_entry = module_entries[a4]
                cpu.pc = code_entry
                bypass_counts['dispatch_5ae0'] += 1
                if bypass_counts['dispatch_5ae0'] <= 5:
                    print(f"  DISPATCH $5AE0: jumping to code at ${code_entry:06X} "
                          f"A5=${cpu.a[5] & 0xFFFFFF:06X}", flush=True, file=sys.stderr)
                continue

        # ─── Exception handler trace ───
        # Illegal instruction handler at $0F96, Line-F at $10BC
        # Capture the faulting PC from the exception frame
        if pc in (0x0F96, 0x10BC, 0x0F58) and comint_started:
            vec_name = {0x0F96: "ILLEGAL", 0x10BC: "LINE-F", 0x0F58: "ADDR_ERR"}[pc]
            # Exception frame: SR at (SP), PC at (SP+2)
            sp = cpu.a[7] & 0xFFFFFF
            frame_sr = bus.read_word(sp)
            frame_pc = bus.read_long(sp + 2)
            bypass_counts[f'exc_{vec_name}'] += 1
            if bypass_counts[f'exc_{vec_name}'] <= 5:
                # Try to read the faulting instruction
                try:
                    fault_word = bus.read_word(frame_pc)
                except:
                    fault_word = 0xDEAD
                print(f"\n  *** {vec_name} EXCEPTION at PC=${frame_pc:06X} "
                      f"opcode=${fault_word:04X} SR=${frame_sr:04X} SP=${sp:06X}", flush=True, file=sys.stderr)
                # Show some context
                print(f"      D0-D3: ${cpu.d[0]:08X} ${cpu.d[1]:08X} ${cpu.d[2]:08X} ${cpu.d[3]:08X}", flush=True, file=sys.stderr)
                print(f"      A0-A3: ${cpu.a[0]:08X} ${cpu.a[1]:08X} ${cpu.a[2]:08X} ${cpu.a[3]:08X}", flush=True, file=sys.stderr)
                print(f"      A4-A6: ${cpu.a[4]:08X} ${cpu.a[5]:08X} ${cpu.a[6]:08X}", flush=True, file=sys.stderr)

        # Handshake bypass
        if pc == 0x006D80:
            bypass_counts['handshake'] += 1
            cpu.pc = 0x006DDE
            cpu.sr = (cpu.sr & 0xFF00) | 0x04
            continue

        # ─── $A03E yield ───
        if opcode == 0xA03E:
            d6 = cpu.d[6] & 0xFFFF
            bypass_counts[f'a03e_d6_{d6:02X}'] += 1

            # During CMDLIN init: suppress ALL yields to prevent scheduler
            # from switching context and losing our sentinel return address
            if cmdlin_init_phase:
                cpu.pc = pc + 2
                continue

        # ─── CMDLIN init: force registration path ───
        # At $02009E (desc+$0092): BNE $0200F0 checks if char class setup succeeded.
        # If data+$526=$85 (success), BNE taken → skips command registration!
        # Force Z=1 so BNE is NOT taken, allowing registration to proceed.
        if cmdlin_init_phase and pc == 0x02009E:
            cpu.sr = (cpu.sr & 0xFF00) | 0x04  # Z=1
            print(f"  [INIT] Forced Z=1 at $02009E to enter registration path", flush=True, file=sys.stderr)

            if comint_started:
                if d6 == 2:
                    if ini_data_pending:
                        bus.write_word(TCB_ADDR + 0x00, 0x0009)
                        ini_data_pending = False
                    elif cmd_index >= len(test_commands) and bypass_counts.get('a03e_d6_02', 0) > 20:
                        print(f"\n=== Halting at step {step:,} ===", flush=True, file=sys.stderr)
                        break

                # Maintain JOBCUR
                if INIT_JCB and bus.read_long(0x041C) == 0:
                    bus.write_long(0x041C, INIT_JCB)

            # Skip $A03E after synchronous disk I/O
            if disk_a03c_just_skipped and boot_complete:
                cpu.pc = (pc + 2) & 0xFFFFFFFF
                continue

        # Clear disk I/O flag on any non-$A03E instruction
        if opcode != 0xA03E:
            disk_a03c_just_skipped = False

        # After init → set up COMINT
        if pc == 0x001C56 and not comint_started and INIT_JCB:
            bypass_counts['sched_idle'] += 1
            if bypass_counts.get('a086', 0) >= 5:
                comint_started = True
                print(f"\n=== COMINT setup at step {step:,} ===", flush=True, file=sys.stderr)
                jcb = INIT_JCB
                for i in range(0, 0x70, 2):
                    bus.write_word(TCB_ADDR + i, 0)
                bus.write_long(TCB_ADDR + 0x1A, TCB_BUF_SIZE)
                bus.write_long(TCB_ADDR + 0x44, TCB_BUF_ADDR)
                bus.write_long(TCB_ADDR + 0x48, TCB_BUF_SIZE)
                for i in range(0, TCB_BUF_SIZE, 2):
                    bus.write_word(TCB_BUF_ADDR + i, 0)
                bus.write_long(jcb + 0x38, TCB_ADDR)
                bus.write_word(jcb + 0x20, 0)
                bus.write_long(jcb + 0x104, 0)

                # Set JCB+$14 = nonzero (privilege/login status)
                # COMINT at $4AFA checks TST.W $14(A0) and takes error path
                # if zero. $0102 = normal logged-in user privilege.
                bus.write_word(jcb + 0x14, 0x0102)
                print(f"  JCB+$14 = $0102 (user privilege)", flush=True, file=sys.stderr)

                # Load system modules and set up SRCH chain
                module_chain = load_sys_modules()
                if module_chain:
                    bus.write_long(0x0414, module_chain)  # SYSBAS
                    print(f"  SYSBAS=${module_chain:06X} (module chain loaded)", flush=True, file=sys.stderr)
                bus.write_long(jcb + 0x0C, module_chain if module_chain else 0)
                bus.write_long(jcb + 0x78, 0)

                # Fix CMDLIN descriptor fields for proper $A080 initialization
                # desc+$24 and desc+$28 contain raw code bytes from the file.
                # $A080 checks desc+$28: nonzero = "already initialized" → skips init.
                # We must zero them so $A080 (or our manual init) works correctly.
                if cmdlin_desc_addr:
                    bus.write_long(cmdlin_desc_addr + 0x24, 0)  # init code vector
                    bus.write_long(cmdlin_desc_addr + 0x28, 0)  # initialized flag
                    print(f"  CMDLIN desc+$24/$28 zeroed for init", flush=True, file=sys.stderr)

                    # Allocate and clear CMDLIN data area (separate from module code!)
                    for i in range(0, CMDLIN_DATA_SIZE, 2):
                        bus.write_word(CMDLIN_DATA_ADDR + i, 0)
                    print(f"  CMDLIN data area at ${CMDLIN_DATA_ADDR:06X} ({CMDLIN_DATA_SIZE} bytes)", flush=True, file=sys.stderr)

                    # Launch CMDLIN init: save CPU state, redirect to init code
                    cmdlin_init_saved = {
                        'pc': cpu.pc, 'sr': cpu.sr,
                        'd': [cpu.d[i] for i in range(8)],
                        'a': [cpu.a[i] for i in range(8)],
                    }
                    # Set up registers for init code at $020032:
                    #   A5 = data area, A0 = JCB, A4 = descriptor
                    init_addr = cmdlin_desc_addr + 0x26  # desc+$08 = $002A → file+$002A → desc + ($2A-4) = desc+$26
                    cpu.a[5] = CMDLIN_DATA_ADDR
                    cpu.a[0] = jcb
                    cpu.a[4] = cmdlin_desc_addr
                    cpu.a[2] = 0
                    cpu.a[3] = 0
                    cpu.d[7] = 0
                    # Push sentinel return address on stack
                    sp = cpu.a[7]
                    sp -= 4
                    bus.write_long(sp, INIT_SENTINEL)
                    cpu.a[7] = sp
                    cpu.pc = init_addr
                    cmdlin_init_phase = True
                    print(f"  CMDLIN init launched: PC=${init_addr:06X} A5=${CMDLIN_DATA_ADDR:06X}", flush=True, file=sys.stderr)
                    # Continue main loop — init code will execute with all LINE-A handlers active

                # Load SYSMSG.USA into memory for message lookup
                sysmsg_addr = load_sysmsg()
                if sysmsg_addr:
                    sysmsg_block_addr = sysmsg_addr
                    sysmsg_loaded = True

                # Create zeroed sysdata block for JCB+$D0
                # $A068 MATCH reads JCB+$D0+$45 (special chars), +$56 (alpha ext),
                # +$74 (ERSATZ table). Raw SYSMSG data at those offsets is wrong.
                for i in range(0, 0x100, 2):
                    bus.write_word(SYSDATA_ADDR + i, 0)
                bus.write_long(jcb + 0xD0, SYSDATA_ADDR)
                print(f"  JCB+$D0 = ${SYSDATA_ADDR:06X} (zeroed sysdata)", flush=True, file=sys.stderr)
                if sysmsg_addr:
                    print(f"  SYSMSG at ${sysmsg_addr:06X} (for message lookup intercept)", flush=True, file=sys.stderr)
                status = bus.read_word(jcb)
                bus.write_word(jcb, status | 0x2000)
                bus.write_long(0x041C, jcb)
                safe_sp = 0x8700
                bus.write_word(safe_sp - 6, 0x2000)
                bus.write_long(safe_sp - 4, COMINT_ENTRY)
                bus.write_long(jcb + 0x80, safe_sp - 6)
                bus.write_long(jcb + 0x7C, safe_sp)
                sys_dispatch = bus.read_long(0x0514)
                print(f"  JCB=${jcb:08X} JOBCUR=${bus.read_long(0x041C):08X}", flush=True, file=sys.stderr)
                print(f"  ($0514) dispatch vector=${sys_dispatch:08X}", flush=True, file=sys.stderr)
                if sys_dispatch and sys_dispatch < 0x100000:
                    print(f"  dispatch+$04=${bus.read_word(sys_dispatch + 4):04X} "
                          f"dispatch+$06=${bus.read_word(sys_dispatch + 6):04X}", flush=True, file=sys.stderr)

        # $A072 (terminal read)
        if opcode == 0xA072 and comint_started:
            bypass_counts['a072'] += 1
            term = bus.read_long(bus.read_long(0x041C) + 0x38) if bus.read_long(0x041C) else TCB_ADDR
            if term and term < 0x100000:
                rptr = bus.read_long(term + 0x1E)
                count = bus.read_word(term + 0x12)
                if count > 0 and rptr and rptr < 0x100000:
                    ch = bus.read_byte(rptr)
                    bus.write_long(term + 0x1E, rptr + 1)
                    bus.write_word(term + 0x12, count - 1)
                    cpu.d[1] = (cpu.d[1] & 0xFFFFFF00) | ch
                else:
                    cpu.d[1] = (cpu.d[1] & 0xFFFFFF00) | 0
            cpu.pc = pc + 2
            continue

        # $A008 (TTYLIN)
        if opcode == 0xA008 and comint_started:
            bypass_counts['a008'] += 1
            print(f"\n  [step {step:,}] $A008 TTYLIN #{bypass_counts['a008']}", flush=True, file=sys.stderr)
            input_injected = False
            ini_data_pending = False
            if cmd_index < len(test_commands):
                last_cmd_line = test_commands[cmd_index]
                inject_input_to_tcb(test_commands[cmd_index])
                print(f"    Injected: {test_commands[cmd_index]!r}", flush=True, file=sys.stderr)
                cmd_index += 1

        # Maintain JOBCUR
        if comint_started and INIT_JCB:
            if bus.read_long(0x041C) == 0:
                bus.write_long(0x041C, INIT_JCB)

        # Loop detection
        if pc == last_pc:
            stuck_count += 1
            if stuck_count >= 10000:
                key = f"loop_{pc:06X}"
                bypass_counts[key] += 1
                if bypass_counts[key] <= 3:
                    try:
                        dis, _ = disassemble_one(bus, pc)
                    except:
                        dis = "???"
                    print(f"  LOOP BREAK ${pc:06X}: {dis}", flush=True, file=sys.stderr)
                    print(f"    D0-D3: ${cpu.d[0]:08X} ${cpu.d[1]:08X} ${cpu.d[2]:08X} ${cpu.d[3]:08X}", flush=True, file=sys.stderr)
                    print(f"    A0-A3: ${cpu.a[0]:08X} ${cpu.a[1]:08X} ${cpu.a[2]:08X} ${cpu.a[3]:08X}", flush=True, file=sys.stderr)
                cpu.d[7] = cpu.d[7] | 0xFF
                stuck_count = 0
        else:
            stuck_count = 0
            last_pc = pc

        if pc >= 0x100000 and pc < 0x800000:
            print(f"\n*** CRASH at ${pc:06X} ***", flush=True, file=sys.stderr)
            print(f"  A7=${cpu.a[7]:08X} SR=${cpu.sr:04X}", flush=True, file=sys.stderr)
            break

        if step > 0 and step % 5_000_000 == 0:
            print(f"\n  [step {step:,}] PC=${pc:06X} JOBCUR=${bus.read_long(0x041C):08X}", flush=True, file=sys.stderr)
            print(f"  Bypasses: {dict(bypass_counts)}", flush=True, file=sys.stderr)

        cycles = cpu.step()
        bus.tick(cycles)
        step += 1

except KeyboardInterrupt:
    print(f"\n*** Interrupted at step {step:,} ***", flush=True, file=sys.stderr)
except Exception as e:
    print(f"\n*** Exception at step {step:,}: {e} ***", flush=True, file=sys.stderr)
    import traceback
    traceback.print_exc()

print(f"\nFinal: step {step:,} PC=${cpu.pc:06X}", flush=True, file=sys.stderr)
print(f"Bypasses: {dict(bypass_counts)}", flush=True, file=sys.stderr)
print(f"MEMBAS=${bus.read_long(0x0430):08X} JOBCUR=${bus.read_long(0x041C):08X}", flush=True, file=sys.stderr)

# Check disk state
zsydsk = bus.read_long(0x040C)
print(f"ZSYDSK=${zsydsk:08X}", flush=True, file=sys.stderr)
if zsydsk and zsydsk < 0x100000:
    dkflg = bus.read_byte(zsydsk)
    print(f"DK.FLG=${dkflg:02X}", flush=True, file=sys.stderr)

# Dump captured output chars
print(f"\n=== Output chars ({len(output_chars)} total) ===", flush=True, file=sys.stderr)
post_comint = [c for c in output_chars if c[2]]
# Show all post-COMINT chars with their LINE-A opcode
from collections import defaultdict
# Group by opcode — show which opcode has the text
opcode_chars = defaultdict(list)
for step_n, ch, _, opcode_n in post_comint:
    if ch >= 0x20 and ch < 0x7F:
        opcode_chars[opcode_n].append(chr(ch))

print(f"Post-COMINT text by LINE-A opcode:", flush=True, file=sys.stderr)
for op in sorted(opcode_chars.keys()):
    text = "".join(opcode_chars[op])
    print(f"  ${op:04X} ({len(opcode_chars[op])} chars): {text!r}", flush=True, file=sys.stderr)
