#!/usr/bin/env python3
"""Trace native command-file/file-open flow with the init job kept runnable."""

from __future__ import annotations

import codecs
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import MethodType

sys.path.insert(0, ".")

from alphasim.config import SystemConfig
from alphasim.cpu.disassemble import disassemble_one
from alphasim.devices.sasi import SASIController
from alphasim.devices.timer6840 import Timer6840
from alphasim.main import build_system


REPO_ROOT = Path(__file__).resolve().parent
ROM_EVEN = REPO_ROOT / "roms" / "AM-178-01-B05.BIN"
ROM_ODD = REPO_ROOT / "roms" / "AM-178-00-B05.BIN"
BOOT_IMAGE = REPO_ROOT / "images" / "AMOS_1-3_Boot_OS.img"

DDT_ADDR = 0x7038
JOB_QUEUE = DDT_ADDR + 0x78
JCB_MOD_CHAIN = DDT_ADDR + 0x0C
JCB_CMD_FILE = DDT_ADDR + 0x20
JCB_TCB_PTR = DDT_ADDR + 0x38
JCB_NAME_DESC = DDT_ADDR + 0x3C
JCB_WORK_AREA = DDT_ADDR + 0x0A8A
JOBCUR = 0x041C
SYSBAS = 0x0414
TOPRAM = 0x0410

WATCH_PCS = {
    0x29D0,
    0x29F0,
    0x36F4,
    0x3710,
    0x3720,
    0x3748,
    0x37B8,
    0x375E,
    0x390A,
    0x392C,
    0x3932,
    0x1C30,
    0x1C58,
    0x1C60,
    0x1CC6,
    0x1CFE,
    0x1C6E,
    0x4A92,
    0x4A96,
    0x4AAA,
    0x4982,
    0x49A8,
    0x5042,
    0x49B2,
    0x49C0,
    0x4D8C,
    0x4D94,
    0x5140,
    0x5102,
    0x55AA,
    0x56A6,
    0x56D2,
    0x571C,
    0x06F6,
    0x0710,
    0x0FA2,
    0x1A8C,
    0x13D2,
    0x13FA,
}
WATCH_OPS = {
    0xA00A: "OPEN",
    0xA008: "TTYLIN",
    0xA072: "TTYIN",
    0xA01C: "SCNMOD",
    0xA03C: "QUEUEIO",
    0xA03E: "IOWAIT",
    0xA052: "A052",
    0xA064: "A064",
    0xA086: "A086",
    0xA0D0: "A0D0",
    0xA0DC: "A0DC",
}
PROMOTION_PCS = {
    0x54D0,
    0x55AA,
    0x55AC,
    0x5692,
    0x5696,
    0x5698,
    0x569C,
    0x56A0,
    0x56A2,
    0x56A6,
    0x56BC,
    0x56C0,
    0x56C4,
    0x56C8,
    0x56CA,
    0x56D2,
    0x56D4,
    0x56D6,
    0x56DA,
    0x56DE,
    0x571C,
}
DECISION_CHAIN_PCS = {
    0x54D0,
    0x54D2,
    0x54D6,
    0x54D8,
    0x54DC,
    0x54E0,
    0x54EA,
    0x54EE,
    0x54F2,
    0x54F4,
    0x54F8,
    0x5508,
    0x550C,
    0x5510,
    0x5518,
    0x551A,
    0x5102,
    0x5140,
} | set(range(0x50E2, 0x5142, 2))


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def raw_write_word(orig_write, addr: int, value: int) -> None:
    addr &= ~1
    orig_write(addr, value & 0xFF)
    orig_write(addr + 1, (value >> 8) & 0xFF)


def raw_write_long(orig_write, addr: int, value: int) -> None:
    raw_write_word(orig_write, addr, (value >> 16) & 0xFFFF)
    raw_write_word(orig_write, addr + 2, value & 0xFFFF)


def dump_text(bus, addr: int, max_len: int = 24) -> str:
    addr &= 0xFFFFFF
    if addr == 0 or addr >= 0x400000:
        return "<invalid>"
    chars: list[str] = []
    for offset in range(max_len):
        b = bus.read_byte(addr + offset)
        if b == 0:
            break
        chars.append(chr(b) if 0x20 <= b < 0x7F else ".")
    return "".join(chars)


def dump_bytes(bus, addr: int, max_len: int = 24) -> str:
    addr &= 0xFFFFFF
    if addr >= 0x400000:
        return "<invalid>"
    return " ".join(f"{bus.read_byte(addr + offset):02X}" for offset in range(max_len))


def decode_rad50_word(word: int) -> str:
    alphabet = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    a = word // 1600
    b = (word % 1600) // 40
    c = word % 40
    if a >= len(alphabet) or b >= len(alphabet) or c >= len(alphabet):
        return "???"
    return alphabet[a] + alphabet[b] + alphabet[c]


def dump_rad50_name(bus, addr: int) -> str:
    if addr == 0 or addr >= 0x400000:
        return "<invalid>"
    words = [bus.read_word(addr + offset) for offset in (0, 2, 4)]
    return "".join(decode_rad50_word(word) for word in words).rstrip()


def dump_le_rad50_name(bus, addr: int) -> str:
    addr &= 0xFFFFFF
    if addr == 0 or addr + 5 >= 0x400000:
        return "<invalid>"
    words = [
        bus.read_byte(addr + offset) | (bus.read_byte(addr + offset + 1) << 8)
        for offset in (0, 2, 4)
    ]
    return "".join(decode_rad50_word(word) for word in words).rstrip()


def dump_state(prefix: str, cpu, bus, last_lba: int | None, op: int | None = None) -> None:
    opname = f" op=${op:04X}" if op is not None else ""
    print(
        f"{prefix} pc=${cpu.pc:06X}{opname} D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} "
        f"D6=${cpu.d[6]:08X} A0=${cpu.a[0]&0xFFFFFF:06X} A1=${cpu.a[1]&0xFFFFFF:06X} "
        f"A2=${cpu.a[2]&0xFFFFFF:06X} A3=${cpu.a[3]&0xFFFFFF:06X} "
        f"A5=${cpu.a[5]&0xFFFFFF:06X} A6=${cpu.a[6]&0xFFFFFF:06X} "
        f"USP=${cpu.usp & 0xFFFFFFFF:08X} SSP=${cpu.ssp & 0xFFFFFFFF:08X} "
        f"TOPRAM=${read_long(bus, TOPRAM):08X} SYSBAS=${read_long(bus, SYSBAS):08X} "
        f"JOBCUR=${read_long(bus, JOBCUR):08X} JOBQ=${read_long(bus, JOB_QUEUE):08X} "
        f"JCB+$00=${bus.read_word(DDT_ADDR):04X} "
        f"JCB+$0C=${read_long(bus, JCB_MOD_CHAIN):08X} JCB+$20=${bus.read_word(JCB_CMD_FILE):04X} "
        f"JCB+$38=${read_long(bus, JCB_TCB_PTR):08X} last_lba={last_lba}"
    )


def dump_name_desc(bus) -> None:
    print(
        f"        JCB name desc ${JCB_NAME_DESC:06X}: "
        f"{dump_bytes(bus, JCB_NAME_DESC, 0x20)}"
    )
    print(
        f"        JCB name query='{dump_le_rad50_name(bus, JCB_NAME_DESC + 6)}'"
    )


def dump_work_area(bus) -> None:
    print(f"        JCB work area at ${JCB_WORK_AREA:06X}:")
    for offset in range(0, 0x30, 2):
        addr = JCB_WORK_AREA + offset
        print(f"          +${offset:02X} ${addr:06X} = ${bus.read_word(addr):04X}")


def dump_tcb_state(bus) -> None:
    tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
    if tcb == 0 or tcb >= 0x400000:
        print(f"        TCB = <invalid ${tcb:06X}>")
        return
    cur_ptr = read_long(bus, tcb + 0x1E) & 0xFFFFFF
    buf_ptr = read_long(bus, tcb + 0x44) & 0xFFFFFF
    buf_size = read_long(bus, tcb + 0x48)
    print(
        f"        TCB at ${tcb:06X}: "
        f"+00=${bus.read_word(tcb):04X} "
        f"+12w=${bus.read_word(tcb + 0x12):04X} "
        f"+12l=${read_long(bus, tcb + 0x12):08X} "
        f"+16l=${read_long(bus, tcb + 0x16):08X} "
        f"+1A=${read_long(bus, tcb + 0x1A):08X} "
        f"+1E=${cur_ptr:06X} "
        f"+44=${buf_ptr:06X} "
        f"+48=${buf_size:08X}"
    )
    if 0 < cur_ptr < 0x400000:
        print(f"        TCB cur bytes={dump_bytes(bus, cur_ptr, 24)}")
        print(f"        TCB cur text='{dump_text(bus, cur_ptr, 24)}'")
    if 0 < buf_ptr < 0x400000:
        print(f"        TCB buf bytes={dump_bytes(bus, buf_ptr, 24)}")
        print(f"        TCB buf text='{dump_text(bus, buf_ptr, 24)}'")


