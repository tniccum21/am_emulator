"""Front panel LED display at $FE00 (write-only)."""

import sys
from .base import IODevice


class LED(IODevice):
    """7-segment LED status display.

    Boot ROM LED codes:
      $06 = hardware init starting
      $0B = controller init in progress
      $00 = trying next drive / success
      $0E = OS handoff (boot complete)
      $80+ = self-test diagnostic codes
    """

    def __init__(self):
        self.value: int = 0
        self.history: list[int] = []
        self.stdout_mid_line = False  # tracks if ACIA left cursor mid-line

    def read(self, address: int, size: int) -> int:
        return 0xFF  # write-only; reads return bus float

    def write(self, address: int, size: int, value: int) -> None:
        self.value = value & 0xFF
        self.history.append(self.value)
        if self.stdout_mid_line:
            sys.stderr.buffer.write(b"\r\n")
            self.stdout_mid_line = False
        sys.stderr.buffer.write(f"[LED] {self.value:02X}\r\n".encode())
        sys.stderr.buffer.flush()
