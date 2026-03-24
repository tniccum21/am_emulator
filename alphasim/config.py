"""System configuration for AlphaSim emulator."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SystemConfig:
    """Configuration for an AM-1200 emulation session."""

    # ROM files (EPROM pair)
    rom_even_path: Path = Path("roms/AM-178-01-B05.BIN")  # D15-D8 (high to CPU)
    rom_odd_path: Path = Path("roms/AM-178-00-B05.BIN")   # D7-D0 (low to CPU)

    # RAM
    ram_size: int = 0x400000  # 4MB default

    # Config DIP switch value ($0A = SCSI boot for AM-178-05 ROM)
    config_dip: int = 0x0A

    # Disk image
    disk_image_path: Path | None = None

    # Boot behavior
    # native: hardware-first path, no Python trap/I/O/file bypasses
    # compat: legacy path with Python-side boot assistance still enabled
    boot_mode: str = "native"
    boot_monitor_override: str | None = None
    cpu_model: str = "68010"

    # Debug options
    trace_enabled: bool = False
    trace_file: str | None = None
    native_find_trace: bool = False
    native_dispatch_trace: bool = False
    breakpoints: list[int] = field(default_factory=list)
    max_instructions: int = 0  # 0 = unlimited

    # Execution
    instructions_per_tick: int = 1000