def dump_a4_ddb(cpu, bus) -> None:
    a4 = cpu.a[4] & 0xFFFFFF
    if a4 == 0 or a4 >= 0x400000:
        print(f"        A4 DDB = <invalid ${a4:06X}>")
        return

    fields = (
        (0x00, "D.FLG/ERR", "word"),
        (0x02, "D.DEV", "word"),
        (0x04, "D.DRV", "word"),
        (0x06, "D.FIL", "long"),
        (0x0A, "D.EXT", "word"),
        (0x0C, "D.PPN", "word"),
        (0x0E, "D.REC", "long"),
        (0x12, "D.BUF", "long"),
        (0x16, "D.SIZ", "word"),
        (0x1E, "D.OPN", "word"),
        (0x20, "D.ARG", "long"),
        (0x26, "D.DVR", "long"),
    )
    print(f"        A4 DDB at ${a4:06X}:")
    for offset, name, kind in fields:
        addr = a4 + offset
        if kind == "long":
            value = read_long(bus, addr)
            print(f"          +${offset:02X} ${addr:06X} {name:<10} = ${value:08X}")
        else:
            value = bus.read_word(addr)
            print(f"          +${offset:02X} ${addr:06X} {name:<10} = ${value:04X}")


def dump_timer_state(timer: Timer6840) -> None:
    print(
        "        PTM "
        f"CR1=${timer._cr1:02X} CR2=${timer._cr2:02X} CR3=${timer._cr3:02X} "
        f"flags={[int(x) for x in timer._irq_flag]} "
        f"armed={[int(x) for x in timer._clearing_armed]} "
        f"pending={int(timer._interrupt_pending)} enabled={int(timer._timers_enabled)}"
    )
    print(
        "        PTM "
        f"counter={[f'{x:04X}' for x in timer._counter]} "
        f"latch={[f'{x:04X}' for x in timer._latch]}"
    )


def dump_disasm_window(bus, start: int, count: int = 12) -> None:
    pc = start & 0xFFFFFF
    print(f"        Disassembly from ${pc:06X}:")
    for _ in range(count):
        try:
            op = bus.read_word(pc)
            text, size = disassemble_one(bus, pc)
        except Exception:
            print(f"          ${pc:06X}: ???? ???")
            break
        print(f"          ${pc:06X}: ${op:04X} {text}")
        pc = (pc + size) & 0xFFFFFF


def try_disasm(bus, addr: int) -> str:
    try:
        text, _ = disassemble_one(bus, addr)
    except Exception:
        return "???"
    return text


@dataclass(frozen=True)
class DiskRead:
    lba: int
    count: int
    size: int | None


class RecordingTarget:
    def __init__(self, backend) -> None:
        self._backend = backend
        self.read_calls: list[DiskRead] = []

    def read_sectors(self, lba: int, count: int) -> bytes | None:
        data = self._backend.read_sectors(lba, count)
        self.read_calls.append(
            DiskRead(lba=lba, count=count, size=len(data) if data is not None else None)
        )
        return data

    @property
    def sector_count(self) -> int:
        return self._backend.sector_count


