"""RAM device — raw byte-addressable storage.

The memory bus handles the Alpha Micro word-level byte-swap between the CPU
and this physical storage.  RAM just stores and returns bytes at addresses.
"""

from .base import IODevice


class RAM(IODevice):
    """System RAM.  Stores bytes in physical (PDP-11) layout."""

    def __init__(self, size: int):
        self.size = size
        self.data = bytearray(size)

    def read(self, address: int, size: int) -> int:
        offset = address
        if offset < 0 or offset >= self.size:
            return 0xFF
        return self.data[offset]

    def write(self, address: int, size: int, value: int) -> None:
        offset = address
        if 0 <= offset < self.size:
            self.data[offset] = value & 0xFF
