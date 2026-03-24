"""Helpers for native boot integration tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from alphasim.config import SystemConfig
from alphasim.devices.sasi import SASIController
from alphasim.main import build_system


REPO_ROOT = Path(__file__).resolve().parents[2]
ROM_DIR = REPO_ROOT / "roms"
IMAGE_DIR = REPO_ROOT / "images"

ROM_EVEN = ROM_DIR / "AM-178-01-B05.BIN"
ROM_ODD = ROM_DIR / "AM-178-00-B05.BIN"
BOOT_IMAGE_CANDIDATES = (
    IMAGE_DIR / "AMOS_1-3_Boot_OS.img",
    IMAGE_DIR / "A13 Boot OS.img",
)


def roms_available() -> bool:
    return ROM_EVEN.exists() and ROM_ODD.exists()


def find_boot_image() -> Path | None:
    for path in BOOT_IMAGE_CANDIDATES:
        if path.exists():
            return path
    return None


def require_native_boot_assets() -> bool:
    return roms_available() and find_boot_image() is not None


def find_amosl_ini_start_block() -> int | None:
    """Return the first AMOS logical block for AMOSL.INI if discoverable locally."""
    try:
        import sys

        sys.path.insert(0, "/Volumes/RAID0/repos/Alpha-Python/lib")
        from Alpha_Disk_Lib import AlphaDisk
    except Exception:
        return None

    image = find_boot_image()
    if image is None:
        return None

    try:
        with AlphaDisk(str(image)) as disk:
            dev = disk.get_logical_device(0)
            ufd = dev.read_user_file_directory((1, 4))
            for entry in ufd.get_active_entries():
                if entry.filename == "AMOSL" and entry.extension == "INI":
                    return int(entry.first_block)
    except Exception:
        return None

    return None


@dataclass(frozen=True)
class DiskRead:
    lba: int
    count: int
    size: int | None


class RecordingTarget:
    """Wrap a disk backend and record native read activity."""

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


@dataclass(frozen=True)
class BootRunResult:
    completed: bool
    instructions: int
    cycles: int
    pc: int
    led_history: tuple[int, ...]


def build_native_boot_system(
    disk_image_path: Path | None = None,
    config_dip: int = 0x0A,
    cpu_model: str = "68010",
):
    config = SystemConfig(
        rom_even_path=ROM_EVEN,
        rom_odd_path=ROM_ODD,
        ram_size=0x400000,
        config_dip=config_dip,
        disk_image_path=disk_image_path,
        cpu_model=cpu_model,
    )
    cpu, bus, led, acia = build_system(config)
    sasi = next(
        device
        for _, _, device in bus._devices
        if isinstance(device, SASIController)
    )
    return cpu, bus, led, acia, sasi


def run_native_boot(cpu, bus, led, stop: Callable[[], bool], max_instructions: int) -> BootRunResult:
    instructions = 0
    while instructions < max_instructions and not cpu.halted:
        cycles = cpu.step()
        bus.tick(cycles)
        instructions += 1
        if stop():
            return BootRunResult(
                completed=True,
                instructions=instructions,
                cycles=cpu.cycles,
                pc=cpu.pc,
                led_history=tuple(led.history),
            )

    return BootRunResult(
        completed=False,
        instructions=instructions,
        cycles=cpu.cycles,
        pc=cpu.pc,
        led_history=tuple(led.history),
    )
