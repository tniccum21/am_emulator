"""AlphaSim main entry point — wire system and run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import os
import select
import termios
import tty

from .config import SystemConfig
from .bus.memory_bus import MemoryBus
from .cpu.mc68010 import MC68010
from .cpu.opcodes import build_opcode_table
from .devices.ram import RAM
from .devices.rom import ROM
from .devices.led import LED
from .devices.config_dip import ConfigDIP
from .devices.sasi import SASIController
from .devices.acia6850 import ACIA6850
from .devices.primary_serial_setup import PrimarySerialSetup
from .devices.timer6840 import Timer6840
from .devices.rtc_direct_bank import RTCDirectBank
from .devices.rtc_msm5832 import RTC_MSM5832
from .devices.scsi_bus import SCSIBusInterface
from .storage.disk_image import DiskImage
from .storage.scsi_target import SCSITarget
from .cpu.accelerators import LoopAccelerator
from .devices.serial_driver import DRIVER_BASE, assemble_driver
from .storage.amos_fs import read_file as amos_read_file, _rad50_encode, _read_word_le
from .debug.trace import TraceLogger
from .cpu.disassemble import disassemble_one


def build_system(config: SystemConfig) -> tuple[MC68010, MemoryBus, LED, ACIA6850]:
    """Instantiate and wire all components. Returns (cpu, bus, led)."""
    bus = MemoryBus()

    # RAM
    ram = RAM(config.ram_size)
    bus.set_ram(ram)

    # ROM (interleaved EPROM pair)
    rom = ROM(config.rom_even_path, config.rom_odd_path)
    bus.set_rom(rom)

    # LED display at $FE00 (absolute short → $FFFE00 on 24-bit bus)
    led = LED()
    bus.register_device(0xFFFE00, 0xFFFE00, led)

    # Config DIP switch at $FE03 (absolute short → $FFFE03)
    dip = ConfigDIP(config.config_dip)
    bus.register_device(0xFFFE03, 0xFFFE03, dip)

    # MSM5832 RTC at $FFFE04-$FFFE05
    rtc = RTC_MSM5832()
    bus.register_device(0xFFFE04, 0xFFFE05, rtc)

    # Native AMOSL.MON also probes a direct-mapped clock/date bank here.
    rtc_direct = RTCDirectBank()
    bus.register_device(0xFFFE40, 0xFFFE5F, rtc_direct)

    # MC6840 PTM timer at $FFFE10-$FFFE1F (odd byte addresses)
    timer = Timer6840()
    bus.register_device(0xFFFE10, 0xFFFE1F, timer)

    # SCSI bus interface at $FFFFC8-$FFFFC9 (used by OS driver SCZ.DVR)
    scsi_bus = SCSIBusInterface()
    bus.register_device(0xFFFFC8, 0xFFFFC9, scsi_bus)

    # Primary serial register block.
    # The ROM self-test uses $FFFE20/$24/$30 as the three port bases and
    # writes a separate setup sequence to $FFFE28.
    acia = ACIA6850()
    bus.register_device(0xFFFE20, 0xFFFE26, acia)
    bus.register_device(0xFFFE30, 0xFFFE32, acia)
    serial_setup = PrimarySerialSetup()
    bus.register_device(0xFFFE28, 0xFFFE28, serial_setup)

    # SASI/SCSI controller at $FFFFE0-$FFFFE7 (used by boot ROM only)
    sasi = SASIController()
    bus.register_device(0xFFFFE0, 0xFFFFE7, sasi)

    # Connect disk image if provided
    if config.disk_image_path and config.disk_image_path.exists():
        disk = DiskImage(config.disk_image_path)
        target = SCSITarget(disk)
        sasi.target = target
        scsi_bus.target = target  # Same disk for OS driver

    # CPU
    cpu = MC68010(bus, cpu_model=config.cpu_model)
    cpu.opcode_table = build_opcode_table()

    # Wire DMA references for SCSI bus interface
    scsi_bus._dma_bus = bus
    scsi_bus._dma_cpu = cpu

    return cpu, bus, led, acia


def _patch_boot_monitor_override(bus: MemoryBus, override: str) -> None:
    """Patch the ROM's inline OS monitor lookup filename.

    The AM-178 ROM embeds the SYS:[1,4] monitor name as inline RAD50
    words at CPU addresses $800182-$800186.  Overriding that name lets
    us test alternate monitor builds without modifying the disk image.
    """
    rom = getattr(bus, "_rom", None)
    if rom is None or not hasattr(rom, "data"):
        raise ValueError("ROM patch requested but no mutable ROM is present")

    stem, dot, ext = override.partition(".")
    if not dot:
        ext = "MON"
    stem = stem.strip().upper()
    ext = ext.strip().upper()
    if not stem or len(stem) > 6 or len(ext) > 3:
        raise ValueError(
            f"Invalid boot monitor override {override!r}; use NAME[.EXT] with up to 6.3 chars"
        )

    words = (
        _rad50_encode(stem[:3]),
        _rad50_encode(stem[3:6]),
        _rad50_encode(ext[:3]),
    )
    for i, word in enumerate(words):
        offset = 0x0182 + i * 2
        rom.data[offset] = word & 0xFF
        rom.data[offset + 1] = (word >> 8) & 0xFF

    sys.stderr.write(
        f"[BOOT] ROM OS filename override -> {stem}.{ext or 'MON'}\n"
    )


def _setup_terminal() -> list | None:
    """Set terminal to raw mode for character-at-a-time I/O.

    Returns the original terminal settings for restoration, or None
    if stdin is not a TTY.
    """
    if not os.isatty(sys.stdin.fileno()):
        return None
    old_settings = termios.tcgetattr(sys.stdin)
    tty.setraw(sys.stdin.fileno())
    return old_settings


def _restore_terminal(old_settings: list | None) -> None:
    """Restore terminal settings."""
    if old_settings is not None:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


def _check_stdin() -> bytes:
    """Non-blocking read from stdin. Returns available bytes or empty."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            return os.read(sys.stdin.fileno(), 64)
    except (ValueError, OSError):
        pass
    return b""