def main() -> int:
    force_68000_after_cmd_drain = "--68000-after-cmd-drain" in sys.argv
    force_a060_gate = "--force-a060-gate" in sys.argv
    preserve_desc12_at_1d14 = "--preserve-desc12-at-1d14" in sys.argv
    promote_a086_to_desc12 = "--promote-a086-to-desc12" in sys.argv
    promote_a086_to_a060_block = "--promote-a086-to-a060-block" in sys.argv
    prime_a086_target_from_desc = "--prime-a086-target-from-desc" in sys.argv
    trace_decision_chain = "--trace-decision-chain" in sys.argv
    trace_tcb = "--trace-tcb" in sys.argv
    trace_usp_history = "--trace-usp-history" in sys.argv
    emulate_ttyin_consume = "--emulate-ttyin-consume" in sys.argv
    pc_seeds_once = "--pc-seeds-once" in sys.argv
    max_after_fix = 4_000_000
    stop_pc: int | None = None
    stop_pc_occurrence: tuple[int, int] | None = None
    stop_pc_when_cmd_drained = False
    suppress_timer_after_a060 = 0
    suppress_timer_at_pc: list[tuple[int, int]] = []
    trace_a086_occurrences: list[int] = []
    trace_a0d0_occurrences: list[int] = []
    force_a086_d1: int | None = None
    force_a086_d6: int | None = None
    force_a086_a1: int | None = None
    force_a6_at_56bc: int | None = None
    bypass_a064_when_cmd_drained = False
    stop_window_before = 0x10
    stop_window_count = 16
    seed_long_writes: list[tuple[int, int]] = []
    desc_long_writes_at_54d0: list[tuple[int, int]] = []
    mem_byte_writes_at_pc: list[tuple[int, int, int]] = []
    mem_word_writes_at_pc: list[tuple[int, int, int]] = []
    mem_long_writes_at_pc: list[tuple[int, int, int]] = []
    persistent_mem_long_writes_at_pc: list[tuple[int, int, int]] = []
    advance_tcb_cur_at_pc: list[int] = []
    inject_tcb_text_at_pc_occurrence: list[tuple[int, int, bytes]] = []
    desc_byte_writes_at_pc: list[tuple[int, int, int]] = []
    desc_word_writes_at_pc: list[tuple[int, int, int]] = []
    desc_long_writes_at_pc: list[tuple[int, int, int]] = []
    reg_writes_at_pc: list[tuple[int, str, int]] = []
    persistent_reg_writes_at_pc: list[tuple[int, str, int]] = []
    watch_write_ranges: list[tuple[int, int]] = []
    suppress_write_ranges_at_pc: list[tuple[int, int, int]] = []
    suppress_write_ranges_at_pc_when: list[tuple[str, int, int, int]] = []
    for arg in sys.argv[1:]:
        if arg.startswith("--max-after-fix="):
            max_after_fix = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--stop-pc="):
            stop_pc = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--stop-pc-occurrence="):
            pc_text, count_text = arg.split("=", 1)[1].split(":", 1)
            stop_pc_occurrence = (int(pc_text, 0), int(count_text, 0))
        elif arg.startswith("--suppress-timer-after-a060="):
            suppress_timer_after_a060 = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--suppress-timer-at-pc="):
            pc_text, steps_text = arg.split("=", 1)[1].split(":", 1)
            suppress_timer_at_pc.append((int(pc_text, 0), int(steps_text, 0)))
        elif arg.startswith("--trace-a086-occurrence="):
            trace_a086_occurrences.append(int(arg.split("=", 1)[1], 0))
        elif arg.startswith("--trace-a0d0-occurrence="):
            trace_a0d0_occurrences.append(int(arg.split("=", 1)[1], 0))
        elif arg == "--stop-pc-when-cmd-drained":
            stop_pc_when_cmd_drained = True
        elif arg.startswith("--force-a086-d1="):
            force_a086_d1 = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--force-a086-d6="):
            force_a086_d6 = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--force-a086-a1="):
            force_a086_a1 = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--force-a6-at-56bc="):
            force_a6_at_56bc = int(arg.split("=", 1)[1], 0)
        elif arg == "--bypass-a064-when-cmd-drained":
            bypass_a064_when_cmd_drained = True
        elif arg.startswith("--stop-window-before="):
            stop_window_before = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--stop-window-count="):
            stop_window_count = int(arg.split("=", 1)[1], 0)
        elif arg.startswith("--seed-long="):
            addr_text, value_text = arg.split("=", 1)[1].split(":", 1)
            seed_long_writes.append((int(addr_text, 0), int(value_text, 0)))
        elif arg.startswith("--seed-desc-long-at-54d0="):
            offset_text, value_text = arg.split("=", 1)[1].split(":", 1)
            desc_long_writes_at_54d0.append((int(offset_text, 0), int(value_text, 0)))
        elif arg.startswith("--seed-desc-byte-at-pc="):
            pc_text, offset_text, value_text = arg.split("=", 1)[1].split(":", 2)
            desc_byte_writes_at_pc.append(
                (int(pc_text, 0), int(offset_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--seed-byte-at-pc="):
            pc_text, addr_text, value_text = arg.split("=", 1)[1].split(":", 2)
            mem_byte_writes_at_pc.append(
                (int(pc_text, 0), int(addr_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--seed-word-at-pc="):
            pc_text, addr_text, value_text = arg.split("=", 1)[1].split(":", 2)
            mem_word_writes_at_pc.append(
                (int(pc_text, 0), int(addr_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--seed-word-series-at-pc="):
            pc_text, start_text, stride_text, count_text, value_text = arg.split(
                "=", 1
            )[1].split(":", 4)
            match_pc = int(pc_text, 0)
            start_addr = int(start_text, 0)
            stride = int(stride_text, 0)
            count = int(count_text, 0)
            value = int(value_text, 0)
            for series_idx in range(count):
                mem_word_writes_at_pc.append(
                    (match_pc, start_addr + (series_idx * stride), value)
                )
        elif arg.startswith("--seed-long-at-pc="):
            pc_text, addr_text, value_text = arg.split("=", 1)[1].split(":", 2)
            mem_long_writes_at_pc.append(
                (int(pc_text, 0), int(addr_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--seed-long-at-pc-always="):
            pc_text, addr_text, value_text = arg.split("=", 1)[1].split(":", 2)
            persistent_mem_long_writes_at_pc.append(
                (int(pc_text, 0), int(addr_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--advance-tcb-cur-at-pc="):
            advance_tcb_cur_at_pc.append(int(arg.split("=", 1)[1], 0))
        elif arg.startswith("--inject-tcb-text-at-pc-occurrence="):
            pc_text, count_text, text = arg.split("=", 1)[1].split(":", 2)
            inject_tcb_text_at_pc_occurrence.append(
                (
                    int(pc_text, 0),
                    int(count_text, 0),
                    codecs.decode(text.encode("utf-8"), "unicode_escape").encode(
                        "ascii", errors="replace"
                    ),
                )
            )
        elif arg.startswith("--seed-desc-word-at-pc="):
            pc_text, offset_text, value_text = arg.split("=", 1)[1].split(":", 2)
            desc_word_writes_at_pc.append(
                (int(pc_text, 0), int(offset_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--seed-desc-long-at-pc="):
            pc_text, offset_text, value_text = arg.split("=", 1)[1].split(":", 2)
            desc_long_writes_at_pc.append(
                (int(pc_text, 0), int(offset_text, 0), int(value_text, 0))
            )
        elif arg.startswith("--force-reg-at-pc="):
            pc_text, reg_name, value_text = arg.split("=", 1)[1].split(":", 2)
            reg_writes_at_pc.append((int(pc_text, 0), reg_name.lower(), int(value_text, 0)))
        elif arg.startswith("--force-reg-at-pc-always="):
            pc_text, reg_name, value_text = arg.split("=", 1)[1].split(":", 2)
            persistent_reg_writes_at_pc.append(
                (int(pc_text, 0), reg_name.lower(), int(value_text, 0))
            )
        elif arg.startswith("--watch-write-range="):
            addr_text, size_text = arg.split("=", 1)[1].split(":", 1)
            watch_write_ranges.append((int(addr_text, 0), int(size_text, 0)))
        elif arg.startswith("--suppress-write-range-at-pc="):
            pc_text, addr_text, size_text = arg.split("=", 1)[1].split(":", 2)
            suppress_write_ranges_at_pc.append(
                (int(pc_text, 0), int(addr_text, 0), int(size_text, 0))
            )
        elif arg.startswith("--suppress-write-range-at-pc-when="):
            guard_name, pc_text, addr_text, size_text = arg.split("=", 1)[1].split(":", 3)
            suppress_write_ranges_at_pc_when.append(
                (guard_name, int(pc_text, 0), int(addr_text, 0), int(size_text, 0))
            )

    config = SystemConfig(
        rom_even_path=ROM_EVEN,
        rom_odd_path=ROM_ODD,
        ram_size=0x400000,
        config_dip=0x0A,
        disk_image_path=BOOT_IMAGE,
        trace_enabled=False,
        max_instructions=80_000_000,
        breakpoints=[],
    )
    cpu, bus, led, acia = build_system(config)
    sasi = next(device for _, _, device in bus._devices if isinstance(device, SASIController))
    timer = next(device for _, _, device in bus._devices if isinstance(device, Timer6840))
    assert sasi.target is not None
    recording_target = RecordingTarget(sasi.target)
    sasi.target = recording_target

    orig_get_interrupt_level = acia.get_interrupt_level
    orig_timer_get_interrupt_level = timer.get_interrupt_level

    timer_suppressed_steps = 0

    def gated_get_interrupt_level(self) -> int:
        # Let the low-memory poll/read routine complete its paired data reads
        # before RX IRQ delivery; otherwise a stale echoed NUL survives to $3752.
        if 0x006C3E <= cpu.pc <= 0x006C64:
            return 0
        return orig_get_interrupt_level()

    acia.get_interrupt_level = MethodType(gated_get_interrupt_level, acia)

    def gated_timer_get_interrupt_level(self) -> int:
        if timer_suppressed_steps > 0:
            return 0
        return orig_timer_get_interrupt_level()

    timer.get_interrupt_level = MethodType(gated_timer_get_interrupt_level, timer)

    first_tx: tuple[int, int, int, int] | None = None
    tx_log: list[tuple[int, int, int, int]] = []
    queue_fixed = False
    cmd_drained = False
    a060_suppression_armed = False
    a060_entry_a6: int | None = None
    a060_block_addr: int | None = None
    a060_post_trace_left = 0
    a060_post_trace_count = 0
    a086_post_trace_left = 0
    a086_post_trace_count = 0
    a0d0_post_trace_left = 0
    a0d0_post_trace_count = 0
    a086_working_count = 0
    preserve_desc12_hits = 0
    a086_promotion_hits = 0
    a086_prime_hits = 0
    seed_long_hits = 0
    force_a6_56bc_hits = 0
    desc_seed_54d0_hits = 0
    desc_seed_pc_hits = 0
    reg_force_pc_hits = 0
    timer_pc_suppression_hits = 0
    tcb_cur_advance_hits = 0
    ttyin_emu_hits = 0
    tcb_text_injection_hits = 0
    a086_call_count = 0
    a0d0_call_count = 0
    last_a086_pc: int | None = None
    last_a086_pre_a4: int | None = None
    last_a086_pre_a6: int | None = None
    last_a086_a4: int | None = None
    last_a086_a6: int | None = None
    last_a086_a1: int | None = None
    last_a086_d1: int | None = None
    last_a086_d6: int | None = None
    last_a086_target: int | None = None
    saw_55aa = False
    saw_56bc = False
    saw_56d2 = False
    recent_exec = deque(maxlen=32)
    consumed_desc_seed_54d0: set[int] = set()
    consumed_desc_byte_seed_pc: set[int] = set()
    consumed_mem_byte_seed_pc: set[int] = set()
    consumed_mem_word_seed_pc: set[int] = set()
    consumed_mem_long_seed_pc: set[int] = set()
    consumed_tcb_advance_pc: set[int] = set()
    consumed_tcb_text_injections: set[int] = set()
    consumed_desc_word_seed_pc: set[int] = set()
    consumed_desc_long_seed_pc: set[int] = set()
    consumed_reg_force_pc: set[int] = set()
    pending_tcb_input = False

    def inject_tcb_text(text: bytes) -> bool:
        nonlocal tcb_text_injection_hits
        tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
        if not (0 < tcb < 0x400000):
            print(f"[TCB inject] invalid TCB=${tcb:06X}")
            return False
        buf_ptr = read_long(bus, tcb + 0x44) & 0xFFFFFF
        buf_size = read_long(bus, tcb + 0x48) & 0xFFFFFFFF
        if not (0 < buf_ptr < 0x400000):
            print(f"[TCB inject] invalid buffer=${buf_ptr:06X}")
            return False
        if not text:
            print("[TCB inject] empty text")
            return False
        if len(text) > buf_size:
            print(
                f"[TCB inject] text too long len={len(text)} buf_size={buf_size}"
            )
            return False
        for idx, ch in enumerate(text):
            orig_write(buf_ptr + idx, ch)
        if len(text) < buf_size:
            orig_write(buf_ptr + len(text), 0)
        raw_write_long(orig_write, tcb + 0x1E, buf_ptr)
        # Native TTYIN decrements TCB+$12 with SUBQ.L, so seed it as a long.
        raw_write_long(orig_write, tcb + 0x12, len(text))
        raw_write_long(orig_write, tcb + 0x1A, buf_size)
        tcb_text_injection_hits += 1
        print(
            f"[TCB inject] pc=${cpu.pc:06X} cycles={cpu.cycles} "
            f"TCB=${tcb:06X} buf=${buf_ptr:06X} len={len(text)} text={text!r}"
        )
        return True

    orig_write = bus._write_byte_physical

    def suppression_guard_matches(guard_name: str) -> bool:
        match guard_name:
            case "cmd_drained":
                return bus.read_word(JCB_CMD_FILE) == 0
            case "saw_55aa":
                return saw_55aa
            case "saw_56bc":
                return saw_56bc
            case "saw_56d2":
                return saw_56d2
            case "late_promotion":
                return saw_55aa or saw_56bc or saw_56d2
            case _:
                raise ValueError(f"unsupported suppression guard: {guard_name}")

    def wrapped_write(address: int, value: int) -> None:
        nonlocal first_tx, queue_fixed
        addr = address & 0xFFFFFF
        if first_tx is None and addr in (0xFFFE22, 0xFFFE26, 0xFFFE32, 0xFFFFC9):
            # The tracer wants the first native serial-data write as a
            # milestone for arming the queue self-link. Do not rely on the
            # host-facing tx_callback here: it intentionally ignores HW.SER.
            first_tx = (cpu.pc, cpu.cycles, 0, value & 0xFF)
        for match_pc, base, size in suppress_write_ranges_at_pc:
            if cpu.pc == match_pc and base <= addr < base + size:
                rel = addr - base
                print(
                    f"[Suppress write] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                    f"base=${base:06X} addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
                )
                return
        for guard_name, match_pc, base, size in suppress_write_ranges_at_pc_when:
            if not suppression_guard_matches(guard_name):
                continue
            if cpu.pc == match_pc and base <= addr < base + size:
                rel = addr - base
                print(
                    f"[Suppress write:{guard_name}] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                    f"base=${base:06X} addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
                )
                return
        orig_write(address, value)
        if first_tx is None:
            return
        if not queue_fixed and read_long(bus, JOB_QUEUE) == 0:
            raw_write_long(orig_write, JOB_QUEUE, DDT_ADDR)
            queue_fixed = True
            print(
                f"Forced JOBQ self-link at pc=${cpu.pc:06X} cycles={cpu.cycles} "
                f"JOBCUR=${read_long(bus, JOBCUR):08X}"
            )
        if queue_fixed and JOB_QUEUE <= addr <= JOB_QUEUE + 3 and read_long(bus, JOB_QUEUE) == 0:
            raw_write_long(orig_write, JOB_QUEUE, DDT_ADDR)
            print(f"Restored JOBQ self-link at pc=${cpu.pc:06X} cycles={cpu.cycles}")
        if JCB_NAME_DESC <= addr < JCB_NAME_DESC + 0x30:
            rel = addr - JCB_NAME_DESC
            print(
                f"[Name desc write] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                f"addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
            )
        if trace_tcb:
            tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
            if 0 < tcb < 0x400000 and tcb <= addr < tcb + 0x50:
                rel = addr - tcb
                print(
                    f"[TCB write] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                    f"tcb=${tcb:06X} addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
                )
            if 0 < tcb < 0x400000:
                buf = read_long(bus, tcb + 0x44) & 0xFFFFFF
                if 0 < buf < 0x400000 and buf <= addr < buf + 0x40:
                    rel = addr - buf
                    print(
                        f"[TCB buf write] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                        f"buf=${buf:06X} addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
                    )
        if a060_block_addr is not None and a060_block_addr <= addr < a060_block_addr + 0x40:
            rel = addr - a060_block_addr
            print(
                f"[A060 block write] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                f"addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
            )
        for base, size in watch_write_ranges:
            if base <= addr < base + size:
                rel = addr - base
                print(
                    f"[Watch write] pc=${cpu.pc:06X} cycles={cpu.cycles} "
                    f"base=${base:06X} addr=${addr:06X} +${rel:02X} value=${value & 0xFF:02X}"
                )

    bus._write_byte_physical = wrapped_write

    def emit_summary(reason: str, pc: int, last_lba: int | None, op: int | None = None) -> None:
        desc12 = read_long(bus, JCB_NAME_DESC + 0x12) & 0xFFFFFF
        print(
            "SUMMARY "
            f"reason={reason} "
            f"pc={pc:06X} "
            f"op={(f'{op:04X}' if op is not None else '----')} "
            f"last_lba={(last_lba if last_lba is not None else -1)} "
            f"reached_amosl_ini={int(last_lba is not None and last_lba >= 3335)} "
            f"desc12={desc12:06X} "
            f"a060_block={(f'{a060_block_addr:06X}' if a060_block_addr is not None else '000000')} "
            f"preserve_hits={preserve_desc12_hits} "
            f"a086_promotions={a086_promotion_hits} "
            f"a086_primes={a086_prime_hits} "
            f"seed_long_hits={seed_long_hits} "
            f"force_a6_56bc_hits={force_a6_56bc_hits} "
            f"desc_seed_54d0_hits={desc_seed_54d0_hits} "
            f"desc_seed_pc_hits={desc_seed_pc_hits} "
            f"reg_force_pc_hits={reg_force_pc_hits} "
            f"timer_pc_suppression_hits={timer_pc_suppression_hits} "
            f"tcb_cur_advance_hits={tcb_cur_advance_hits} "
            f"ttyin_emu_hits={ttyin_emu_hits} "
            f"tcb_text_injections={tcb_text_injection_hits} "
            f"last_a086_pc={(f'{last_a086_pc:06X}' if last_a086_pc is not None else '000000')} "
            f"last_a086_pre_a4={(f'{last_a086_pre_a4:06X}' if last_a086_pre_a4 is not None else '000000')} "
            f"last_a086_pre_a6={(f'{last_a086_pre_a6:06X}' if last_a086_pre_a6 is not None else '000000')} "
            f"last_a086_a4={(f'{last_a086_a4:06X}' if last_a086_a4 is not None else '000000')} "
            f"last_a086_a6={(f'{last_a086_a6:06X}' if last_a086_a6 is not None else '000000')} "
            f"last_a086_a1={(f'{last_a086_a1:06X}' if last_a086_a1 is not None else '000000')} "
            f"last_a086_d1={(f'{last_a086_d1:08X}' if last_a086_d1 is not None else '00000000')} "
            f"last_a086_d6={(f'{last_a086_d6:08X}' if last_a086_d6 is not None else '00000000')} "
            f"last_a086_target={(f'{last_a086_target:06X}' if last_a086_target is not None else '000000')} "
            f"saw_55aa={int(saw_55aa)} "
            f"saw_56bc={int(saw_56bc)} "
            f"saw_56d2={int(saw_56d2)}"
        )

    def tx_callback(port: int, value: int) -> None:
        nonlocal first_tx
        if first_tx is None:
            first_tx = (cpu.pc, cpu.cycles, port, value)
        if len(tx_log) < 32:
            tx_log.append((cpu.pc, cpu.cycles, port, value))

    acia.tx_callback = tx_callback
    cpu.reset()

    instructions = 0
    while instructions < config.max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1
        if first_tx is not None and queue_fixed:
            break

    if first_tx is None or not queue_fixed:
        print("Did not reach first TX + queue fix")
        return 1

    print(
        f"First TX at pc=${first_tx[0]:06X} cycles={first_tx[1]} "
        f"port={first_tx[2]} value=${first_tx[3]:02X}"
    )
    print(f"Frame mode initially 68000={cpu.use_68000_frames}")
    for addr, value in seed_long_writes:
        raw_write_long(orig_write, addr, value)
        seed_long_hits += 1
        print(f"[Seed long] addr=${addr:06X} value=${value:08X}")
    last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None
    dump_state("Initial:", cpu, bus, last_lba)

    pc_counts: dict[int, int] = {}
    op_counts: dict[int, int] = {}
    hot_pcs: dict[int, int] = {}
    prev_usp = cpu.usp & 0xFFFFFFFF
    prev_ssp = cpu.ssp & 0xFFFFFFFF
    prev_supervisor = cpu.supervisor

    for _ in range(max_after_fix):
        if cpu.halted:
            break

        pc = cpu.pc
        op = bus.read_word(pc)
        desc_ptr12 = read_long(bus, JCB_NAME_DESC + 0x12) & 0xFFFFFF
        last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None
        hot_pcs[pc] = hot_pcs.get(pc, 0) + 1
        current_pc_occurrence = hot_pcs[pc]
        if pc == 0x55AA:
            saw_55aa = True
        if pc == 0x56BC:
            saw_56bc = True
        if pc == 0x56D2:
            saw_56d2 = True
        recent_exec.append(
            (
                pc,
                op,
                cpu.a[4] & 0xFFFFFF,
                cpu.a[6] & 0xFFFFFF,
                cpu.d[0],
                cpu.d[1],
                cpu.d[6],
                cpu.d[7],
            )
        )

        if trace_usp_history and (op & 0xFFF0) == 0x4E60:
            reg = op & 0x7
            mnemonic = f"MOVE USP,A{reg}" if (op & 0x8) else f"MOVE A{reg},USP"
            print(
                f"[MOVE USP pre] pc=${pc:06X} cycles={cpu.cycles} {mnemonic} "
                f"A{reg}=${cpu.a[reg] & 0xFFFFFFFF:08X} "
                f"USP=${cpu.usp & 0xFFFFFFFF:08X} "
                f"SSP=${cpu.ssp & 0xFFFFFFFF:08X} "
                f"SR=${cpu.sr:04X}"
            )

        if pc in PROMOTION_PCS:
            print(
                f"[Promote pre] pc=${pc:06X} op=${op:04X} "
                f"{try_disasm(bus, pc):<28} A4=${cpu.a[4] & 0xFFFFFF:06X} "
                f"A6=${cpu.a[6] & 0xFFFFFF:06X} desc+12=${desc_ptr12:06X}"
            )

        decision_trace = None
        if (
            trace_decision_chain
            and pc in DECISION_CHAIN_PCS
            and bus.read_word(JCB_CMD_FILE) == 0
            and (cpu.a[4] & 0xFFFFFF) == JCB_NAME_DESC
        ):
            desc_word0 = bus.read_word(JCB_NAME_DESC)
            desc_rec = read_long(bus, JCB_NAME_DESC + 0x0E)
            desc_buf = read_long(bus, JCB_NAME_DESC + 0x12)
            desc_siz = bus.read_word(JCB_NAME_DESC + 0x16)
            desc_opn = bus.read_word(JCB_NAME_DESC + 0x1E)
            decision_trace = (
                pc,
                op,
                cpu.d[0],
                cpu.d[1],
                cpu.d[6],
                cpu.d[7],
                cpu.a[1] & 0xFFFFFF,
                cpu.a[5] & 0xFFFFFF,
                cpu.a[6] & 0xFFFFFF,
                desc_word0,
                desc_rec,
                desc_buf,
                desc_siz,
                desc_opn,
            )
            print(
                f"[Decision pre] pc=${pc:06X} op=${op:04X} {try_disasm(bus, pc):<28} "
                f"D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X} "
                f"A1=${cpu.a[1] & 0xFFFFFF:06X} A5=${cpu.a[5] & 0xFFFFFF:06X} "
                f"A6=${cpu.a[6] & 0xFFFFFF:06X} "
                f"desc+00=${desc_word0:04X} desc+0E=${desc_rec:08X} "
                f"desc+12=${desc_buf:08X} desc+16=${desc_siz:04X} desc+1E=${desc_opn:04X}"
            )

        for idx, (match_pc, offset, value) in enumerate(desc_byte_writes_at_pc):
            if pc_seeds_once and idx in consumed_desc_byte_seed_pc:
                continue
            if pc != match_pc:
                continue
            orig_write(JCB_NAME_DESC + offset, value & 0xFF)
            desc_seed_pc_hits += 1
            if pc_seeds_once:
                consumed_desc_byte_seed_pc.add(idx)
            print(
                f"[Seed desc byte @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"desc+${offset:02X}=${value & 0xFF:02X}"
            )
        for idx, (match_pc, addr, value) in enumerate(mem_byte_writes_at_pc):
            if pc_seeds_once and idx in consumed_mem_byte_seed_pc:
                continue
            if pc != match_pc:
                continue
            old_value = bus.read_byte(addr)
            orig_write(addr, value & 0xFF)
            reg_force_pc_hits += 1
            if pc_seeds_once:
                consumed_mem_byte_seed_pc.add(idx)
            print(
                f"[Seed byte @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"addr=${addr:06X} ${old_value:02X}->${value & 0xFF:02X}"
            )
        for idx, (match_pc, addr, value) in enumerate(mem_word_writes_at_pc):
            if pc_seeds_once and idx in consumed_mem_word_seed_pc:
                continue
            if pc != match_pc:
                continue
            old_value = bus.read_word(addr)
            raw_write_word(orig_write, addr, value)
            reg_force_pc_hits += 1
            if pc_seeds_once:
                consumed_mem_word_seed_pc.add(idx)
            print(
                f"[Seed word @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"addr=${addr:06X} ${old_value:04X}->${value & 0xFFFF:04X}"
            )
        for idx, (match_pc, addr, value) in enumerate(mem_long_writes_at_pc):
            if pc_seeds_once and idx in consumed_mem_long_seed_pc:
                continue
            if pc != match_pc:
                continue
            old_value = read_long(bus, addr)
            raw_write_long(orig_write, addr, value)
            reg_force_pc_hits += 1
            if pc_seeds_once:
                consumed_mem_long_seed_pc.add(idx)
                print(
                    f"[Seed long @pc] pc=${pc:06X} cycles={cpu.cycles} "
                    f"addr=${addr:06X} ${old_value:08X}->${value & 0xFFFFFFFF:08X}"
                )
        for match_pc, addr, value in persistent_mem_long_writes_at_pc:
            if pc != match_pc:
                continue
            old_value = read_long(bus, addr)
            raw_write_long(orig_write, addr, value)
            reg_force_pc_hits += 1
            print(
                f"[Seed long @pc always] pc=${pc:06X} cycles={cpu.cycles} "
                f"addr=${addr:06X} ${old_value:08X}->${value & 0xFFFFFFFF:08X}"
            )
        for inject_idx, (match_pc, occurrence, text) in enumerate(
            inject_tcb_text_at_pc_occurrence
        ):
            if inject_idx in consumed_tcb_text_injections:
                continue
            if pc != match_pc or current_pc_occurrence != occurrence:
                continue
            if inject_tcb_text(text):
                consumed_tcb_text_injections.add(inject_idx)
                if op == 0xA03E:
                    tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
                    raw_write_word(orig_write, tcb + 0x00, 0x0009)
                    print(
                        f"[TCB inject wake] pc=${pc:06X} cycles={cpu.cycles} "
                        f"TCB=${tcb:06X} status=$0009 skip A03E"
                    )
                    cpu.pc = (pc + 2) & 0xFFFFFFFF
                    instructions += 1
                    break
                tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
                raw_write_word(orig_write, tcb + 0x00, 0x0000)
                pending_tcb_input = True
                print(
                    f"[TCB inject pending] pc=${pc:06X} cycles={cpu.cycles} "
                    f"TCB=${tcb:06X} status=$0000"
                )
        if cpu.pc != pc:
            continue
        if pc in advance_tcb_cur_at_pc and (not pc_seeds_once or pc not in consumed_tcb_advance_pc):
            tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
            if 0 < tcb < 0x400000:
                old_cur = read_long(bus, tcb + 0x1E) & 0xFFFFFF
                new_cur = (old_cur + 1) & 0xFFFFFF
                raw_write_long(orig_write, tcb + 0x1E, new_cur)
                tcb_cur_advance_hits += 1
                if pc_seeds_once:
                    consumed_tcb_advance_pc.add(pc)
                print(
                    f"[Advance TCB+1E @pc] pc=${pc:06X} cycles={cpu.cycles} "
                    f"TCB=${tcb:06X} ${old_cur:06X}->${new_cur:06X}"
                )
        for idx, (match_pc, offset, value) in enumerate(desc_word_writes_at_pc):
            if pc_seeds_once and idx in consumed_desc_word_seed_pc:
                continue
            if pc != match_pc:
                continue
            raw_write_word(orig_write, JCB_NAME_DESC + offset, value)
            desc_seed_pc_hits += 1
            if pc_seeds_once:
                consumed_desc_word_seed_pc.add(idx)
            print(
                f"[Seed desc word @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"desc+${offset:02X}=${value & 0xFFFF:04X}"
            )
        for idx, (match_pc, offset, value) in enumerate(desc_long_writes_at_pc):
            if pc_seeds_once and idx in consumed_desc_long_seed_pc:
                continue
            if pc != match_pc:
                continue
            raw_write_long(orig_write, JCB_NAME_DESC + offset, value)
            desc_seed_pc_hits += 1
            if pc_seeds_once:
                consumed_desc_long_seed_pc.add(idx)
            print(
                f"[Seed desc long @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"desc+${offset:02X}=${value:08X}"
            )
        def apply_reg_force(reg_name: str, value: int) -> int:
            old_value: int
            if reg_name.startswith("a") and len(reg_name) == 2 and reg_name[1].isdigit():
                reg_idx = int(reg_name[1])
                old_value = cpu.a[reg_idx] & 0xFFFFFFFF
                cpu.a[reg_idx] = value
            elif reg_name.startswith("d") and len(reg_name) == 2 and reg_name[1].isdigit():
                reg_idx = int(reg_name[1])
                old_value = cpu.d[reg_idx] & 0xFFFFFFFF
                cpu.d[reg_idx] = value
            elif reg_name == "usp":
                old_value = cpu.usp & 0xFFFFFFFF
                cpu.usp = value
            elif reg_name == "ssp":
                old_value = cpu.ssp & 0xFFFFFFFF
                cpu.ssp = value
            else:
                raise ValueError(f"unsupported register name: {reg_name}")
            return old_value

        for force_idx, (match_pc, reg_name, value) in enumerate(reg_writes_at_pc):
            if pc_seeds_once and force_idx in consumed_reg_force_pc:
                continue
            if pc != match_pc:
                continue
            old_value = apply_reg_force(reg_name, value)
            reg_force_pc_hits += 1
            if pc_seeds_once:
                consumed_reg_force_pc.add(force_idx)
            print(
                f"[Force reg @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"{reg_name.upper()} ${old_value:08X}->${value & 0xFFFFFFFF:08X}"
            )
        for match_pc, reg_name, value in persistent_reg_writes_at_pc:
            if pc != match_pc:
                continue
            old_value = apply_reg_force(reg_name, value)
            reg_force_pc_hits += 1
            print(
                f"[Force reg @pc always] pc=${pc:06X} cycles={cpu.cycles} "
                f"{reg_name.upper()} ${old_value:08X}->${value & 0xFFFFFFFF:08X}"
            )

        if not cmd_drained and bus.read_word(JCB_CMD_FILE) == 0:
            cmd_drained = True
            dump_state("[CMD drained] ", cpu, bus, last_lba, op)
            dump_name_desc(bus)
            if force_68000_after_cmd_drain:
                cpu.use_68000_frames = True
                print(
                    f"        Switched to 68000 exception frames at pc=${cpu.pc:06X} "
                    f"cycles={cpu.cycles}"
                )

        if pc in WATCH_PCS:
            count = pc_counts.get(pc, 0) + 1
            pc_counts[pc] = count
            if count <= 10:
                dump_state(f"[PC {count}] ", cpu, bus, last_lba, op)
                if pc in (
                    0x29D0,
                    0x29F0,
                    0x4982,
                    0x49A8,
                    0x49B2,
                    0x49C0,
                    0x4D8C,
                    0x4D94,
                    0x5140,
                    0x55AA,
                    0x56A6,
                    0x56D2,
                    0x571C,
                    0x3720,
                    0x3748,
                    0x375E,
                    0x1C30,
                    0x1C58,
                    0x1C60,
                    0x1C6E,
                    0x1CC6,
                    0x1CFE,
                    0x4A92,
                    0x4A96,
                    0x4AAA,
                    0x5042,
                    0x06F6,
                    0x0710,
                    0x0FA2,
                    0x1A8C,
                ):
                    print(
                        f"        A6 text='{dump_text(bus, cpu.a[6])}' "
                        f"A1 text='{dump_text(bus, cpu.a[1])}'"
                    )
                if pc in (0x29D0, 0x29F0):
                    print(f"        A1 bytes={dump_bytes(bus, cpu.a[1], 16)}")
                    a0 = cpu.a[0] & 0xFFFFFF
                    if 0 < a0 < 0x400000:
                        print(
                            "        "
                            f"A0+0C=${read_long(bus, a0 + 0x0C):08X} "
                            f"A0+10=${bus.read_word(a0 + 0x10):04X} "
                            f"A0+20=${bus.read_word(a0 + 0x20):04X}"
                        )
                if trace_tcb and pc in (0x1E2C, 0x1E2E, 0x390A, 0x392C, 0x3932):
                    dump_tcb_state(bus)
                if pc == 0x1C30:
                    print(
                        f"        Query RAD50(le)='"
                        f"{dump_le_rad50_name(bus, (cpu.a[6] & 0xFFFFFF) + 6)}'"
                    )
                    dump_name_desc(bus)
                if pc == 0x3748:
                    print(
                        f"        A6 is JCB work area: "
                        f"{(cpu.a[6] & 0xFFFFFF) == JCB_WORK_AREA}"
                    )
                    dump_work_area(bus)
                    dump_a4_ddb(cpu, bus)
                if pc == 0x37B8:
                    arg_ptr = read_long(bus, cpu.a[6] & 0xFFFFFF)
                    print(f"        A064 arg ptr=${arg_ptr:08X}")
                    if 0 < arg_ptr < 0x400000:
                        print(
                            f"        A064 arg words="
                            f"{' '.join(f'{bus.read_word(arg_ptr + off):04X}' for off in range(0, 16, 2))}"
                        )
                if pc in (0x1CC6, 0x1CFE, 0x4982, 0x49A8, 0x49B2, 0x49C0, 0x4A92, 0x4A96, 0x4D8C, 0x4D94, 0x5042):
                    dump_name_desc(bus)
                if pc in (0x49B2, 0x4D8C, 0x4D94, 0x5140, 0x55AA, 0x56A6, 0x56D2, 0x571C):
                    dump_name_desc(bus)
                if pc in (0x4AAA, 0x06F6, 0x0710, 0x0FA2, 0x1A8C):
                    sp = cpu.a[7] & 0xFFFFFF
                    print(f"        SP=${sp:06X} stack={dump_bytes(bus, sp, 24)}")
                if pc in (0x0FA2, 0x13D2):
                    dump_timer_state(timer)

        if (
            suppress_timer_after_a060 > 0
            and force_a060_gate
            and pc == 0x4AAA
            and not a060_suppression_armed
        ):
            timer_suppressed_steps = suppress_timer_after_a060
            a060_suppression_armed = True
            print(
                f"[A060 call-site suppress] pc=${pc:06X} cycles={cpu.cycles} "
                f"steps={timer_suppressed_steps}"
            )

        if pc == 0x1A8C and a060_entry_a6 is None:
            a060_entry_a6 = cpu.a[6] & 0xFFFFFF
            if suppress_timer_after_a060 > 0 and not a060_suppression_armed:
                timer_suppressed_steps = suppress_timer_after_a060
                print(
                    f"[A060 timer suppress] pc=${pc:06X} cycles={cpu.cycles} "
                    f"steps={timer_suppressed_steps}"
                )

        if a060_entry_a6 is not None and a060_block_addr is None:
            candidate = read_long(bus, a060_entry_a6) & 0xFFFFFF
            if 0 < candidate < 0x400000:
                a060_block_addr = candidate
                a060_post_trace_left = 64
                print(
                    f"[A060 return block] pc=${pc:06X} cycles={cpu.cycles} "
                    f"a6=${a060_entry_a6:06X} block=${a060_block_addr:06X}"
                )
                print(
                    f"        block bytes={dump_bytes(bus, a060_block_addr, 32)}"
                )

        if a060_post_trace_left > 0 and a060_post_trace_count < 128:
            print(
                f"[A060 post {a060_post_trace_count + 1}] pc=${pc:06X} "
                f"op=${op:04X} A4=${cpu.a[4] & 0xFFFFFF:06X} "
                f"A6=${cpu.a[6] & 0xFFFFFF:06X} D0=${cpu.d[0]:08X} D1=${cpu.d[1]:08X} "
                f"D7=${cpu.d[7]:08X}"
            )
            if a060_block_addr is not None:
                print(f"        block bytes={dump_bytes(bus, a060_block_addr, 32)}")
            a060_post_trace_left -= 1
            a060_post_trace_count += 1

        if pc == 0x54D0:
            for idx, (offset, value) in enumerate(desc_long_writes_at_54d0):
                if pc_seeds_once and idx in consumed_desc_seed_54d0:
                    continue
                raw_write_long(orig_write, JCB_NAME_DESC + offset, value)
                desc_seed_54d0_hits += 1
                if pc_seeds_once:
                    consumed_desc_seed_54d0.add(idx)
                print(
                    f"[Seed desc @54D0] pc=${pc:06X} cycles={cpu.cycles} "
                    f"desc+${offset:02X}=${value:08X}"
                )

        for match_pc, steps in suppress_timer_at_pc:
            if pc != match_pc:
                continue
            if timer_suppressed_steps < steps:
                timer_suppressed_steps = steps
            timer_pc_suppression_hits += 1
            print(
                f"[Timer suppress @pc] pc=${pc:06X} cycles={cpu.cycles} "
                f"steps={steps} active={timer_suppressed_steps}"
            )

        if force_a6_at_56bc is not None and pc == 0x56BC:
            old_a6 = cpu.a[6] & 0xFFFFFF
            cpu.a[6] = force_a6_at_56bc
            force_a6_56bc_hits += 1
            print(
                f"[Force A6 @56BC] pc=${pc:06X} cycles={cpu.cycles} "
                f"A6 ${old_a6:06X}->${force_a6_at_56bc:06X}"
            )

        if op == 0xA064 and bypass_a064_when_cmd_drained and bus.read_word(JCB_CMD_FILE) == 0:
            arg_ptr = read_long(bus, cpu.a[6] & 0xFFFFFF) & 0xFFFFFF
            print(
                f"[A064 bypass] pc=${pc:06X} cycles={cpu.cycles} "
                f"A6=${cpu.a[6] & 0xFFFFFF:06X} arg_ptr=${arg_ptr:06X}"
            )
            cpu.sr = (cpu.sr & ~0x04) | 0x04
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            instructions += 1
            continue

        if op == 0xA03E and pending_tcb_input:
            tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
            raw_write_word(orig_write, tcb + 0x00, 0x0009)
            pending_tcb_input = False
            print(
                f"[TCB inject wake] pc=${pc:06X} cycles={cpu.cycles} "
                f"TCB=${tcb:06X} status=$0009 skip A03E"
            )
            cpu.pc = (pc + 2) & 0xFFFFFFFF
            instructions += 1
            continue

        if op == 0xA072 and emulate_ttyin_consume and bus.read_word(JCB_CMD_FILE) == 0:
            tcb = read_long(bus, JCB_TCB_PTR) & 0xFFFFFF
            if 0 < tcb < 0x400000:
                tcb_count = read_long(bus, tcb + 0x12)
                rd_ptr = read_long(bus, tcb + 0x1E) & 0xFFFFFF
                if tcb_count > 0 and 0 < rd_ptr < 0x400000:
                    pending_tcb_input = False
                    ch = bus.read_byte(rd_ptr)
                    raw_write_long(orig_write, tcb + 0x1E, (rd_ptr + 1) & 0xFFFFFF)
                    raw_write_long(orig_write, tcb + 0x12, (tcb_count - 1) & 0xFFFFFFFF)
                    cpu.d[1] = (cpu.d[1] & 0xFFFFFF00) | ch
                    ttyin_emu_hits += 1
                    print(
                        f"[TTYIN emulate] pc=${pc:06X} cycles={cpu.cycles} "
                        f"TCB=${tcb:06X} rd_ptr=${rd_ptr:06X} ch=${ch:02X} "
                        f"count ${tcb_count:08X}->${(tcb_count - 1) & 0xFFFFFFFF:08X}"
                    )
                    cpu.pc = (pc + 2) & 0xFFFFFFFF
                    instructions += 1
                    continue

        if op == 0xA086:
            a086_call_count += 1
            promote_target: int | None = None
            promote_label: str | None = None
            if promote_a086_to_a060_block and a060_block_addr is not None:
                promote_target = a060_block_addr
                promote_label = "a060-block"
            elif promote_a086_to_desc12 and desc_ptr12 not in (0, JCB_NAME_DESC):
                promote_target = desc_ptr12
                promote_label = "desc+12"
            pre_a4 = cpu.a[4] & 0xFFFFFF
            pre_a6 = cpu.a[6] & 0xFFFFFF
            if (
                promote_target is not None
                and (pre_a4 == JCB_NAME_DESC or pre_a6 == JCB_NAME_DESC)
            ):
                if prime_a086_target_from_desc:
                    for offset in range(0x28):
                        orig_write(promote_target + offset, bus.read_byte(JCB_NAME_DESC + offset))
                    a086_prime_hits += 1
                    print(
                        f"[A086 prime {promote_label}] pc=${pc:06X} cycles={cpu.cycles} "
                        f"target=${promote_target:06X}"
                    )
                cpu.a[4] = promote_target
                cpu.a[6] = promote_target
                a086_promotion_hits += 1
                print(
                    f"[A086 promote {promote_label}] pc=${pc:06X} cycles={cpu.cycles} "
                    f"A4 ${pre_a4:06X}->${promote_target:06X} "
                    f"A6 ${pre_a6:06X}->${promote_target:06X}"
                )
                last_a086_target = promote_target
            if force_a086_a1 is not None:
                old_a1 = cpu.a[1] & 0xFFFFFF
                cpu.a[1] = force_a086_a1
                print(
                    f"[A086 force A1] pc=${pc:06X} cycles={cpu.cycles} "
                    f"A1 ${old_a1:06X}->${force_a086_a1:06X}"
                )
            if force_a086_d1 is not None:
                old_d1 = cpu.d[1]
                cpu.d[1] = force_a086_d1
                print(
                    f"[A086 force D1] pc=${pc:06X} cycles={cpu.cycles} "
                    f"D1 ${old_d1:08X}->${force_a086_d1 & 0xFFFFFFFF:08X}"
                )
            if force_a086_d6 is not None:
                old_d6 = cpu.d[6]
                cpu.d[6] = force_a086_d6
                print(
                    f"[A086 force D6] pc=${pc:06X} cycles={cpu.cycles} "
                    f"D6 ${old_d6:08X}->${force_a086_d6 & 0xFFFFFFFF:08X}"
                )
            last_a086_pc = pc
            last_a086_pre_a4 = pre_a4
            last_a086_pre_a6 = pre_a6
            last_a086_a4 = cpu.a[4] & 0xFFFFFF
            last_a086_a6 = cpu.a[6] & 0xFFFFFF
            last_a086_a1 = cpu.a[1] & 0xFFFFFF
            last_a086_d1 = cpu.d[1]
            last_a086_d6 = cpu.d[6]
            if a086_call_count in trace_a086_occurrences:
                a086_post_trace_left = 48
                a086_post_trace_count = 0
                print(f"[A086 traced prelude {a086_call_count}]")
                for idx, (hpc, hop, ha4, ha6, d0, d1, d6, d7) in enumerate(recent_exec, 1):
                    print(
                        f"  {idx:02d}: pc=${hpc:06X} op=${hop:04X} "
                        f"A4=${ha4:06X} A6=${ha6:06X} "
                        f"D0=${d0:08X} D1=${d1:08X} D6=${d6:08X} D7=${d7:08X}"
                    )

        if op == 0xA0D0:
            a0d0_call_count += 1
            if a0d0_call_count in trace_a0d0_occurrences:
                a0d0_post_trace_left = 48
                a0d0_post_trace_count = 0
                print(f"[A0D0 traced prelude {a0d0_call_count}]")
                for idx, (hpc, hop, ha4, ha6, d0, d1, d6, d7) in enumerate(recent_exec, 1):
                    print(
                        f"  {idx:02d}: pc=${hpc:06X} op=${hop:04X} "
                        f"A4=${ha4:06X} A6=${ha6:06X} "
                        f"D0=${d0:08X} D1=${d1:08X} D6=${d6:08X} D7=${d7:08X}"
                    )

        if op == 0xA086 and (cpu.a[6] & 0xFFFFFF) == JCB_NAME_DESC:
            a086_post_trace_left = 48
            a086_post_trace_count = 0
            print("[A086 failing prelude]")
            for idx, (hpc, hop, ha4, ha6, d0, d1, d6, d7) in enumerate(recent_exec, 1):
                print(
                    f"  {idx:02d}: pc=${hpc:06X} op=${hop:04X} "
                    f"A4=${ha4:06X} A6=${ha6:06X} "
                    f"D0=${d0:08X} D1=${d1:08X} D6=${d6:08X} D7=${d7:08X}"
                )
        elif op == 0xA086 and a086_working_count < 3:
            a086_working_count += 1
            print(f"[A086 working prelude {a086_working_count}]")
            for idx, (hpc, hop, ha4, ha6, d0, d1, d6, d7) in enumerate(recent_exec, 1):
                print(
                    f"  {idx:02d}: pc=${hpc:06X} op=${hop:04X} "
                    f"A4=${ha4:06X} A6=${ha6:06X} "
                    f"D0=${d0:08X} D1=${d1:08X} D6=${d6:08X} D7=${d7:08X}"
                )

        if a086_post_trace_left > 0 and a086_post_trace_count < 96:
            print(
                f"[A086 post {a086_post_trace_count + 1}] pc=${pc:06X} "
                f"op=${op:04X} A4=${cpu.a[4] & 0xFFFFFF:06X} "
                f"A6=${cpu.a[6] & 0xFFFFFF:06X} D0=${cpu.d[0]:08X} "
                f"D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X}"
            )
            dump_name_desc(bus)
            a086_post_trace_left -= 1
            a086_post_trace_count += 1

        if a0d0_post_trace_left > 0 and a0d0_post_trace_count < 96:
            print(
                f"[A0D0 post {a0d0_post_trace_count + 1}] pc=${pc:06X} "
                f"op=${op:04X} A4=${cpu.a[4] & 0xFFFFFF:06X} "
                f"A6=${cpu.a[6] & 0xFFFFFF:06X} D0=${cpu.d[0]:08X} "
                f"D1=${cpu.d[1]:08X} D6=${cpu.d[6]:08X} D7=${cpu.d[7]:08X}"
            )
            dump_name_desc(bus)
            a0d0_post_trace_left -= 1
            a0d0_post_trace_count += 1

        if force_a060_gate and pc == 0x4A96:
            desc = cpu.a[4] & 0xFFFFFF
            old = bus.read_byte(desc + 1)
            if old & 0x40:
                orig_write(desc + 1, old & ~0x40)
                print(
                    f"[Gate clear] pc=${pc:06X} cycles={cpu.cycles} "
                    f"desc=${desc:06X} byte1 ${old:02X}->${old & ~0x40:02X}"
                )

        if op in WATCH_OPS:
            count = op_counts.get(op, 0) + 1
            op_counts[op] = count
            if count <= 12:
                dump_state(f"[{WATCH_OPS[op]} {count}] ", cpu, bus, last_lba, op)
                if op in (0xA00A, 0xA052, 0xA064, 0xA0DC):
                    print(
                        f"        A6 text='{dump_text(bus, cpu.a[6])}' "
                        f"A1 text='{dump_text(bus, cpu.a[1])}'"
                    )
                if op == 0xA086:
                    dump_name_desc(bus)
                if trace_tcb and op in (0xA008, 0xA072, 0xA01C, 0xA03E):
                    dump_tcb_state(bus)
                if op == 0xA0DC:
                    print(
                        f"        A6 is JCB work area: "
                        f"{(cpu.a[6] & 0xFFFFFF) == JCB_WORK_AREA}"
                    )
                    dump_work_area(bus)
                    dump_a4_ddb(cpu, bus)
                if op == 0xA064:
                    arg_ptr = read_long(bus, cpu.a[6] & 0xFFFFFF)
                    print(f"        A064 arg ptr=${arg_ptr:08X}")
                    if 0 < arg_ptr < 0x400000:
                        print(
                            f"        A064 arg words="
                            f"{' '.join(f'{bus.read_word(arg_ptr + off):04X}' for off in range(0, 16, 2))}"
                        )

        if last_lba is not None and last_lba >= 3335:
            print(f"\nReached AMOSL.INI area at LBA {last_lba}")
            dump_state("Final milestone:", cpu, bus, last_lba, op)
            emit_summary("reached_amosl_ini", cpu.pc, last_lba, op)
            return 0

        if (
            stop_pc is not None
            and pc == stop_pc
            and (not stop_pc_when_cmd_drained or bus.read_word(JCB_CMD_FILE) == 0)
        ):
            print(f"\nReached stop PC ${stop_pc:06X}")
            dump_state("Final milestone:", cpu, bus, last_lba, op)
            dump_name_desc(bus)
            print("        Recent execution:")
            for idx, (hpc, hop, ha4, ha6, d0, d1, d6, d7) in enumerate(recent_exec, 1):
                print(
                    f"          {idx:02d}: pc=${hpc:06X} op=${hop:04X} "
                    f"A4=${ha4:06X} A6=${ha6:06X} "
                    f"D0=${d0:08X} D1=${d1:08X} D6=${d6:08X} D7=${d7:08X}"
                )
            print(f"        PC bytes={dump_bytes(bus, pc, 32)}")
            if pc >= stop_window_before:
                dump_disasm_window(bus, pc - stop_window_before, stop_window_count)
            else:
                dump_disasm_window(bus, 0, stop_window_count)
            print(f"        A1 bytes={dump_bytes(bus, cpu.a[1], 32)}")
            print(f"        A2 bytes={dump_bytes(bus, cpu.a[2], 32)}")
            print(f"        A3 bytes={dump_bytes(bus, cpu.a[3], 32)}")
            print(f"        A5 bytes={dump_bytes(bus, cpu.a[5], 32)}")
            print(f"        A6 bytes={dump_bytes(bus, cpu.a[6], 32)}")
            if trace_tcb:
                dump_tcb_state(bus)
            sp = cpu.a[7] & 0xFFFFFF
            print(f"        SP=${sp:06X} stack={dump_bytes(bus, sp, 32)}")
            emit_summary("stop_pc", cpu.pc, last_lba, op)
            return 0

        if (
            stop_pc_occurrence is not None
            and pc == stop_pc_occurrence[0]
            and current_pc_occurrence == stop_pc_occurrence[1]
            and (not stop_pc_when_cmd_drained or bus.read_word(JCB_CMD_FILE) == 0)
        ):
            print(
                f"\nReached stop PC ${pc:06X} occurrence #{current_pc_occurrence}"
            )
            dump_state("Final milestone:", cpu, bus, last_lba, op)
            dump_name_desc(bus)
            print("        Recent execution:")
            for idx, (hpc, hop, ha4, ha6, d0, d1, d6, d7) in enumerate(recent_exec, 1):
                print(
                    f"          {idx:02d}: pc=${hpc:06X} op=${hop:04X} "
                    f"A4=${ha4:06X} A6=${ha6:06X} "
                    f"D0=${d0:08X} D1=${d1:08X} D6=${d6:08X} D7=${d7:08X}"
                )
            print(f"        PC bytes={dump_bytes(bus, pc, 32)}")
            if pc >= stop_window_before:
                dump_disasm_window(bus, pc - stop_window_before, stop_window_count)
            else:
                dump_disasm_window(bus, 0, stop_window_count)
            print(f"        A1 bytes={dump_bytes(bus, cpu.a[1], 32)}")
            print(f"        A2 bytes={dump_bytes(bus, cpu.a[2], 32)}")
            print(f"        A3 bytes={dump_bytes(bus, cpu.a[3], 32)}")
            print(f"        A5 bytes={dump_bytes(bus, cpu.a[5], 32)}")
            print(f"        A6 bytes={dump_bytes(bus, cpu.a[6], 32)}")
            if trace_tcb:
                dump_tcb_state(bus)
            sp = cpu.a[7] & 0xFFFFFF
            print(f"        SP=${sp:06X} stack={dump_bytes(bus, sp, 32)}")
            emit_summary("stop_pc_occurrence", cpu.pc, last_lba, op)
            return 0

        prev_pc = pc
        prev_op = op
        prev_a4 = cpu.a[4] & 0xFFFFFF
        prev_a6 = cpu.a[6] & 0xFFFFFF
        prev_desc_ptr12 = desc_ptr12
        cycles = cpu.step()
        bus.tick(cycles)
        if timer_suppressed_steps > 0:
            timer_suppressed_steps -= 1
        if trace_usp_history:
            new_usp = cpu.usp & 0xFFFFFFFF
            new_ssp = cpu.ssp & 0xFFFFFFFF
            new_supervisor = cpu.supervisor
            if (
                new_usp != prev_usp
                or new_ssp != prev_ssp
                or new_supervisor != prev_supervisor
            ):
                print(
                    f"[USP history] after pc=${prev_pc:06X} op=${prev_op:04X} "
                    f"SR=${cpu.sr:04X} supervisor={int(new_supervisor)} "
                    f"USP ${prev_usp:08X}->${new_usp:08X} "
                    f"SSP ${prev_ssp:08X}->${new_ssp:08X} "
                    f"A7=${cpu.a[7] & 0xFFFFFFFF:08X}"
                )
            prev_usp = new_usp
            prev_ssp = new_ssp
            prev_supervisor = new_supervisor
        cur_a4 = cpu.a[4] & 0xFFFFFF
        cur_a6 = cpu.a[6] & 0xFFFFFF
        cur_desc_ptr12 = read_long(bus, JCB_NAME_DESC + 0x12) & 0xFFFFFF
        if (
            preserve_desc12_at_1d14
            and prev_pc == 0x1D14
            and prev_desc_ptr12 != 0
            and cur_desc_ptr12 != prev_desc_ptr12
        ):
            raw_write_long(orig_write, JCB_NAME_DESC + 0x12, prev_desc_ptr12)
            preserve_desc12_hits += 1
            print(
                f"[Preserve desc+12] pc=${prev_pc:06X} cycles={cpu.cycles} "
                f"desc+12 ${cur_desc_ptr12:06X}->${prev_desc_ptr12:06X}"
            )
            cur_desc_ptr12 = prev_desc_ptr12
        if (
            prev_pc in PROMOTION_PCS
            or prev_desc_ptr12 != cur_desc_ptr12
            or prev_a4 != cur_a4
            or prev_a6 != cur_a6
        ):
            if (
                prev_pc in PROMOTION_PCS
                or (prev_desc_ptr12 != cur_desc_ptr12)
                or (prev_a4 == JCB_NAME_DESC and cur_a4 != prev_a4)
                or (prev_a6 == JCB_NAME_DESC and cur_a6 != prev_a6)
                or (cur_a4 == prev_desc_ptr12 and cur_a4 != prev_a4)
                or (cur_a6 == prev_desc_ptr12 and cur_a6 != prev_a6)
            ):
                print(
                    f"[Promote post] pc=${prev_pc:06X} op=${prev_op:04X} "
                    f"{try_disasm(bus, prev_pc):<28} "
                    f"A4 ${prev_a4:06X}->${cur_a4:06X} "
                    f"A6 ${prev_a6:06X}->${cur_a6:06X} "
                    f"desc+12 ${prev_desc_ptr12:06X}->${cur_desc_ptr12:06X}"
                )
        if decision_trace is not None:
            (
                trace_pc,
                trace_op,
                prev_d0,
                prev_d1,
                prev_d6,
                prev_d7,
                prev_a1,
                prev_a5,
                prev_a6_trace,
                prev_desc_word0,
                prev_desc_rec,
                prev_desc_buf,
                prev_desc_siz,
                prev_desc_opn,
            ) = decision_trace
            cur_desc_word0 = bus.read_word(JCB_NAME_DESC)
            cur_desc_rec = read_long(bus, JCB_NAME_DESC + 0x0E)
            cur_desc_buf = read_long(bus, JCB_NAME_DESC + 0x12)
            cur_desc_siz = bus.read_word(JCB_NAME_DESC + 0x16)
            cur_desc_opn = bus.read_word(JCB_NAME_DESC + 0x1E)
            print(
                f"[Decision post] pc=${trace_pc:06X} op=${trace_op:04X} next=${cpu.pc:06X} "
                f"D0 ${prev_d0:08X}->${cpu.d[0]:08X} "
                f"D1 ${prev_d1:08X}->${cpu.d[1]:08X} "
                f"D6 ${prev_d6:08X}->${cpu.d[6]:08X} "
                f"D7 ${prev_d7:08X}->${cpu.d[7]:08X} "
                f"A1 ${prev_a1:06X}->${cpu.a[1] & 0xFFFFFF:06X} "
                f"A5 ${prev_a5:06X}->${cpu.a[5] & 0xFFFFFF:06X} "
                f"A6 ${prev_a6_trace:06X}->${cpu.a[6] & 0xFFFFFF:06X} "
                f"desc+00 ${prev_desc_word0:04X}->${cur_desc_word0:04X} "
                f"desc+0E ${prev_desc_rec:08X}->${cur_desc_rec:08X} "
                f"desc+12 ${prev_desc_buf:08X}->${cur_desc_buf:08X} "
                f"desc+16 ${prev_desc_siz:04X}->${cur_desc_siz:04X} "
                f"desc+1E ${prev_desc_opn:04X}->${cur_desc_opn:04X}"
            )
        instructions += 1

    print("\nFinal:")
    last_lba = recording_target.read_calls[-1].lba if recording_target.read_calls else None
    dump_state("  ", cpu, bus, last_lba)
    print(f"  pc=${cpu.pc:06X} cycles={cpu.cycles} leds={[f'{x:02X}' for x in led.history]}")
    print("  TX log:")
    for pc, cycles, port, value in tx_log:
        print(f"    pc=${pc:06X} cycles={cycles} port={port} value=${value:02X}")
    print("  Hot PCs:")
    for pc, count in sorted(hot_pcs.items(), key=lambda item: item[1], reverse=True)[:16]:
        print(f"    ${pc:06X}: {count}")
    print("  Recent disk reads:")
    for read in recording_target.read_calls[-16:]:
        print(f"    LBA={read.lba} count={read.count} size={read.size}")
    dump_name_desc(bus)
    emit_summary("final", cpu.pc, last_lba)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
