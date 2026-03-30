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

from .base import IODevice
from .rtc_shared import RTCSharedState


class RTC_MSM5832(IODevice):
    """MSM5832 real-time clock chip."""

    def __init__(
        self,
        shared_state: RTCSharedState | None = None,
        *,
        tick_owner: bool = True,
    ):
        self._command: int = 0  # last command byte written to $FFFE04
        self._hold_active: bool = False
        self._clock = shared_state or RTCSharedState()
        self._tick_owner = tick_owner
        self._latched_regs: list[int] = self._clock.copy_regs()

    def read(self, address: int, size: int) -> int:
        if (address & 0xFFFFFF) == 0xFFFE05:
            reg = self._command & 0x0F
            if reg < 14:
                regs = self._latched_regs if self._hold_active else self._clock.copy_regs()
                return regs[reg] & 0x0F
            return 0
        # Reading command register returns 0
        return 0

    def write(self, address: int, size: int, value: int) -> None:
        addr = address & 0xFFFFFF
        if addr == 0xFFFE04:
            new_command = value & 0xFF
            new_hold = bool(new_command & 0x40)
            if new_hold and not self._hold_active:
                self._latched_regs = self._clock.copy_regs()
            self._hold_active = new_hold
            self._command = new_command
        elif addr == 0xFFFE05:
            # Write data to selected register (SET DATE/TIME)
            reg = self._command & 0x0F
            if reg < 14:
                self._clock.write_reg(reg, value & 0x0F)
                if self._hold_active:
                    self._latched_regs[reg] = value & 0x0F

    def tick(self, cycles: int) -> None:
        if self._tick_owner:
            self._clock.tick(cycles)