def run(config: SystemConfig) -> None:
    """Build system and run the emulation loop."""
    cpu, bus, led, acia = build_system(config)
    if config.boot_monitor_override:
        _patch_boot_monitor_override(bus, config.boot_monitor_override)
    compat_mode = config.boot_mode == "compat"
    native_find_trace = (
        config.boot_mode == "native" and config.native_find_trace
    )
    native_dispatch_trace = (
        config.boot_mode == "native" and config.native_dispatch_trace
    )
    native_trace_enabled = native_find_trace or native_dispatch_trace

    # Loop accelerator — speeds up tight delay/division loops
    accel = LoopAccelerator(bus)
    scsi_bus = next(
        (
            device for _, _, device in bus._devices
            if isinstance(device, SCSIBusInterface)
        ),
        None,
    )

    # Pre-load SYSMSG.USA from disk image (for dev spec entry).
    # This is equivalent to what MONGEN embeds at system generation.
    _sysmsg_data = None
    if config.disk_image_path and config.disk_image_path.exists():
        from .storage.disk_image import DiskImage as _DI
        _tmp_disk = _DI(config.disk_image_path)
        _sysmsg_data = amos_read_file(_tmp_disk, (1, 4), "SYSMSG", "USA")
        if _sysmsg_data:
            sys.stderr.write(
                f"[SYS] SYSMSG.USA: {len(_sysmsg_data)} bytes from disk\n"
            )

    # Open disk image for direct Python I/O (IOINI intercept).
    _disk_img = None
    if config.disk_image_path and config.disk_image_path.exists():
        _disk_img = DiskImage(config.disk_image_path)

    # One-shot hook: when $0462 contains the dummy driver ($632C),
    # inject serial driver code into unused RAM at $00B800 and patch
    # the vector.  Must happen after OS disk load (which fills most RAM)
    # but before terminal detect runs.
    _driver_installed = [False]
    _drv_code = assemble_driver()
    _need_magic_init = [False]  # deferred file channel magic init
    # (DEVSPEC removed — was blocking FIND's disk search path)
    _chain_seen = [False]  # CHAIN ($A008) seen — enables AMOS32 patch
    _ini_patched = [False]  # AMOS32→AMOSL patch done
    _ddb_populated = [False]  # DDB/DDT populated with disk geometry
    _ini_injected = [False]  # AMOSL.INI data fed to ACIA

    # Pre-read AMOSL.INI from disk for injection after CHAIN.
    # With MEMBAS set, CHAIN yields immediately (D6=2) without calling
    # SCNFIL — the AMOS32→AMOSL filename patch never fires.  Instead,
    # we read the INI file from disk and feed it as terminal input so
    # COMINT processes each line as a command.
    _ini_data = None
    if _disk_img:
        _ini_data = amos_read_file(_disk_img, (1, 4), "AMOSL", "INI")
        if _ini_data:
            sys.stderr.write(
                f"[INI] Pre-read AMOSL.INI: {len(_ini_data)} bytes\n"
            )

    # DDB/DDT population constants (MONGEN-equivalent disk geometry)
    DISK_DRV_BASE = 0xB700
    IO_BUF = 0x29000
    CMD_LOAD_BASE = 0x40000   # Buffer for command program loading (FIND D6=1)
    FILE_DATA_BASE = 0x30000  # Buffer for FETCH data loading (FIND D6=0/2)

    # Raw disk bytes for direct file loading (FIND bypass)
    _raw_disk = None
    if config.disk_image_path and config.disk_image_path.exists():
        _raw_disk = config.disk_image_path.read_bytes()

    # FIND bypass helpers
    _find_count = [0]

    def _rad50_decode(w: int) -> str:
        chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
        c3 = w % 40; w //= 40; c2 = w % 40; w //= 40; c1 = w % 40
        return chars[c1] + chars[c2] + chars[c3]

    def _find_file_on_disk(name1: int, name2: int, ext_w: int, ppn_w: int):
        """Search AMOS disk directory for a file. Returns (start_block, size, attr) or None."""
        if not _disk_img:
            return None
        mfd = _disk_img.read_sector(2)
        if not mfd:
            return None
        for off in range(0, 504, 8):
            entry_ppn = _read_word_le(mfd, off)
            if entry_ppn == 0:
                break
            if entry_ppn == ppn_w:
                ufd_block = _read_word_le(mfd, off + 2)
                block = ufd_block
                for _ in range(200):
                    sector = _disk_img.read_sector(block + 1)
                    if not sector:
                        break
                    link = _read_word_le(sector, 0)
                    soff = 2
                    while soff + 12 <= 512:
                        w0 = _read_word_le(sector, soff)
                        w1 = _read_word_le(sector, soff + 2)
                        w2 = _read_word_le(sector, soff + 4)
                        if w0 == 0 and w1 == 0:
                            break
                        if w0 == name1 and w1 == name2 and w2 == ext_w:
                            attr = _read_word_le(sector, soff + 6)
                            size = _read_word_le(sector, soff + 8)
                            start = _read_word_le(sector, soff + 10)
                            return (start, size, attr)
                        soff += 12
                    if link == 0:
                        break
                    block = link
        return None

    def _try_find_file(name1: int, name2: int):
        """Try to find a file with common extensions."""
        for ext_w in [_rad50_encode("LIT"), _rad50_encode("CMD"), _rad50_encode("RUN")]:
            for ppn_w in [0x0104, 0x0B0B, 0x0202, 0x010B, 0x0106]:
                result = _find_file_on_disk(name1, name2, ext_w, ppn_w)
                if result:
                    return result, ext_w, ppn_w
        return None, 0, 0

    def _load_file_data(start_block: int, size_blocks: int, dest_addr: int) -> int:
        """Load file data into RAM, stripping link words from each block.

        AMOS files have a 2-byte link word at the start of each 512-byte block.
        Each block contributes 510 bytes of data. Contiguous file convention:
        if first block's link = 0, file is contiguously allocated.
        """
        if not _raw_disk:
            return 0
        block = start_block
        write_pos = 0
        max_blocks = max(size_blocks, 500)
        for i in range(max_blocks):
            lba = block + 1
            byte_offset = lba * 512
            if byte_offset + 512 > len(_raw_disk):
                break
            link = _raw_disk[byte_offset] | (_raw_disk[byte_offset + 1] << 8)
            for j in range(2, 512):
                bus.dma_write_byte(dest_addr + write_pos, _raw_disk[byte_offset + j])
                write_pos += 1
            if link == 0:
                if i == 0:
                    block = start_block + 1  # contiguous: continue from start+1
                else:
                    break
            else:
                block = link
        return write_pos

    # Terminal I/O bridging state — models what ATRS.DVR's ISR does:
    # transfers data between ACIA hardware and OS terminal buffers.
    _jobcur_addr = [0]  # JCB address of the init job (learned at yield)
    _tcb_addr = [0]     # TCB address (learned from JCB+$38)
    _ioini_trace_count = [0]
    _a072_dbg = [0]
    _ttyout_count = [0]
    _srch_trace = [False]
    _srch_trace_lines = [0]
    _a052_trace = [False]
    _a052_count = [0]
    _a054_trace = [False]
    _a054_count = [0]
    _jcb_devspec_set = [False]
    _find_path_count = [0]
    _native_trace_armed = [False]
    _native_trace_count = [0]
    _native_trace_truncated = [False]
    _native_trace_limit = 800 if native_dispatch_trace else 400
    _native_zsydsk_snapshot = [None]
    _native_watch = {
        0x0400: None,  # SYSTEM
        0x0404: None,  # DEVTBL
        0x0408: None,  # DDBCHN
        0x040C: None,  # ZSYDSK
        0x041C: None,  # JOBCUR
        0x0430: None,  # MEMBAS
        0x0438: None,  # MEMSIZ
        0x0462: None,  # serial driver vector
        0x0564: None,  # low-stage gate state
    }
    _native_watch_labels = {
        0x0400: "SYSTEM",
        0x0404: "DEVTBL",
        0x0408: "DDBCHN",
        0x040C: "ZSYDSK",
        0x041C: "JOBCUR",
        0x0430: "MEMBAS",
        0x0438: "MEMSIZ",
        0x0462: "DRVVEC",
        0x0564: "W0564",
    }
    _native_recent_watch: dict[int, list[tuple[int, int, int, int, str]]] = {
        0x0400: [],
        0x0564: [],
    }
    _native_dispatch_hits: set[int] = set()
    _native_exec_hits = {"scz": 0, "b440": 0}
    _native_dispatch_labels = {
        0x0019CC: "CLEAR-ZSTATE",
        0x00199C: "ZSTATE-CALLBACK-DISPATCH",
        0x001A14: "QUEUE-ZSTATE",
        0x001B9E: "DISK-CALLBACK",
        0x001AEA: "LINK-ZSTATE",
        0x001B00: "IOINI-CALLBACK",
        0x001C0A: "DDT-SET",
        0x001D10: "IOINI-ENTRY",
        0x001D42: "DDT-CLEAR",
        0x001D5C: "IOINI-QUEUE",
        0x001D6A: "IOINI-STORE",
        0x001D72: "IOINI-MARK",
        0x0022FE: "ZSTATE-FREE-RESET",
        0x00236A: "DDBCHN-LINK",
        0x0022E2: "IO-DISPATCH",
        0x002322: "ZSTATE-FREE-PUSH",
        0x003EBE: "SYSTEM-GATE",
        0x003F7A: "RTC-DATE-PATH",
        0x005FBC: "MOUNT-UNLINK-PREP",
        0x005FBE: "MOUNT-UNLINK-CALL",
        0x005FC2: "MOUNT-UNLINK-WRITE",
        0x005FC4: "DDBCHN-UNLINK",
        0x005FD0: "MOUNT-UNLINK-MATCH",
        0x005FE0: "MOUNT-IOINI",
        0x0062E4: "DISPATCH-JSR-A6",
        0x00A2EA: "SCZ-A034",
        0x00A308: "SCZ-A044",
    }

    def _emit_native_trace(msg: str, *, arm: bool = False) -> None:
        if not native_trace_enabled:
            return
        if arm:
            _native_trace_armed[0] = True
        if not _native_trace_armed[0]:
            return
        if _native_trace_count[0] >= _native_trace_limit:
            if not _native_trace_truncated[0]:
                _native_trace_truncated[0] = True
                sys.stderr.write(
                    "[NTRACE] trace limit reached; suppressing further events\n"
                )
            return
        sys.stderr.write(
            f"[NTRACE] cyc={cpu.cycles:08d} pc=${cpu.pc & 0xFFFFFF:06X} {msg}\n"
        )
        _native_trace_count[0] += 1

    def _trace_native_watchpoints() -> None:
        if not native_trace_enabled:
            return
        for addr, prev in _native_watch.items():
            value = bus.read_long(addr) & 0xFFFFFFFF
            if prev is None:
                _native_watch[addr] = value
                continue
            if value != prev:
                _native_watch[addr] = value
                try:
                    disasm, _ = disassemble_one(bus, cpu.pc & 0xFFFFFF)
                except Exception:
                    disasm = "???"
                if addr in _native_recent_watch:
                    recent = _native_recent_watch[addr]
                    recent.append(
                        (cpu.cycles, cpu.pc & 0xFFFFFF, prev, value, disasm)
                    )
                    if len(recent) > 12:
                        del recent[0]
                _emit_native_trace(
                    f"SYSVAR {_native_watch_labels[addr]} ${addr:04X} "
                    f"${prev:08X} -> ${value:08X} via {disasm}"
                )

    def _dump_native_recent_watch(addr: int) -> None:
        label = _native_watch_labels.get(addr, f"${addr:04X}")
        recent = _native_recent_watch.get(addr, [])
        if not recent:
            _emit_native_trace(f"{label}-HIST none")
            return
        for cyc, pc_hist, prev, value, disasm in recent:
            _emit_native_trace(
                f"{label}-HIST cyc={cyc:08d} pc=${pc_hist:06X} "
                f"${prev:08X} -> ${value:08X} via {disasm}"
            )

    def _dump_native_ddb(ddb_addr: int, label: str) -> None:
        if not (0x1000 <= ddb_addr < 0x100000):
            _emit_native_trace(f"{label} invalid ${ddb_addr:06X}")
            return
        fields = (
            ("LINK", 0x00),
            ("DDT", 0x08),
            ("BPS", 0x0C),
            ("SPT", 0x10),
            ("SPC", 0x14),
            ("MFD", 0x20),
            ("BMP", 0x24),
            ("PAR", 0x28),
            ("SIZ", 0x2C),
            ("BUF", 0x7C),
        )
        parts = [
            f"{name}=${bus.read_long(ddb_addr + off) & 0xFFFFFFFF:08X}"
            for name, off in fields
        ]
        _emit_native_trace(f"{label} @${ddb_addr:06X} " + " ".join(parts))
        ddt_addr = bus.read_long(ddb_addr + 0x08) & 0xFFFFFF
        if ddt_addr:
            _dump_native_ddt(ddt_addr, f"{label}-DDT")

    def _dump_native_words(addr: int, label: str, *, words: int = 8) -> None:
        if not (0x1000 <= addr < 0x100000):
            _emit_native_trace(f"{label} invalid ${addr:06X}")
            return
        data = " ".join(
            f"{bus.read_word(addr + off * 2):04X}"
            for off in range(words)
        )
        _emit_native_trace(f"{label} @${addr:06X} {data}")

    def _dump_native_raw_words(addr: int, label: str, *, words: int = 8) -> None:
        if not (0 <= addr < 0x100000):
            _emit_native_trace(f"{label} invalid ${addr:06X}")
            return
        data = " ".join(
            f"{bus.read_word(addr + off * 2):04X}"
            for off in range(words)
        )
        _emit_native_trace(f"{label} @${addr:06X} {data}")

    def _dump_native_ddt(ddt_addr: int, label: str) -> None:
        if not (0x1000 <= ddt_addr < 0x100000):
            _emit_native_trace(f"{label} invalid ${ddt_addr:06X}")
            return
        _emit_native_trace(
            f"{label} @${ddt_addr:06X} "
            f"STS=${bus.read_word(ddt_addr):04X} "
            f"FLG=${bus.read_word(ddt_addr + 0x02):04X} "
            f"DDB=${bus.read_long(ddt_addr + 0x04) & 0xFFFFFFFF:08X} "
            f"DRV=${bus.read_long(ddt_addr + 0x06) & 0xFFFFFFFF:08X} "
            f"DISP=${bus.read_long(ddt_addr + 0x0E) & 0xFFFFFFFF:08X} "
            f"INT=${bus.read_long(ddt_addr + 0x14) & 0xFFFFFFFF:08X} "
            f"NAM=${bus.read_word(ddt_addr + 0x34):04X} "
            f"PEND=${bus.read_long(ddt_addr + 0x84) & 0xFFFFFFFF:08X}"
        )

    def _trace_native_zsydsk_block() -> None:
        if not native_dispatch_trace:
            return
        zsydsk = bus.read_long(0x040C) & 0xFFFFFF
        if not (0x1000 <= zsydsk < 0x100000):
            return
        words = tuple(bus.read_word(zsydsk + off * 2) for off in range(16))
        prev = _native_zsydsk_snapshot[0]
        if prev is None:
            _native_zsydsk_snapshot[0] = (zsydsk, words)
            return
        if prev != (zsydsk, words):
            _native_zsydsk_snapshot[0] = (zsydsk, words)
            _emit_native_trace(
                "ZSYDSK-BLOCK "
                f"@${zsydsk:06X} "
                + " ".join(f"{word:04X}" for word in words)
            )

    def _trace_native_dispatch(cpu_ref, opword: int, pc: int) -> None:
        if not native_dispatch_trace:
            return
        if pc in _native_dispatch_labels:
            label = _native_dispatch_labels[pc]
            a0 = cpu_ref.a[0] & 0xFFFFFF
            a1 = cpu_ref.a[1] & 0xFFFFFF
            a6 = cpu_ref.a[6] & 0xFFFFFF
            zsydsk = bus.read_long(0x040C) & 0xFFFFFF
            ddbchn = bus.read_long(0x0408) & 0xFFFFFF
            jobcur = bus.read_long(0x041C) & 0xFFFFFF
            _emit_native_trace(
                f"DISPATCH {label} "
                f"D0=${cpu_ref.d[0] & 0xFFFFFFFF:08X} "
                f"D6=${cpu_ref.d[6] & 0xFFFFFFFF:08X} "
                f"A0=${a0:06X} A1=${a1:06X} A6=${a6:06X} "
                f"ZSYDSK=${zsydsk:06X} DDBCHN=${ddbchn:06X} JOBCUR=${jobcur:06X}",
                arm=True,
            )
            if 0x1000 <= a0 < 0x100000:
                _dump_native_ddt(a0, f"{label}-A0")
            if pc == 0x001B00 and 0x1000 <= a0 - 0x0C < 0x100000:
                _dump_native_words(a0 - 0x0C, "ZSTATE", words=8)
            if pc == 0x0062E4:
                _dump_native_words(a6, "JSR-A6-TGT", words=8)
            if pc in {0x00236A, 0x005FBC, 0x005FBE, 0x005FC2, 0x005FC4, 0x005FD0}:
                a3 = cpu_ref.a[3] & 0xFFFFFF
                _dump_native_raw_words(a1, f"{label}-A1", words=8)
                if 0x1000 <= a3 < 0x100000:
                    _dump_native_words(a3, f"{label}-A3", words=8)
                if 0x1000 <= a6 < 0x100000:
                    _dump_native_words(a6, f"{label}-A6", words=8)
            if bus.read_word(0xA870) or bus.read_long(0xA872):
                _dump_native_ddt(0xA86E, "LIVE-DDT")
            if pc not in _native_dispatch_hits:
                _native_dispatch_hits.add(pc)
                if zsydsk:
                    _dump_native_words(zsydsk, "ZSYDSK-WORDS", words=8)
                if pc in {0x003EBE, 0x003F7A}:
                    _emit_native_trace(
                        f"GATE-STATE SYSTEM=${bus.read_long(0x0400) & 0xFFFFFFFF:08X} "
                        f"W0564=${bus.read_long(0x0564) & 0xFFFFFFFF:08X}"
                    )
                    _dump_native_raw_words(0x0400, "SYSTEM-RAW", words=4)
                    _dump_native_raw_words(0x0560, "GATE-RAW", words=4)
                    _dump_native_recent_watch(0x0400)
                    _dump_native_recent_watch(0x0564)

        if 0x00A104 <= pc <= 0x00A900 and _native_exec_hits["scz"] < 24:
            _emit_native_trace(
                f"SCZ-EXEC PC=${pc:06X} D6=${cpu_ref.d[6] & 0xFFFFFFFF:08X} "
                f"A0=${cpu_ref.a[0] & 0xFFFFFF:06X}"
            )
            _native_exec_hits["scz"] += 1
        elif 0x00B440 <= pc <= 0x00B500 and _native_exec_hits["b440"] < 12:
            _emit_native_trace(
                f"B440-EXEC PC=${pc:06X} D6=${cpu_ref.d[6] & 0xFFFFFFFF:08X} "
                f"A0=${cpu_ref.a[0] & 0xFFFFFF:06X}"
            )
            _native_exec_hits["b440"] += 1

    if scsi_bus is not None:
        scsi_bus.trace_callback = _emit_native_trace

    def _trace_linea_context(cpu_ref, opword: int, pc: int) -> None:
        if opword == 0xA06C:
            a4 = cpu_ref.a[4] & 0xFFFFFF
            a6 = cpu_ref.a[6] & 0xFFFFFF
            jcb = bus.read_long(0x041C) & 0xFFFFFF
            ddbchn = bus.read_long(0x0408) & 0xFFFFFF
            zsydsk = bus.read_long(0x040C) & 0xFFFFFF
            spec = []
            if 0x1000 <= a4 < 0x100000:
                spec = [
                    bus.read_word(a4 + 0x06),
                    bus.read_word(a4 + 0x08),
                    bus.read_word(a4 + 0x0A),
                    bus.read_word(a4 + 0x0C),
                ]
            d6_val = cpu_ref.d[6] & 0xFFFFFFFF
            # Deferred JCB+$0C setup: MONGEN places the default
            # devspec below ZSYDSK so FIND's CMPA at $00258E takes
            # the disk-search path.  Must be set AFTER CHAIN uses
            # JCB+$0C for its own init, but BEFORE FIND reads it
            # via $A052 (GETDEF).  Trigger on first FIND after CHAIN.
            if (not compat_mode and _chain_seen[0]
                    and _ddb_populated[0]
                    and not _jcb_devspec_set[0]
                    and d6_val == 1):
                _jcb_devspec_set[0] = True
                # Create a devspec entry that makes FIND's chain
                # walk match the file spec.  The walk at $002578:
                #   A5=A2(devspec), TST.L(A5), ADD.L(A5)+→A2,
                #   ADDQ#2→A5, CMP A5 vs A1(filespec+6)
                # For match on first iteration:
                #   devspec + 6 = filespec + 6
                #   → devspec = filespec = A4
                # But the devspec must have link=0 at +$00 so
                # the chain terminates.  Can't use the file spec
                # because its +$00 has flags (non-zero).
                #
                # Instead: place devspec BELOW ZSYDSK with link=0.
                # The chain walk will find link=0 → BEQ $00258E.
                # Then CMPA checks devspec addr < ZSYDSK → passes.
                # FIND then goes to disk search at $0025A8.
                # JCB+$0C below ZSYDSK so CMPA at $00258E passes.
                # Pre-set file spec DDB pointer so SRCH shortcut
                # at $005AB6 fires.  FIND at $0025EC only clears
                # spec+$28 when A4==A6; with A6=devspec≠A4=filespec,
                # the DDB pointer survives.  SRCH sees non-zero
                # at spec+$28 → shortcut → FIND success.
                # This is MONGEN-equivalent: configured device specs
                # in the JCB workspace have DDB pointers pre-set.
                _zsydsk = bus.read_long(0x040C) & 0xFFFFFF
                if 0x1000 <= jcb < 0x100000 and _zsydsk:
                    ds = _zsydsk - 0x10
                    bus.write_long(ds, 0)  # link=0
                    bus.write_long(jcb + 0x0C, ds)
                    # FIND at $002604 writes LNK ($4D3B) as the
                    # default device.  LNK is the AMOS "link device"
                    # indirection layer — but this .MON has no LNK
                    # configuration (DEVTBL was empty).  Change the
                    # default device constant to DSK ($1C03) so FIND
                    # assigns files directly to the physical disk.
                    # SRCH then matches DSK in DEVTBL and the DDB
                    # walk at $005C72 (CMP #$1C03) triggers naturally.
                    bus.write_word(0x2606, 0x1C03)
                    sys.stderr.write(
                        f"[DSK] JCB+$0C → ${ds:06X}"
                        f" spec+$2E=${_zsydsk:06X}\n")
            elif (not compat_mode and _chain_seen[0]
                    and d6_val == 1
                    and not _jcb_devspec_set[0]):
                sys.stderr.write(
                    f"[DSK-SKIP] chain={_chain_seen[0]}"
                    f" ddb={_ddb_populated[0]}\n")
            # Dump full spec and JCB devspec at FIND entry
            if d6_val == 1 and 0x1000 <= a4 < 0x100000:
                jcb_0c = bus.read_long(jcb + 0x0C) & 0xFFFFFF
                sys.stderr.write(
                    f"[ENTRY-SPEC] @${a4:06X} pc=${pc:06X}"
                    f" JCB+0C=${jcb_0c:06X}\n")
                if 0x1000 <= jcb_0c < 0x100000:
                    sys.stderr.write(
                        f"[JCB-DEVSPEC] @${jcb_0c:06X}:"
                        + "".join(
                            f" {off:02X}:{bus.read_word(jcb_0c+off):04X}"
                            for off in range(0, 0x30, 2)
                        ) + "\n"
                    )
            _emit_native_trace(
                "LINEA A06C "
                f"D6=${d6_val:08X} "
                f"A4=${a4:06X} A6=${a6:06X} "
                f"JCB=${jcb:06X} DDBCHN=${ddbchn:06X} ZSYDSK=${zsydsk:06X} "
                f"spec={'.'.join(f'{word:04X}' for word in spec) if spec else 'n/a'}"
                f" D7=${cpu_ref.d[7] & 0xFFFFFFFF:08X}"
                f" SR=${cpu_ref.sr:04X}",
                arm=True,
            )
            # Dump A6 frame for FETCH calls (D6=0 or D6=2)
            if d6_val in (0, 2) and 0x1000 <= a6 < 0x100000:
                _emit_native_trace(
                    f"  A6-frame @${a6:06X}:"
                    + "".join(
                        f" {off:02X}:{bus.read_long(a6+off):08X}"
                        for off in range(0, 0x20, 4)
                    )
                )
            return

        if not _native_trace_armed[0]:
            return

        if opword == 0xA03C:
            a0 = cpu_ref.a[0] & 0xFFFFFF
            ddt = buf = blk = 0
            if 0x1000 <= a0 < 0x100000:
                ddt = bus.read_long(a0 + 0x08) & 0xFFFFFF
                buf = bus.read_long(a0 + 0x0C) & 0xFFFFFF
                blk = bus.read_long(a0 + 0x10)
            _emit_native_trace(
                "LINEA A03C "
                f"D6=${cpu_ref.d[6] & 0xFFFFFFFF:08X} "
                f"A0=${a0:06X} DDT=${ddt:06X} BUF=${buf:06X} BLK=${blk:08X}"
            )
        elif opword == 0xA080:
            a4 = cpu_ref.a[4] & 0xFFFFFF
            dev = drv = ddb = 0
            if 0x1000 <= a4 < 0x100000:
                dev = bus.read_word(a4 + 0x02)
            # Debug: read $A8AC directly
            if _jcb_devspec_set[0]:
                sys.stderr.write(
                    f"[SRCH-DBG] A4=${a4:06X}"
                    f" A4+2=${bus.read_word(a4+2) if 0x1000<=a4<0x100000 else 0:04X}"
                    f" $A8AC=${bus.read_word(0xA8AC):04X}"
                    f" $A8B2=${bus.read_word(0xA8B2):04X}\n")
                drv = bus.read_long(a4 + 0x24) & 0xFFFFFF
                ddb = bus.read_long(a4 + 0x28) & 0xFFFFFF
            _emit_native_trace(
                "LINEA A080 "
                f"A4=${a4:06X} DEV=${dev:04X} DRV=${drv:06X} DDB=${ddb:06X}"
            )
            if ddb:
                _dump_native_ddb(ddb, "SRCH-DDB")

    def _accel_with_driver_hook(cpu_ref):
        pc = cpu_ref.pc & 0xFFFFFF
        _trace_native_watchpoints()
        _trace_native_zsydsk_block()

        if not _driver_installed[0]:
            # Run the early boot bootstrap at the first GETMEM (LINE-A $A03C).
            # AMOSL.MON's init calls GETMEM 14 times at $1B06 to
            # allocate DDTs, DDBs, and file channels.  These all fail
            # when MEMBAS=0 (ROM zeroes sysvars and the disk image's
            # AMOSL.MON doesn't recalculate it).  On real hardware,
            # MONGEN embeds MEMBAS/MEMSIZ at build time.  This boot
            # ROM extension provides the equivalent initialization.
            #
            # We trigger on the first GETMEM opcode rather than a
            # fixed PC because the OS code flow may vary.  By this
            # point the disk load is complete ($B800 area is safe).
            opword = bus.read_word(pc)
            if opword == 0xA03C:
                if compat_mode:
                    # Legacy compatibility path: inject a minimal serial
                    # driver and patch the shared dispatch vector. Native
                    # mode must not touch $0462; the disk mount path uses it.
                    for i in range(0, len(_drv_code), 2):
                        word = (_drv_code[i] << 8) | _drv_code[i + 1]
                        bus.write_word(DRIVER_BASE + i, word)
                    bus.write_long(0x0462, DRIVER_BASE)
                    sys.stderr.write(
                        f"[DRV] Serial driver at ${DRIVER_BASE:06X} "
                        f"(compat bootstrap before GETMEM at ${pc:06X})\n"
                    )
                else:
                    sys.stderr.write(
                        "[DRV] Native bootstrap leaves $0462 unchanged\n"
                    )
                _driver_installed[0] = True

                # Initialize memory pool — equivalent to the driver's
                # init handler at +$18 executing its MEMBAS setup.
                # This is hardware configuration (RAM layout), not an
                # OS data structure.  Values match what the driver's
                # 68000 code at +$1E through +$3D would write.
                if bus.read_long(0x0430) == 0:  # MEMBAS not yet set
                    bus.write_long(0x0430, 0x0000C000)  # MEMBAS
                    bus.write_long(0x0438, 0x000E0000)  # MEMSIZ
                    # Free memory block header at MEMBAS
                    bus.write_long(0xC000, 0x00000000)  # next = 0
                    bus.write_long(0xC004, 0x000D4000)  # size
                    sys.stderr.write(
                        "[DRV] Memory pool: MEMBAS=$C000 "
                        "MEMSIZ=$0E0000\n"
                    )

                # DDT/DDB: let the OS create its own via GETMEM.
                # The .MON init sets ZSYDSK, DDBCHN, and geometry
                # from MONGEN config embedded in the file.
                sys.stderr.write(
                    "[DSK] OS will create DDT/DDB via GETMEM\n"
                )

                # File channel/TCB/devspec fixups are compatibility
                # scaffolding, not hardware behavior.
                if compat_mode:
                    # File channel magic init is deferred — JCB ($041C)
                    # is temporarily 0 during scheduler context switch
                    # when the first GETMEM fires.  Set flag so the
                    # magic gets written on the next instruction where
                    # JCB is valid.
                    _need_magic_init[0] = True

        elif _driver_installed[0]:
            # Deferred file channel magic init — write $5A5A to
            # JCB+$D0+$2CA once JCB is available (was 0 at install time
            # due to scheduler context switch clearing $041C).
            if compat_mode and _need_magic_init[0]:
                jcb = bus.read_long(0x041C) & 0xFFFFFF
                if 0x1000 <= jcb < 0x100000:
                    ws = bus.read_long(jcb + 0xD0) & 0xFFFFFF
                    if 0x1000 <= ws < 0x100000:
                        bus.write_word(ws + 0x2CA, 0x5A5A)
                        sys.stderr.write(
                            f"[DRV] File channel magic $5A5A at "
                            f"${ws + 0x2CA:06X}\n"
                        )

                    # Fix FIND's device spec list: JCB+$0C must
                    # point BELOW ZSYDSK ($B440) so FIND's code
                    # at $258E (CMPA.L ZSYDSK, A2 / BCC) takes
                    # the disk-search path.  Place a zeroed spec
                    # just below the DDB — IOINI's SRCH needs
                    # spec+$28=0 so it does a DEVTBL lookup and
                    # triggers the mount code with SCSI reads.
                    DEVSPEC_ADDR = 0xB430
                    bus.write_long(DEVSPEC_ADDR, 0x00000000)
                    bus.write_long(jcb + 0x0C, DEVSPEC_ADDR)
                    sys.stderr.write(
                        f"[DRV] JCB+$0C → ${DEVSPEC_ADDR:06X} "
                        f"(DSK0 dev spec at ${DEVSPEC_ADDR:06X})\n"
                    )

                    _need_magic_init[0] = False

            # --- Driver entry tracing ---
            if not native_find_trace and 0xB440 <= pc <= 0xB500:
                if _ioini_trace_count[0] < 200:
                    sys.stderr.write(
                        f"[DRV-EXEC] PC=${pc:06X} "
                        f"D6=${cpu_ref.d[6]:08X} "
                        f"A0=${cpu_ref.a[0]&0xFFFFFF:06X}\n"
                    )
                    _ioini_trace_count[0] += 1

            # --- Output bridging ---
            # Intercept $A0CA (TTYOUT): grab the character from D1.B
            # (AMOS convention: D1.B = char, D6 = flags) and send it
            # directly to stdout via tx_callback.  We bypass
            # acia.write() to avoid generating spurious echoes that
            # would clobber the ACIA RX register.
            opword = bus.read_word(pc)
            _trace_linea_context(cpu_ref, opword, pc)
            _trace_native_dispatch(cpu_ref, opword, pc)

            # --- SRCH per-instruction trace ---
            if _srch_trace[0] and (opword & 0xF000) != 0xA000:
                _srch_trace_lines[0] += 1
                if _srch_trace_lines[0] <= 300:
                    sys.stderr.write(
                        f"  ${pc:06X}: ${opword:04X}")
                    # Show key register reads
                    if _srch_trace_lines[0] <= 50:
                        sys.stderr.write(
                            f"  D0={cpu_ref.d[0]:08X}"
                            f" D7={cpu_ref.d[7]:08X}"
                            f" A2={cpu_ref.a[2]&0xFFFFFF:06X}"
                            f" A4={cpu_ref.a[4]&0xFFFFFF:06X}"
                            f" A6={cpu_ref.a[6]&0xFFFFFF:06X}")
                    sys.stderr.write("\n")

            # --- LINE-A tracing ---
            if (not native_find_trace
                    and opword & 0xF000 == 0xA000
                    and _ioini_trace_count[0] < 200):
                # Skip high-volume init and scheduler calls
                _skip = {0xA03E, 0xA0CA, 0xA036, 0xA04C,
                         0xA04E, 0xA046, 0xA034, 0xA044}
                if opword not in _skip:
                    if opword == 0xA03C:
                        d6 = cpu_ref.d[6]
                        a0 = cpu_ref.a[0] & 0xFFFFFF
                        if d6 & 0x80000000:
                            ddt_back = (bus.read_long(a0 + 0x08)
                                        & 0xFFFFFF)
                            buf = (bus.read_long(a0 + 0x0C)
                                   & 0xFFFFFF)
                            blk = bus.read_long(a0 + 0x10)
                            sys.stderr.write(
                                f"[IOINI] I/O: A0=${a0:06X} "
                                f"DDT=${ddt_back:06X} "
                                f"buf=${buf:06X} blk={blk}\n"
                            )
                        else:
                            sys.stderr.write(
                                f"[IOINI] MNT: D6={d6:X}\n"
                            )
                    elif opword == 0xA06C:
                        v0414 = (bus.read_long(0x0414)
                                 & 0xFFFFFF)
                        jcb = (bus.read_long(0x041C)
                               & 0xFFFFFF)
                        jcb_0c = 0
                        if 0x1000 <= jcb < 0x100000:
                            jcb_0c = (bus.read_long(jcb + 0x0C)
                                      & 0xFFFFFF)
                        zsydsk = (bus.read_long(0x040C)
                                  & 0xFFFFFF)
                        ddbchn = (bus.read_long(0x0408)
                                  & 0xFFFFFF)
                        sys.stderr.write(
                            f"[FIND] PC=${pc:06X} "
                            f"D6={cpu_ref.d[6]:X} "
                            f"DDBCHN=${ddbchn:06X} "
                            f"JCB+0C=${jcb_0c:06X} "
                            f"ZSYDSK=${zsydsk:06X}\n"
                        )
                        # Dump DDB chain state
                        if 0x1000 <= ddbchn < 0x100000:
                            ddb_link = bus.read_long(ddbchn)
                            ddb_ddt = (bus.read_long(
                                ddbchn + 0x08) & 0xFFFFFF)
                            sys.stderr.write(
                                f"  DDB+00=${ddb_link:08X} "
                                f"DDB+08(DDT)=${ddb_ddt:06X}\n"
                            )
                            if 0x1000 <= ddb_ddt < 0x100000:
                                dd_sts = bus.read_word(ddb_ddt)
                                dd_flg = bus.read_word(
                                    ddb_ddt + 0x02)
                                dd_drv = (bus.read_long(
                                    ddb_ddt + 0x06) & 0xFFFFFF)
                                dd_nam = bus.read_word(
                                    ddb_ddt + 0x34)
                                dd_int = bus.read_long(
                                    ddb_ddt + 0x14)
                                sys.stderr.write(
                                    f"  DDT: STS=${dd_sts:04X} "
                                    f"FLG=${dd_flg:04X} "
                                    f"DRV=${dd_drv:06X} "
                                    f"NAM=${dd_nam:04X} "
                                    f"INT=${dd_int:08X}\n"
                                )
                        # Dump search spec (A6 in FIND = A4)
                        a6 = cpu_ref.a[6] & 0xFFFFFF
                        if 0x1000 <= a6 < 0x100000:
                            w0 = bus.read_word(a6)
                            w1 = bus.read_word(a6 + 2)
                            w2 = bus.read_word(a6 + 4)
                            sys.stderr.write(
                                f"  spec=${w0:04X}.${w1:04X}"
                                f".${w2:04X} @${a6:06X}\n"
                            )
                            # SRCH tests A4+d16 where d16
                            # is the word at $5AB8
                            _sd = bus.read_word(0x5AB8)
                            _sds = (_sd - 0x10000
                                    if _sd & 0x8000 else _sd)
                            _sv = bus.read_long(a6 + _sds)
                            sys.stderr.write(
                                f"  SRCH field: "
                                f"A4+${_sd:04X}="
                                f"${a6+_sds:06X} "
                                f"val=${_sv:08X}\n")
                            # Full spec dump (first 48 bytes)
                            sys.stderr.write(
                                f"  spec dump:")
                            for _so in range(0, 48, 4):
                                _vv = bus.read_long(a6 + _so)
                                sys.stderr.write(
                                    f" {_so:02X}:{_vv:08X}")
                            sys.stderr.write("\n")
                            # DEVTBL dump
                            _dt = (bus.read_long(0x0404)
                                   & 0xFFFFFF)
                            sys.stderr.write(
                                f"  DEVTBL=${_dt:06X}:")
                            if 0x1000 <= _dt < 0x100000:
                                for _di in range(10):
                                    _dw = bus.read_word(
                                        _dt + _di * 6)
                                    _dw2 = bus.read_word(
                                        _dt + _di * 6 + 2)
                                    _dp = bus.read_word(
                                        _dt + _di * 6 + 4)
                                    if _dw == 0 and _dw2 == 0:
                                        sys.stderr.write(
                                            " (end)")
                                        break
                                    sys.stderr.write(
                                        f" {_dw:04X}.{_dw2:04X}"
                                        f"→{_dp:04X}")
                            sys.stderr.write("\n")
                    else:
                        sys.stderr.write(
                            f"[LA] ${opword:04X} @${pc:06X}\n"
                        )
                        # SRCH DDB fixup — FIND clears spec+$28
                        # (DDB pointer) at $25EC and SRCH's DEVTBL
                        # lookup only sets spec+$24 (driver), not
                        # spec+$28.  When spec+$02 is LNK ($4D3B,
                        # set by FIND at $2604) and spec+$28 is 0,
                        # inject the DDB pointer from DDBCHN so
                        # SRCH's TST.L at $5AB6 succeeds.  This is
                        # MONGEN-equivalent: on a real system the
                        # link device setup would populate this.
                        if (opword == 0xA080
                                and _chain_seen[0]
                                and _ddb_populated[0]):
                            a4 = cpu_ref.a[4] & 0xFFFFFF
                            if 0x1000 <= a4 < 0x100000:
                                dev = bus.read_word(a4 + 2)
                                ddb_ptr = bus.read_long(
                                    a4 + 0x28) & 0xFFFFFF
                                if dev == 0x4D3B and ddb_ptr == 0:
                                    ddbchn = (bus.read_long(0x0408)
                                              & 0xFFFFFF)
                                    if ddbchn and ddbchn < 0x100000:
                                        bus.write_long(
                                            a4 + 0x28, ddbchn)

                        # Activate SRCH trace on first $A080
                        # after CHAIN
                        if (opword == 0xA080
                                and _chain_seen[0]
                                and not _srch_trace[0]):
                            _srch_trace[0] = True
                            _srch_trace_lines[0] = 0
                            sys.stderr.write(
                                "[SRCH-TRACE] Activated\n")
                            sys.stderr.write(
                                f"  D: "
                                f"{cpu_ref.d[0]:08X} "
                                f"{cpu_ref.d[1]:08X} "
                                f"{cpu_ref.d[2]:08X} "
                                f"{cpu_ref.d[3]:08X} "
                                f"{cpu_ref.d[4]:08X} "
                                f"{cpu_ref.d[5]:08X} "
                                f"{cpu_ref.d[6]:08X} "
                                f"{cpu_ref.d[7]:08X}\n")
                            sys.stderr.write(
                                f"  A: "
                                f"{cpu_ref.a[0]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[1]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[2]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[3]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[4]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[5]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[6]&0xFFFFFF:06X} "
                                f"{cpu_ref.a[7]&0xFFFFFF:06X}\n")
                        # Stop trace on any LINE-A after SRCH
                        if _srch_trace[0] and opword != 0xA080:
                            _srch_trace[0] = False
                            sys.stderr.write(
                                f"[SRCH-TRACE] Ended at "
                                f"${opword:04X} after "
                                f"{_srch_trace_lines[0]} insns\n")
                    _ioini_trace_count[0] += 1
            # --- Driver execution trace ---
            if (_ddb_populated[0]
                    and (DISK_DRV_BASE <= pc <= DISK_DRV_BASE + 0x10
                         or 0xA100 <= pc <= 0xA200)):
                sys.stderr.write(
                    f"[DRV] PC=${pc:06X} op=${opword:04X}"
                    f" D0=${cpu_ref.d[0]:08X}"
                    f" A0=${cpu_ref.a[0]&0xFFFFFF:06X}\n")

            # --- FIND D6=2/0 FETCH D7 fixup ---
            # FIND D6=2 (FETCH) returns D7=0 because the 68040
            # cache-flush path at $001452 returns early without
            # setting D7 to a success value.  At $0025D6 FIND
            # writes D7 to stacked SR; D7=0 means Z=0 (failure).
            # For FETCH to succeed, D7 must be 4 (Z bit = $04).
            # On real hardware, the MONGEN-configured readiness
            # check would set D7 via a different code path.
            # --- SCZ.DVR execution trace ---
            if 0xA100 <= pc <= 0xA200 and _ddb_populated[0]:
                sys.stderr.write(
                    f"[SCZ] PC=${pc:06X} ${opword:04X}"
                    f" D0={cpu_ref.d[0]:08X}"
                    f" D6={cpu_ref.d[6]:08X}"
                    f" A0={cpu_ref.a[0]&0xFFFFFF:06X}\n")

            # --- FIND path trace ($0025DA-$002616) ---
            if (_jcb_devspec_set[0] and _find_path_count[0] < 300
                    and (0x2540 <= pc <= 0x26A0
                         or 0x25B0 <= pc <= 0x25E0
                         or 0x4A50 <= pc <= 0x4C00
                         or 0x6680 <= pc <= 0x6720
                         or 0x7880 <= pc <= 0x7920
                         or 0x5C50 <= pc <= 0x5CC0
                         or 0x0F90 <= pc <= 0x0FC0)):
                _find_path_count[0] += 1
                extra = ""
                if pc == 0x5C5E:
                    d16x = bus.read_word(0x5C60)
                    if d16x & 0x8000: d16x -= 0x10000
                    a6v = cpu_ref.a[6] & 0xFFFFFF
                    d6v = cpu_ref.d[6] & 0xFFFF
                    val = bus.read_word(a6v+d16x) if 0x1000<=a6v+d16x<0x100000 else 0
                    extra = f" CMP.W {d16x}(A6=${a6v:06X})=${val:04X} vs D6=${d6v:04X}"
                elif pc == 0x4A54:
                    disp = bus.read_word(pc + 2)
                    if disp & 0x8000:
                        disp -= 0x10000
                    target = pc + 2 + disp
                    extra = f" BEQ ${target:06X}"
                elif pc == 0x25F6:
                    imm = bus.read_word(pc + 2)
                    disp = bus.read_word(pc + 4)
                    if disp & 0x8000:
                        disp -= 0x10000
                    a4v = cpu_ref.a[4] & 0xFFFFFF
                    val = bus.read_word(a4v + disp) if 0x1000 <= a4v + disp < 0x100000 else 0
                    extra = (f" CMPI.W #${imm:04X},{disp}(A4=${a4v:06X})"
                             f" val=${val:04X}")
                elif pc == 0x25FE:
                    a4v = cpu_ref.a[4] & 0xFFFFFF
                    val = bus.read_word(a4v + 2) if 0x1000 <= a4v < 0x100000 else 0
                    extra = f" TST.W 2(A4=${a4v:06X})=${val:04X}"
                elif pc == 0x2604:
                    a4v = cpu_ref.a[4] & 0xFFFFFF
                    extra = f" MOVE.W #$4D3B→${a4v+2:06X}"
                sys.stderr.write(
                    f"  FPATH ${pc:06X}: ${opword:04X}"
                    f" A4=${cpu_ref.a[4]&0xFFFFFF:06X}"
                    f" A6=${cpu_ref.a[6]&0xFFFFFF:06X}"
                    f" SR=${cpu_ref.sr:04X}{extra}\n")

            # --- $A054/$A056 handler trace ---
            # Trace $A054 handler instructions
            if _a054_trace[0]:
                _a054_count[0] += 1
                if (opword & 0xF000) == 0xA000 and opword not in (0xA054, 0xA056):
                    sys.stderr.write(
                        f"  A054h ${pc:06X}: ${opword:04X} (LINE-A)"
                        f" A6=${cpu_ref.a[6]&0xFFFFFF:06X}\n")
                    _a054_trace[0] = False
                elif _a054_count[0] <= 30:
                    sys.stderr.write(
                        f"  A054h ${pc:06X}: ${opword:04X}"
                        f" D0=${cpu_ref.d[0]:08X}"
                        f" A0=${cpu_ref.a[0]&0xFFFFFF:06X}"
                        f" A6=${cpu_ref.a[6]&0xFFFFFF:06X}\n")
            if opword == 0xA054 and _ddb_populated[0] and not _a054_trace[0]:
                _a054_trace[0] = True
                _a054_count[0] = 0
                _a052_count[0] += 1
                sys.stderr.write(
                    f"[LA-{opword:04X}] PC=${pc:06X}"
                    f" A0=${cpu_ref.a[0]&0xFFFFFF:06X}"
                    f" A4=${cpu_ref.a[4]&0xFFFFFF:06X}"
                    f" A6=${cpu_ref.a[6]&0xFFFFFF:06X}"
                    f" D0=${cpu_ref.d[0]:08X}"
                    f" D6=${cpu_ref.d[6]:08X}\n")

            # --- $A052 handler trace (first call only) ---
            if _a052_trace[0]:
                _a052_count[0] += 1
                if (opword & 0xF000) == 0xA000:
                    sys.stderr.write(
                        f"  A052 ${pc:06X}: ${opword:04X} (LINE-A)"
                        f" A4=${cpu_ref.a[4]&0xFFFFFF:06X}\n")
                    _a052_trace[0] = False
                elif _a052_count[0] <= 200:
                    sys.stderr.write(
                        f"  A052 ${pc:06X}: ${opword:04X}"
                        f" D0={cpu_ref.d[0]:08X}"
                        f" A2={cpu_ref.a[2]&0xFFFFFF:06X}"
                        f" A4={cpu_ref.a[4]&0xFFFFFF:06X}"
                        f" A6={cpu_ref.a[6]&0xFFFFFF:06X}"
                        f" SR={cpu_ref.sr:04X}\n")
            if (opword == 0xA052 and _chain_seen[0]
                    and not _a052_trace[0] and _a052_count[0] == 0):
                _a052_trace[0] = True
                sys.stderr.write(
                    f"[A052] ENTRY A4=${cpu_ref.a[4]&0xFFFFFF:06X}"
                    f" A6=${cpu_ref.a[6]&0xFFFFFF:06X}"
                    f" D6=${cpu_ref.d[6]:08X}\n")

            # --- SRCH execution trace (first call only) ---
            if _srch_trace[0] and (opword & 0xF000) != 0xA000:
                _srch_trace_lines[0] += 1
                if _srch_trace_lines[0] <= 300:
                    extra = ""
                    if pc == 0x5C58:
                        d16 = bus.read_word(0x5C5A)
                        if d16 & 0x8000:
                            d16 -= 0x10000
                        a6 = cpu_ref.a[6] & 0xFFFFFF
                        d1 = cpu_ref.d[1] & 0xFFFF
                        val = bus.read_word(a6 + d16) if 0x1000 <= a6 + d16 < 0x100000 else 0
                        extra = (
                            f" CMP.W {d16}(A6=${a6:06X}),"
                            f"D1=${d1:04X}"
                            f" [{a6+d16:06X}]=${val:04X}")
                    elif pc == 0x5C70:
                        a2v = cpu_ref.a[2] & 0xFFFFFF
                        val = bus.read_long(a2v)
                        extra = f" A1←(${a2v:06X})=${val:08X}"
                    elif pc == 0x5CA8:
                        # After spec built — dump it with SP
                        a6v = cpu_ref.a[6] & 0xFFFFFF
                        a7v = cpu_ref.a[7] & 0xFFFFFF
                        if 0x1000 <= a6v < 0x100000:
                            sys.stderr.write(
                                f"  NEW-SPEC @${a6v:06X}"
                                f" SP=${a7v:06X}:")
                            for off in range(0, 0x30, 2):
                                sys.stderr.write(
                                    f" +{off:02X}:{bus.read_word(a6v+off):04X}")
                            sys.stderr.write("\n")
                    elif pc in (0x6200, 0x6206, 0x2542):
                        a7v = cpu_ref.a[7] & 0xFFFFFF
                        extra = f" SP=${a7v:06X}"
                    elif pc == 0x5C3C:
                        d16 = bus.read_word(0x5C3E)
                        if d16 & 0x8000:
                            d16 -= 0x10000
                        a4v = cpu_ref.a[4] & 0xFFFFFF
                        val = bus.read_word(a4v + d16) if 0x1000 <= a4v + d16 < 0x100000 else 0
                        extra = f" D1←{d16}(A4=${a4v:06X})=${val:04X} SP=${cpu_ref.a[7]&0xFFFFFF:06X}"
                    elif pc in (0x5C8C, 0x5C9A, 0x5CA0):
                        imm = bus.read_word(pc + 2)
                        extra = f" PUSH #${imm:04X}"
                    elif pc == 0x5C88:
                        d16 = bus.read_word(pc + 2)
                        if d16 & 0x8000:
                            d16 -= 0x10000
                        a7 = cpu_ref.a[7] & 0xFFFFFF
                        extra = f" ADDQ.B #1,{d16}(A7=${a7:06X})"
                    elif pc == 0x5C72:
                        # CMP.W #imm,D1 — imm at $5C74
                        imm = bus.read_word(0x5C74)
                        d1 = cpu_ref.d[1] & 0xFFFF
                        extra = f" CMP.W #${imm:04X},D1=${d1:04X}"
                    sys.stderr.write(
                        f"  SRCH ${pc:06X}: ${opword:04X}"
                        f" D0={cpu_ref.d[0]:08X}"
                        f" D1={cpu_ref.d[1]:08X}"
                        f" D7={cpu_ref.d[7]:08X}"
                        f" A0={cpu_ref.a[0]&0xFFFFFF:06X}"
                        f" A1={cpu_ref.a[1]&0xFFFFFF:06X}"
                        f" A2={cpu_ref.a[2]&0xFFFFFF:06X}"
                        f" A4={cpu_ref.a[4]&0xFFFFFF:06X}"
                        f" A6={cpu_ref.a[6]&0xFFFFFF:06X}"
                        f" SR={cpu_ref.sr:04X}"
                        f"{extra}\n")
            if (opword == 0xA080
                    and _chain_seen[0]
                    and _ddb_populated[0]):
                # Dump spec at SRCH entry
                a4s = cpu_ref.a[4] & 0xFFFFFF
                if 0x1000 <= a4s < 0x100000 and _srch_trace_lines[0] == 0:
                    sys.stderr.write(
                        f"[SRCH-ENTRY] A4=${a4s:06X}"
                        f" spec: +00:{bus.read_word(a4s):04X}"
                        f" +02:{bus.read_word(a4s+2):04X}"
                        f" +04:{bus.read_word(a4s+4):04X}"
                        f" +06:{bus.read_word(a4s+6):04X}\n")
                    # Also dump original spec at $A8AA
                    sys.stderr.write(
                        f"[SRCH-ENTRY] orig@$A8AA:"
                        f" +00:{bus.read_word(0xA8AA):04X}"
                        f" +02:{bus.read_word(0xA8AC):04X}"
                        f" +04:{bus.read_word(0xA8AE):04X}"
                        f" +06:{bus.read_word(0xA8B0):04X}"
                        f" +08:{bus.read_word(0xA8B2):04X}\n")
                _srch_trace[0] = True
                _srch_trace_lines[0] = 0

            # --- FIND ($A06C) bypass ---
            # Intercept FIND system call to load files from disk
            # directly via Python, bypassing the native FIND path
            # which needs complete disk/device infrastructure.
            if (compat_mode and opword == 0xA06C
                    and _chain_seen[0] and _raw_disk):
                _find_count[0] += 1
                d6 = cpu_ref.d[6] & 0xFFFF
                a4 = cpu_ref.a[4] & 0xFFFFFF
                a6 = cpu_ref.a[6] & 0xFFFFFF

                if d6 == 1 and 0x1000 <= a4 < 0x100000:
                    # Standard FIND by name — read spec from A4
                    name1 = bus.read_word(a4 + 0x06)
                    name2 = bus.read_word(a4 + 0x08)
                    ext_spec = bus.read_word(a4 + 0x0A)
                    ppn_spec = bus.read_word(a4 + 0x0C)

                    result = None
                    ext_used = ext_spec
                    if ext_spec:
                        ppns = ([ppn_spec] if ppn_spec
                                else [0x0104, 0x0B0B, 0x0202,
                                      0x010B, 0x0106])
                        for ppn_w in ppns:
                            result = _find_file_on_disk(
                                name1, name2, ext_spec, ppn_w)
                            if result:
                                break
                    if not result:
                        result, ext_used, _ = _try_find_file(
                            name1, name2)

                    if result:
                        start_block, size_blocks, attr = result
                        load_addr = CMD_LOAD_BASE
                        bytes_loaded = _load_file_data(
                            start_block, size_blocks, load_addr)
                        name_str = (_rad50_decode(name1)
                                    + _rad50_decode(name2))
                        ext_str = _rad50_decode(ext_used)
                        if _find_count[0] <= 20:
                            sys.stderr.write(
                                f"[FIND] '{name_str.strip()}"
                                f".{ext_str.strip()}' → "
                                f"loaded@${load_addr:06X} "
                                f"({bytes_loaded}B)\n")
                        cpu_ref.a[6] = load_addr
                        cpu_ref.d[7] = 4
                        cpu_ref.sr = (cpu_ref.sr & 0xFF00) | 0x04
                    else:
                        cpu_ref.sr = (cpu_ref.sr & 0xFF00) & ~0x04

                elif d6 in (0, 2):
                    # FETCH — read spec from A6 stack frame
                    n1 = n2 = ext = 0
                    if 0x1000 <= a6 < 0x100000:
                        n1 = bus.read_word(a6)
                        n2 = bus.read_word(a6 + 2)
                        ext = bus.read_word(a6 + 4)

                    found = None
                    if n1:
                        for ppn_w in [0x0104, 0x0B0B, 0x0202, 0x010B]:
                            found = _find_file_on_disk(
                                n1, n2, ext, ppn_w)
                            if found:
                                break

                    if found:
                        start_block, size_blocks, attr = found
                        data_addr = FILE_DATA_BASE
                        _load_file_data(
                            start_block, size_blocks, data_addr)
                        cpu_ref.a[6] = data_addr
                        cpu_ref.sr = (cpu_ref.sr & 0xFF00) | 0x04
                        cpu_ref.d[7] = 4
                    else:
                        cpu_ref.sr = (cpu_ref.sr & 0xFF00) & ~0x04
                else:
                    cpu_ref.sr = (cpu_ref.sr & 0xFF00) & ~0x04

                cpu_ref.pc = (pc + 2) & 0xFFFFFF
                return

            # --- Scheduler terminal output intercept ---
            # The scheduler's output routine at $00284A checks
            # $043C (terminal output driver pointer). When null,
            # BEQ at $002854 skips output. We intercept here to
            # capture and emit the character in D1.B.
            if (compat_mode and pc == 0x002854
                    and bus.read_long(0x043C) == 0):
                ch = cpu_ref.d[1] & 0xFF
                if ch and acia.tx_callback:
                    acia.tx_callback(0, ch)

            if not native_find_trace and opword == 0xA0CA:
                _ttyout_count[0] += 1
                ch = cpu_ref.d[1] & 0xFF
                if _ttyout_count[0] <= 30:
                    sys.stderr.write(
                        f"[OUT] ${ch:02X} "
                        f"'{chr(ch) if 32 <= ch < 127 else '?'}'\n")

            # --- TTYIN ($A072) intercept ---
            # Native OS handler at $20F0 is buggy: when TCB+$00 & 9
            # is set it skips the read-pointer advance, causing an
            # infinite re-read of the same character.  Intercept here
            # to properly consume from the TCB buffer.
            if compat_mode and opword == 0xA072:
                jcb = bus.read_long(0x041C) & 0xFFFFFF
                if jcb and jcb < 0x100000:
                    term = bus.read_long(jcb + 0x38) & 0xFFFFFF
                else:
                    term = 0
                if term and term < 0x100000:
                    rptr = bus.read_long(term + 0x1E) & 0xFFFFFF
                    count = bus.read_word(term + 0x12)
                    if count > 0 and rptr and rptr < 0x100000:
                        ch = bus.read_byte(rptr)
                        bus.write_long(term + 0x1E, rptr + 1)
                        new_count = count - 1
                        bus.write_word(term + 0x12, new_count)
                        cpu_ref.d[1] = (cpu_ref.d[1] & 0xFFFFFF00) | ch
                        # Clear TCB status when buffer is empty
                        # so TTYLIN will yield on next call
                        if new_count == 0:
                            bus.write_word(term, 0x0000)
                    else:
                        cpu_ref.d[1] = cpu_ref.d[1] & 0xFFFFFF00
                        bus.write_word(term, 0x0000)
                cpu_ref.pc = (pc + 2) & 0xFFFFFF
                return

            # --- IOINI ($A03C) intercept ---
            # --- DD.XFR intercept ---
            # Trace execution in the driver and SCZ.DVR areas
            if (not native_find_trace
                    and _ddb_populated[0] and _disk_img and (
                    (DISK_DRV_BASE <= pc < DISK_DRV_BASE + 0x10)
                    or (0xA100 <= pc <= 0xA900))):
                sys.stderr.write(
                    f"[DRV] PC=${pc:06X} "
                    f"op=${opword:04X}\n"
                )

            # --- AMOS32→AMOSL filename patch ---
            # The ROM/monitor hardcodes "AMOS32.INI" but our
            # disk has "AMOSL.INI".  Patch the TCB buffer text
            # before SCNFIL parses it into RAD50.
            if opword == 0xA008:  # CHAIN
                _chain_seen[0] = True

                # Populate DDB/DDT at CHAIN time — MONGEN equivalent.
                # The OS allocates ZSYDSK via GETMEM but doesn't populate
                # geometry because MONGEN config is missing from the .MON.
                # This is hardware/boot config, same category as MEMBAS.
                # Runs in both native and compat modes — MONGEN embeds
                # this at system generation time on real hardware.
                if not _ddb_populated[0] and _disk_img:
                    zsydsk = bus.read_long(0x040C) & 0xFFFFFF
                    if zsydsk and zsydsk < 0x100000:
                        # --- Diagnostic: dump pre-overwrite state ---
                        sys.stderr.write(
                            f"[DSK] PRE-OVERWRITE ZSYDSK=${zsydsk:06X}:\n")
                        for _doff in range(0, 0x100, 4):
                            _v = bus.read_long(zsydsk + _doff)
                            if _v != 0:
                                sys.stderr.write(
                                    f"  DDB+${_doff:02X}: ${_v:08X}\n")
                        _ddt = zsydsk + 0x100
                        for _doff in range(0, 0x100, 4):
                            _v = bus.read_long(_ddt + _doff)
                            if _v != 0:
                                sys.stderr.write(
                                    f"  DDT+${_doff:02X}: ${_v:08X}\n")
                        # --- end diagnostic ---
                        ddt_addr = zsydsk + 0x100
                        # Disk driver stub at DISK_DRV_BASE
                        # DD.NAM +$00/$02
                        bus.write_word(DISK_DRV_BASE + 0x00, 0x0000)
                        bus.write_word(DISK_DRV_BASE + 0x02, 0x0200)
                        # DD.XFR +$04: NOP+RTS
                        bus.write_word(DISK_DRV_BASE + 0x04, 0x4E71)
                        bus.write_word(DISK_DRV_BASE + 0x06, 0x4E75)
                        # DD.MNT +$08: NOP+RTS
                        bus.write_word(DISK_DRV_BASE + 0x08, 0x4E71)
                        bus.write_word(DISK_DRV_BASE + 0x0A, 0x4E75)
                        # DD.INI +$0C: NOP+RTS
                        bus.write_word(DISK_DRV_BASE + 0x0C, 0x4E71)
                        bus.write_word(DISK_DRV_BASE + 0x0E, 0x4E75)
                        # DDB fields
                        # DDB+$00 is the chain link (LONG).  The
                        # high byte doubles as DK.FLG.  Set chain
                        # to 0 (end of chain) with flags $0F.
                        bus.write_long(zsydsk, 0x00000000)
                        bus.write_long(zsydsk + 0x08, ddt_addr)
                        bus.write_long(zsydsk + 0x0C, 512)   # DK.BPS
                        bus.write_long(zsydsk + 0x10, 32)    # DK.SPT
                        bus.write_long(zsydsk + 0x14, 16)    # DK.SPC
                        bus.write_long(zsydsk + 0x20, 1)     # DK.MFD
                        bus.write_long(zsydsk + 0x24, 2)     # DK.BMP
                        bus.write_long(zsydsk + 0x28, 0)     # DK.PAR
                        bus.write_long(zsydsk + 0x2C, 61531) # DK.SIZ
                        bus.write_long(zsydsk + 0x7C, IO_BUF)
                        # DDT fields — point to real SCZ.DVR
                        # The OS loaded SCZ.DVR at $A104 as part
                        # of AMOSL.MON.  It handles SCSI I/O via
                        # the hardware at $FFFFC8.
                        SCZ_DVR_ADDR = 0xA104
                        bus.write_word(ddt_addr, 0x2000)  # DD.STS
                        bus.write_long(ddt_addr + 0x06,
                                       SCZ_DVR_ADDR)  # DD.DRV
                        # DD.NAM at +$34: device name for SRCH
                        # matching.  FIND converts DSK→LNK before
                        # calling SRCH, so DDT name must be LNK0.
                        # $4D3B = "LNK" in RAD50, unit 0.
                        bus.write_word(ddt_addr + 0x34, 0x1C03)  # DD.NAM = DSK
                        bus.write_long(ddt_addr + 0x84, 0)
                        # Link into system
                        bus.write_long(0x0408, zsydsk)  # DDBCHN
                        # SYSTEM ($0400) — MONGEN sets bit 31 to tell
                        # FIND that the disk subsystem is active.  The
                        # FIND code at $25F0 does TST.L ($0400) / BPL
                        # to skip disk search when SYSTEM is positive.
                        cur_sys = bus.read_long(0x0400)
                        # Bit 31: disk subsystem active (FIND check at $25F0)
                        # Bit 28: disk mounted/ready (FIND check at $1464)
                        bus.write_long(0x0400, cur_sys | 0x90000000)
                        # DEVTBL fix — FIND at $2604 converts the
                        # device name DSK ($1C03) → LNK ($4D3B)
                        # before calling SRCH.  SRCH's DEVTBL
                        # lookup at $5C58 then needs to find LNK,
                        # not DSK.  This is MONGEN-equivalent:
                        # on a real system MONGEN populates
                        # DEVTBL with the correct link device.
                        devtbl = bus.read_long(0x0404) & 0xFFFFFF
                        if not compat_mode and devtbl and devtbl < 0x100000:
                            # Native mode only: populate DEVTBL and
                            # SRCH globals for native FIND path.
                            # Compat mode uses Python FIND bypass and
                            # doesn't need these; writing here would
                            # corrupt .MON code at the DEVTBL address.
                            # DEVTBL is a linked list of nodes:
                            #   +$00: LONG link (0 = end)
                            #   +$06: WORD device name
                            # SRCH at $005C58 compares 6(A6) with D1.
                            # Single DSK node (no LNK indirection)
                            bus.write_long(devtbl + 0x00, 0)  # link=null
                            bus.write_word(devtbl + 0x04, 0x0000)  # unit
                            bus.write_word(devtbl + 0x06, 0x1C03)  # DSK
                            bus.write_word(devtbl + 0x08, 0x0000)
                            # SRCH global at $7776: driver dispatch ptr
                            bus.write_long(0x7776, SCZ_DVR_ADDR)
                            # JCB devspec DDB pointer — MONGEN embeds
                            # the system disk DDB address in the JCB's
                            # default devspec.  SRCH at $005AB6 tests
                            # A4+$28 (= devspec+$2E after FIND's +6
                            # ADDQ) and takes the shortcut path if set.
                            # JCB devspec: MONGEN populates the
                            # default device spec with disk info.
                            # FIND at $00257A tests (A5) — the first
                            # LONG of the devspec.  If zero, FIND
                            # skips the devspec and enters the LNK
                            # resolution loop.  Set devspec fields:
                            #   +$00: LONG non-zero (enables devspec)
                            #   +$2E: LONG DDB pointer (for SRCH shortcut)
                            pass
                        # Load MFD (block 2) into DDB I/O buffer.
                        # On real hardware, DD.MNT reads the MFD
                        # during boot as part of MONGEN's disk init.
                        # The MFD contains the master file directory
                        # that FIND needs to locate files.
                        if _disk_img:
                            mfd = _disk_img.read_sector(2)
                            if mfd:
                                for _bi in range(len(mfd)):
                                    bus.write_byte(
                                        IO_BUF + _bi, mfd[_bi])
                                sys.stderr.write(
                                    f"[DSK] MFD block 2 → "
                                    f"${IO_BUF:06X} "
                                    f"({len(mfd)} bytes)\n")
                        _ddb_populated[0] = True
                        sys.stderr.write(
                            f"[DSK] DDB=${zsydsk:06X} "
                            f"DDT=${ddt_addr:06X} "
                            f"drv=${SCZ_DVR_ADDR:06X} "
                            f"DEVTBL=${devtbl:06X}\n"
                        )

            if (opword == 0xA068  # SCNFIL
                    and _chain_seen[0]
                    and not _ini_patched[0]):
                a2 = cpu_ref.a[2] & 0xFFFFFF
                if 0x1000 <= a2 < 0x100000:
                    text = bytes(
                        bus._read_byte_physical(a2 + i)
                        for i in range(10))
                    if text == b"AMOS32.INI":
                        repl = b"AMOSL.INI\r\n\x00"
                        for i, b_val in enumerate(repl):
                            bus.write_byte(a2 + i, b_val)
                        _ini_patched[0] = True
                        sys.stderr.write(
                            "[INI] AMOS32→AMOSL patch\n")

            # --- Learn JCB/TCB on the TTYLIN yield ---
            # The first $A03E with D6=2 (terminal input wait) after
            # driver installation is the TTYLIN yield.  At that point,
            # JOBCUR still holds the correct JCB address and the TCB
            # has been allocated by terminal detect.
            if compat_mode and opword == 0xA03E and not _jobcur_addr[0]:
                d6 = cpu_ref.d[6] & 0xFFFF
                if d6 == 2:
                    jcb = bus.read_long(0x041C) & 0xFFFFFF
                    if jcb:
                        _jobcur_addr[0] = jcb
                        tcb = bus.read_long(jcb + 0x38) & 0xFFFFFF
                        _tcb_addr[0] = tcb
                        # Drain stale ACIA RX data (CR echo from
                        # terminal detect) so it doesn't pollute
                        # the first line of real stdin input.
                        acia._rdrf[0] = False
                        acia._rx_queue[0].clear()
                        sys.stderr.write(
                            f"[IO] JCB=${jcb:06X} TCB=${tcb:06X}\n"
                        )

                        # Inject filtered AMOSL.INI as terminal input.
                        # Skip commands that need device infrastructure
                        # we don't have (TRMDEF, MDO, LOAD TRMDEF).
                        if _ini_data and not _ini_injected[0]:
                            _ini_injected[0] = True
                            # Use minimal INI: only commands that
                            # work without full device infrastructure
                            _filtered = (
                                b"JOBS 8\r\n"
                                b"VER\r\n"
                            )
                            sys.stderr.write(
                                f"[INI] Filtered: "
                                f"{len(_ini_data)}→"
                                f"{len(_filtered)} bytes\n")
                            if _filtered:
                                acia.send_to_port(0, bytes(_filtered))
                            sys.stderr.write(
                                f"[INI] Injected {len(_ini_data)}"
                                f" bytes via ACIA\n"
                            )

            # --- Input bridging ---
            # When the scheduler is idle (PC in $1C90-$1CB6 range)
            # and ACIA has received data, inject a complete line
            # into the TCB input buffer and wake the sleeping job.
            # On real hardware the ACIA ISR fires per-byte and
            # stuffs chars into the TCB; the scheduler dispatches
            # the job after a CR/LF arrives.  We simulate that by
            # draining the ACIA queue up to the first line-ending
            # character before forcing dispatch.
            # Note: JCB bit $2000 ("runnable") is NOT cleared by the
            # yield handler, so we cannot use it as a guard.
            # Being in the idle loop range already proves the job is
            # not executing.
            if (compat_mode
                    and 0x1C90 <= pc <= 0x1CB6
                    and _jobcur_addr[0]
                    and acia._rdrf[0]):
                jcb = _jobcur_addr[0]
                tcb = (bus.read_long(jcb + 0x38) & 0xFFFFFF)
                if tcb < 0x1000:
                    tcb = _tcb_addr[0]

                injected = 0
                if tcb:
                    buf_ptr = (bus.read_long(tcb + 0x44)
                               & 0xFFFFFF)
                    buf_size = bus.read_long(tcb + 0x48)

                    # Reset TCB read/write state for new line
                    bus.write_word(tcb + 0x12, 0)        # count (WORD)
                    bus.write_long(tcb + 0x1E, buf_ptr)  # read ptr (LONG) = buffer start
                    write_pos = 0
                    count = 0

                    # Drain ACIA into TCB until CR/LF or
                    # buffer full.  Consume \r\n as a pair
                    # so each line is a single dispatch.
                    while (buf_ptr
                           and count < buf_size
                           and acia._rdrf[0]):
                        rx_char = acia._rx_data[0]
                        acia._rdrf[0] = False
                        if acia._rx_queue[0]:
                            acia._rx_data[0] = (
                                acia._rx_queue[0].popleft())
                            acia._rdrf[0] = True

                        bus.write_byte(
                            buf_ptr + write_pos, rx_char)
                        write_pos += 1
                        count += 1
                        injected += 1

                        if rx_char == 0x0D:
                            # Consume trailing LF if present
                            if (acia._rdrf[0]
                                    and acia._rx_data[0] == 0x0A):
                                acia._rdrf[0] = False
                                if acia._rx_queue[0]:
                                    acia._rx_data[0] = (
                                        acia._rx_queue[0]
                                        .popleft())
                                    acia._rdrf[0] = True
                            # Bug #39: Write LF after CR so TTYLIN
                            # recognizes line termination at $3436
                            if count < buf_size:
                                bus.write_byte(
                                    buf_ptr + write_pos, 0x0A)
                                write_pos += 1
                                count += 1
                            break
                        if rx_char == 0x0A:
                            break

                    # Read ptr stays at buf_ptr — $A072 advances it
                    bus.write_word(tcb + 0x12, count)
                    if injected:
                        bus.write_word(tcb, 0x0009)

                if injected:
                    jcb_status = bus.read_word(jcb)
                    bus.write_word(
                        jcb, jcb_status | 0x2000)
                    bus.write_long(0x041C, jcb)
                    cpu_ref.pc = 0x1CB8

        accel.hook(cpu_ref)
    logger = None
    trace_out = None
    if config.trace_enabled:
        if config.trace_file:
            trace_out = open(config.trace_file, "w")
        else:
            trace_out = sys.stderr
        logger = TraceLogger(trace_out)

    def _combined_trace_hook(cpu_ref):
        _accel_with_driver_hook(cpu_ref)
        if logger is not None:
            logger.trace_hook(cpu_ref)

    cpu.trace_hook = _combined_trace_hook

    # ACIA TX callback — print port 0 output to stdout
    def _tx_callback(port: int, byte_val: int) -> None:
        if port == 0:
            ch = bytes([byte_val])
            sys.stdout.buffer.write(ch)
            sys.stdout.buffer.flush()
            led.stdout_mid_line = (byte_val != 0x0A)  # not mid-line after \n

    acia.tx_callback = _tx_callback

    # Reset CPU (activates phantom, reads vectors)
    cpu.reset()
    sys.stderr.write(f"[BOOT] Mode={config.boot_mode}\n")
    sys.stderr.write(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}\n")

    # Buffer piped stdin upfront.  For pipes, all data is available
    # immediately but _jobcur_addr won't be set for millions of
    # instructions.  By then select() returns nothing (EOF).
    # For TTY stdin this reads nothing (no data yet).
    _stdin_buffer = bytearray()
    if not os.isatty(sys.stdin.fileno()):
        try:
            _stdin_buffer.extend(sys.stdin.buffer.read())
        except Exception:
            pass

    _stdin_fed = [False]  # True once buffered data has been sent

    # Set terminal to raw mode for interactive use
    old_term = _setup_terminal()

    # Main execution loop
    instruction_count = 0
    batch_size = 1000  # check terminal I/O every N instructions
    try:
        while True:
            if cpu.halted:
                sys.stderr.write("[HALT] CPU halted.\n")
                break

            cycles = cpu.step()
            bus.tick(cycles)

            instruction_count += 1
            if config.max_instructions and instruction_count >= config.max_instructions:
                sys.stderr.write(
                    f"[STOP] Reached {instruction_count} instructions limit.\n"
                )
                break

            # Check for breakpoints
            if cpu.pc in config.breakpoints:
                sys.stderr.write(
                    f"[BREAK] PC=${cpu.pc:06X} after {instruction_count} instructions\n"
                )
                _restore_terminal(old_term)
                _interactive_break(cpu)
                old_term = _setup_terminal()

            # Periodically check for terminal input.
            # Only start feeding stdin after the I/O bridging is active
            # (TTYLIN has yielded), so early boot ACIA echo isn't
            # clobbered by premature stdin data.
            if instruction_count % batch_size == 0 and _jobcur_addr[0]:
                # Feed buffered piped data on first opportunity
                if _stdin_buffer and not _stdin_fed[0]:
                    _stdin_fed[0] = True
                    acia.send_to_port(0, bytes(_stdin_buffer))
                    sys.stderr.write(
                        f"[IO] Fed {len(_stdin_buffer)} buffered stdin bytes\n"
                    )
                # Also check for live stdin (TTY mode)
                data = _check_stdin()
                if data:
                    # Ctrl-C or Ctrl-] to exit emulator
                    if b"\x03" in data or b"\x1d" in data:
                        sys.stderr.write("\n[EXIT] User interrupt.\n")
                        break
                    acia.send_to_port(0, data)

    except KeyboardInterrupt:
        sys.stderr.write(f"\n[INTERRUPTED] after {instruction_count} instructions\n")
    finally:
        _restore_terminal(old_term)
        if trace_out is not None and trace_out is not sys.stderr:
            trace_out.close()

    # Print final state
    sys.stderr.write(f"\n[FINAL] PC=${cpu.pc:06X}  SR=${cpu.sr:04X}\n")
    sys.stderr.write(
        f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}\n"
    )
    sys.stderr.write(
        f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}\n"
    )
    sys.stderr.write(
        f"  LED: {led.value:02X}  history: [{', '.join(f'{v:02X}' for v in led.history)}]\n"
    )
    sys.stderr.write(
        f"  Instructions: {instruction_count}  Cycles: {cpu.cycles}\n"
    )

    # Dump memory around final PC for debugging
    pc = cpu.pc
    sys.stderr.write(f"\n  Memory around PC=${pc:06X}:\n")
    for base in range(pc - 16, pc + 32, 16):
        words = []
        for off in range(0, 16, 2):
            addr = base + off
            try:
                w = bus.read_word(addr)
            except Exception:
                w = 0xDEAD
            words.append(f"{w:04X}")
        marker = " <-- PC" if base <= pc < base + 16 else ""
        sys.stderr.write(f"    ${base:06X}: {' '.join(words)}{marker}\n")


