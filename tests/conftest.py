"""Shared test fixtures for AlphaSim."""

import pytest
from pathlib import Path

from alphasim.bus.memory_bus import MemoryBus
from alphasim.cpu.mc68010 import MC68010
from alphasim.cpu.opcodes import build_opcode_table
from alphasim.devices.ram import RAM
from alphasim.devices.rom import ROM
from alphasim.devices.led import LED
from alphasim.devices.config_dip import ConfigDIP
from alphasim.devices.sasi import SASIController


ROM_DIR = Path(__file__).parent.parent / "roms"
ROM_EVEN = ROM_DIR / "AM-178-01-B05.BIN"
ROM_ODD = ROM_DIR / "AM-178-00-B05.BIN"


@pytest.fixture
def bus():
    return MemoryBus()


@pytest.fixture
def ram():
    return RAM(0x400000)


@pytest.fixture
def opcode_table():
    return build_opcode_table()


@pytest.fixture
def full_system():
    """Complete system: bus + RAM + ROM + LED + DIP + SASI + CPU with opcodes."""
    bus = MemoryBus()
    ram = RAM(0x400000)
    bus.set_ram(ram)

    if ROM_EVEN.exists() and ROM_ODD.exists():
        rom = ROM(ROM_EVEN, ROM_ODD)
        bus.set_rom(rom)
    else:
        rom = None

    led = LED()
    bus.register_device(0xFFFE00, 0xFFFE00, led)

    dip = ConfigDIP(0x0A)
    bus.register_device(0xFFFE03, 0xFFFE03, dip)

    sasi = SASIController()
    bus.register_device(0xFFFFE0, 0xFFFFE7, sasi)

    cpu = MC68010(bus)
    cpu.opcode_table = build_opcode_table()

    return cpu, bus, ram, rom, led, dip, sasi
