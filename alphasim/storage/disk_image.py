"""Raw disk image backend for AlphaSim.

Provides sector-level read access to a flat disk image file.
Each sector is 512 bytes, addressed by LBA (Logical Block Address).
File offset = LBA * 512.
"""

from __future__ import annotations

from pathlib import Path


class DiskImage:
    """Raw sector I/O on a flat disk image file."""

    SECTOR_SIZE = 512

    def __init__(self, path: Path, writable: bool = False) -> None:
        self._path = Path(path)
        mode = "r+b" if writable and self._path.exists() else "rb"
        self._file = open(self._path, mode)
        self._writable = "+" in mode
        self._file.seek(0, 2)  # seek to end
        self._size = self._file.tell()
        self._sector_count = self._size // self.SECTOR_SIZE

    def read_sector(self, lba: int) -> bytes | None:
        """Read a single 512-byte sector. Returns None if out of range."""
        if lba < 0 or lba >= self._sector_count:
            return None
        self._file.seek(lba * self.SECTOR_SIZE)
        return self._file.read(self.SECTOR_SIZE)

    def read_sectors(self, lba: int, count: int) -> bytes | None:
        """Read multiple consecutive sectors. Returns None if any out of range."""
        if lba < 0 or lba + count > self._sector_count:
            return None
        self._file.seek(lba * self.SECTOR_SIZE)
        return self._file.read(count * self.SECTOR_SIZE)

    def write_sectors(self, lba: int, data: bytes | bytearray) -> bool:
        """Write sector data at LBA. Returns False if out of range or read-only."""
        count = len(data) // self.SECTOR_SIZE
        if not self._writable or lba < 0 or lba + count > self._sector_count:
            return False
        self._file.seek(lba * self.SECTOR_SIZE)
        self._file.write(data)
        self._file.flush()
        return True

    @property
    def sector_count(self) -> int:
        return self._sector_count

    def close(self) -> None:
        self._file.close()

    def __del__(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass
