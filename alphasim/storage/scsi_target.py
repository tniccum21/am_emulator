"""SCSI target device for AlphaSim.

Wraps a DiskImage and provides sector read access.
Used by the SASI controller as its backend storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .disk_image import DiskImage


class SCSITarget:
    """SCSI target backed by a disk image."""

    def __init__(self, disk: DiskImage) -> None:
        self._disk = disk

    def read_sectors(self, lba: int, count: int) -> bytes | None:
        """Read sectors from disk. Returns None on error."""
        return self._disk.read_sectors(lba, count)

    @property
    def sector_count(self) -> int:
        return self._disk.sector_count
