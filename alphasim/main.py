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
from .devices.timer6840 import Timer6840
from .devices.rtc_msm5832 import RTC_MSM5832
from .storage.disk_image import DiskImage
from .storage.scsi_target import SCSITarget
from .debug.trace import TraceLogger


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

    # MC6840 PTM timer at $FFFE10-$FFFE1F (odd byte addresses)
    timer = Timer6840()
    bus.register_device(0xFFFE10, 0xFFFE1F, timer)

    # MC6850 ACIA serial ports at $FFFE20-$FFFE32
    acia = ACIA6850()
    bus.register_device(0xFFFE20, 0xFFFE32, acia)
    # HW.SER alias: console ACIA at $FFFFC8 (status) / $FFFFC9 (data)
    bus.register_device(0xFFFFC8, 0xFFFFC9, acia)

    # SASI/SCSI controller at $FFFFE0-$FFFFE7
    sasi = SASIController()
    bus.register_device(0xFFFFE0, 0xFFFFE7, sasi)

    # Connect disk image if provided
    if config.disk_image_path and config.disk_image_path.exists():
        disk = DiskImage(config.disk_image_path)
        target = SCSITarget(disk)
        sasi.target = target

    # CPU
    cpu = MC68010(bus)
    cpu.opcode_table = build_opcode_table()

    return cpu, bus, led, acia


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
    if not os.isatty(sys.stdin.fileno()):
        return b""
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        return os.read(sys.stdin.fileno(), 64)
    return b""


def run(config: SystemConfig) -> None:
    """Build system and run the emulation loop."""
    cpu, bus, led, acia = build_system(config)

    # Set up trace if requested
    if config.trace_enabled:
        if config.trace_file:
            trace_out = open(config.trace_file, "w")
        else:
            trace_out = sys.stderr
        logger = TraceLogger(trace_out)
        cpu.trace_hook = logger.trace_hook

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
    sys.stderr.write(f"[BOOT] SSP=${cpu.a[7]:08X}  PC=${cpu.pc:06X}\n")

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

            # Periodically check for terminal input
            if instruction_count % batch_size == 0:
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
        "--max-instructions", type=int, default=0,
        help="Maximum instructions to execute (0=unlimited)"
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
        trace_enabled=args.trace,
        trace_file=args.trace_file,
        max_instructions=args.max_instructions,
        breakpoints=bp_list,
    )

    run(config)


if __name__ == "__main__":
    main()
