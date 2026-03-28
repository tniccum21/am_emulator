"""Placeholder device for the unresolved register at $FFFE28.

The ROM self-test writes a setup sequence to $FFFE28 before polling the
primary serial port bases at $FFFE20, $FFFE24, and $FFFE30. The exact
hardware function is still unresolved, so this device is intentionally
minimal and only records raw byte access.
"""

from __future__ import annotations

from .base import IODevice


class PrimarySerialSetup(IODevice):
    """Record raw access to the unresolved primary-serial setup register."""

    BASE = 0xFFFE28

    def __init__(self) -> None:
        self.last_value = 0x00
        self.read_count = 0
        self.write_history: list[int] = []

    def read(self, address: int, size: int) -> int:
        self.read_count += 1
        return self.last_value

    def write(self, address: int, size: int, value: int) -> None:
        self.last_value = value & 0xFF
        self.write_history.append(self.last_value)
