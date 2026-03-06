"""MSM5832 Real-Time Clock at $FFFE04-$FFFE05.

The AM-1200 uses an OKI MSM5832 RTC chip with a two-register interface:
  $FFFE04 (write): Command/address register
    - Bits 0-3: Register address (0-12)
    - Bit 4: READ enable
    - Bit 6: HOLD (freeze time for consistent reads)
  $FFFE05 (read):  Data register — returns BCD nibble for selected register
  $FFFE05 (write): Data register — sets BCD nibble for selected register

MSM5832 register map (each returns a BCD nibble):
  0: Seconds ones (0-9)     7: Day of month ones (0-9)
  1: Seconds tens (0-5)     8: Day of month tens (0-3)
  2: Minutes ones (0-9)     9: Month ones (0-9)
  3: Minutes tens (0-5)    10: Month tens (0-1)
  4: Hours ones (0-9)      11: Year ones (0-9)
  5: Hours tens (0-2)      12: Year tens (0-9)
  6: Day of week (0-6)     13: Reference/control
"""

from datetime import datetime
from .base import IODevice


class RTC_MSM5832(IODevice):
    """MSM5832 real-time clock chip."""

    def __init__(self):
        self._command: int = 0  # last command byte written to $FFFE04
        # Internal BCD registers (writable by OS for SET DATE)
        self._regs: list[int] = [0] * 14
        self._sync_from_host()

    def _sync_from_host(self) -> None:
        """Load current host time into internal BCD registers."""
        now = datetime.now()
        self._regs[0] = now.second % 10
        self._regs[1] = now.second // 10
        self._regs[2] = now.minute % 10
        self._regs[3] = now.minute // 10
        self._regs[4] = now.hour % 10
        self._regs[5] = now.hour // 10
        self._regs[6] = (now.weekday() + 1) % 7  # Python Mon=0, MSM5832 Sun=0
        self._regs[7] = now.day % 10
        self._regs[8] = now.day // 10
        self._regs[9] = now.month % 10
        self._regs[10] = now.month // 10
        yr = now.year % 100
        self._regs[11] = yr % 10
        self._regs[12] = yr // 10
        self._regs[13] = 0  # reference/control register

    def read(self, address: int, size: int) -> int:
        if (address & 0xFFFFFF) == 0xFFFE05:
            reg = self._command & 0x0F
            if reg < 14:
                return self._regs[reg] & 0x0F
            return 0
        # Reading command register returns 0
        return 0

    def write(self, address: int, size: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if addr == 0xFFFE04:
            self._command = value & 0xFF
            # On HOLD assertion, snapshot host time
            if value & 0x40:
                self._sync_from_host()
        elif addr == 0xFFFE05:
            # Write data to selected register (SET DATE/TIME)
            reg = self._command & 0x0F
            if reg < 14:
                self._regs[reg] = value & 0x0F
