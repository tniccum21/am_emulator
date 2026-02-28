"""Front panel LED display at $FE00 (write-only)."""

from .base import IODevice


class LED(IODevice):
    """7-segment LED status display.

    Boot ROM LED codes:
      6  = hardware init starting
      11 = controller init in progress
      0  = trying next drive / success
      14 = OS handoff (boot complete)
    """

    def __init__(self):
        self.value: int = 0
        self.history: list[int] = []

    def read(self, address: int, size: int) -> int:
        return 0xFF  # write-only; reads return bus float

    def write(self, address: int, size: int, value: int) -> None:
        self.value = value & 0xFF
        self.history.append(self.value)
        print(f"[LED] {self.value}")