def _interactive_break(cpu: MC68010) -> None:
    """Simple interactive breakpoint handler."""
    while True:
        try:
            cmd = input("debug> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if cmd in ("c", "continue"):
            return
        elif cmd in ("r", "regs"):
            print(f"  PC=${cpu.pc:06X}  SR=${cpu.sr:04X}")
            print(f"  D: {' '.join(f'{cpu.d[i]:08X}' for i in range(8))}")
            print(f"  A: {' '.join(f'{cpu.a[i]:08X}' for i in range(8))}")
        elif cmd in ("q", "quit"):
            sys.exit(0)
        elif cmd in ("s", "step"):
            cpu.step()
            print(f"  PC=${cpu.pc:06X}  SR=${cpu.sr:04X}")
            return
        elif cmd in ("h", "help"):
            print("  c/continue  r/regs  s/step  q/quit  h/help")
        else:
            print(f"  Unknown command: {cmd}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AlphaSim — Alpha Microsystems AM-1200 Emulator"
    )
    parser.add_argument(
        "--rom-even", type=Path,
        default=Path("roms/AM-178-01-B05.BIN"),
        help="Path to ROM01 (even/high byte EPROM)"
    )
    parser.add_argument(
        "--rom-odd", type=Path,
        default=Path("roms/AM-178-00-B05.BIN"),
        help="Path to ROM00 (odd/low byte EPROM)"
    )
    parser.add_argument(
        "--ram", type=lambda x: int(x, 0),
        default=0x400000,
        help="RAM size in bytes (default: 4MB)"
    )
    parser.add_argument(
        "--dip", type=lambda x: int(x, 0),
        default=0x0A,
        help="Config DIP switch value (default: 0x0A = SCSI for AM-178-05 ROM)"
    )
    parser.add_argument(
        "--disk", type=Path, default=None,
        help="Path to disk image file"
    )
    parser.add_argument(
        "--boot-monitor", type=str, default=None,
        help="Override the ROM's SYS monitor filename lookup (for example TEST4.MON)"
    )
    parser.add_argument(
        "--boot-mode",
        choices=("native", "compat"),
        default="native",
        help="Boot path to run: native hardware-first or legacy compatibility mode"
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run ROM self-test diagnostics (sets DIP bit 5)"
    )
    parser.add_argument(
        "--trace", action="store_true",
        help="Enable instruction trace"
    )
    parser.add_argument(
        "--trace-file", type=str, default=None,
        help="Write trace to file instead of stderr"
    )
    parser.add_argument(
        "--trace-native-find", action="store_true",
        help="Trace the first native FIND/SCSI path without altering execution"
    )
    parser.add_argument(
        "--trace-native-dispatch", action="store_true",
        help="Trace native driver-init and mount dispatch state without altering execution"
    )
    parser.add_argument(
        "--max-instructions", type=int, default=0,
        help="Maximum instructions to execute (0=unlimited)"
    )
    parser.add_argument(
        "--cpu-model",
        choices=["68010", "68020", "68030", "68040"],
        default="68010",
        help="Expose the requested CPU model to MOVEC-based monitor probes",
    )
    parser.add_argument(
        "--break", dest="breakpoints", type=str, nargs="*", default=[],
        help="Breakpoint addresses (hex, e.g. 800018)"
    )

    args = parser.parse_args()

    bp_list = [int(b, 16) for b in args.breakpoints]

    dip_value = args.dip
    if args.self_test:
        dip_value |= 0x20  # Set bit 5 for diagnostic mode

    config = SystemConfig(
        rom_even_path=args.rom_even,
        rom_odd_path=args.rom_odd,
        ram_size=args.ram,
        config_dip=dip_value,
        disk_image_path=args.disk,
        boot_monitor_override=args.boot_monitor,
        boot_mode=args.boot_mode,
        cpu_model=args.cpu_model,
        trace_enabled=args.trace,
        trace_file=args.trace_file,
        native_find_trace=args.trace_native_find,
        native_dispatch_trace=args.trace_native_dispatch,
        max_instructions=args.max_instructions,
        breakpoints=bp_list,
    )

    run(config)


if __name__ == "__main__":
    main()
