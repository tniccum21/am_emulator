#!/usr/bin/env python3
"""Trace the native LED=12 idle frontier and optional ACIA RX wake test."""

from __future__ import annotations

import argparse
from pathlib import Path

from alphasim.config import SystemConfig
from alphasim.devices.timer6840 import Timer6840
from alphasim.main import build_system


REPO_ROOT = Path(__file__).resolve().parent
ROM_EVEN = REPO_ROOT / "roms" / "AM-178-01-B05.BIN"
ROM_ODD = REPO_ROOT / "roms" / "AM-178-00-B05.BIN"
BOOT_IMAGE = REPO_ROOT / "images" / "HD0-V1.4C-Bootable-on-1400.img"

IDLE_PCS = set(range(0x001C90, 0x001CB8, 2))


def read_long(bus, addr: int) -> int:
    return (bus.read_word(addr) << 16) | bus.read_word(addr + 2)


def build_idle_system():
    config = SystemConfig(
        rom_even_path=ROM_EVEN,
        rom_odd_path=ROM_ODD,
        ram_size=0x400000,
        config_dip=0x0A,
        disk_image_path=BOOT_IMAGE,
        cpu_model="68020",
        trace_enabled=False,
        max_instructions=8_000_000,
        breakpoints=[],
    )
    cpu, bus, led, acia = build_system(config)
    timer = next(device for _, _, device in bus._devices if isinstance(device, Timer6840))
    return cpu, bus, led, acia, timer


def run_to_idle(cpu, bus, led, limit: int = 6_000_000) -> int:
    for instructions in range(limit):
        pc = cpu.pc & 0xFFFFFF
        if len(led.history) >= 7 and led.history[-1] == 0x12 and pc in IDLE_PCS:
            return instructions
        cycles = cpu.step()
        bus.tick(cycles)
    raise RuntimeError("did not reach LED=12 idle window")


def dump_state(cpu, bus, led, acia, timer) -> None:
    print(
        f"pc=${cpu.pc & 0xFFFFFF:06X} sr=${cpu.sr:04X} "
        f"leds={[f'{x:02X}' for x in led.history]}"
    )
    print(
        f"vector64=${read_long(bus, 64 * 4):08X} "
        f"vector65=${read_long(bus, 65 * 4):08X} "
        f"vector29=${read_long(bus, 29 * 4):08X} "
        f"vector30=${read_long(bus, 30 * 4):08X}"
    )
    for addr, name in (
        (0x0400, "SYSTEM"),
        (0x0404, "DEVTBL"),
        (0x0408, "DDBCHN"),
        (0x040C, "ZSYDSK"),
        (0x0414, "SYSBAS"),
        (0x041C, "JOBCUR"),
        (0x0462, "DRVVEC"),
    ):
        print(f"{name}=${read_long(bus, addr):08X}")
    print(
        f"ACIA0 CR=${acia._control[0]:02X} "
        f"RDRF={int(acia._rdrf[0])} IRQ={acia.get_interrupt_level()} "
        f"RX=${acia._rx_data[0]:02X}"
    )
    print(
        f"PTM CR1=${timer._cr1:02X} CR2=${timer._cr2:02X} CR3=${timer._cr3:02X} "
        f"latch={[f'{x:04X}' for x in timer._latch]} "
        f"counter={[f'{x:04X}' for x in timer._counter]} "
        f"flags={timer._irq_flag} pending={timer._interrupt_pending}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force-rx-irq",
        action="store_true",
        help="Force ACIA0 control to $95 and inject one byte at the idle loop.",
    )
    parser.add_argument(
        "--byte",
        default="V",
        help="Single byte to inject when --force-rx-irq is used.",
    )
    args = parser.parse_args()

    if len(args.byte) != 1:
        raise SystemExit("--byte must be exactly one character")

    cpu, bus, led, acia, timer = build_idle_system()
    cpu.reset()
    instructions = run_to_idle(cpu, bus, led)
    print(f"idle_reached_at={instructions}")
    dump_state(cpu, bus, led, acia, timer)

    if not args.force_rx_irq:
        return 0

    acia.write(0xFFFE20, 1, 0x95)
    acia.send_to_port(0, args.byte.encode("ascii"))
    print(f"forced_rx_irq byte={args.byte!r} irq={acia.get_interrupt_level()}")

    checkpoints = {1, 10, 100, 1_000, 10_000, 50_000, 100_000, 200_000}
    for step in range(1, 250_001):
        cycles = cpu.step()
        bus.tick(cycles)
        if step in checkpoints:
            print(
                f"step={step} pc=${cpu.pc & 0xFFFFFF:06X} sr=${cpu.sr:04X} "
                f"JOBCUR=${read_long(bus, 0x041C):08X} "
                f"RDRF={int(acia._rdrf[0])} IRQ={acia.get_interrupt_level()}"
            )

    dump_state(cpu, bus, led, acia, timer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
