"""Configuration DIP switch at $FE03.

Read returns the configured value.  Writes are absorbed (latch pulse).
Default $02 = SCSI boot.
"""

from .base import IODevice


class ConfigDIP(IODevice):
    """Configuration DIP switch."""

    def __init__(self, value: int = 0x02):
        self.value = value & 0xFF

    def read(self, address: int, size: int) -> int:
        return self.value

    def write(self, address: int, size: int, value: int) -> None:
        pass  # absorb latch pulses
