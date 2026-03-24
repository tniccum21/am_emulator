"""Direct-mapped clock/date register bank at $FFFE40-$FFFE5F.

Native AMOSL.MON reaches a second clock-like bank in the $FFFE40 range after
the low-memory service loop. The observed access pattern lines up with a
directly addressable 14-register BCD layout on even offsets:

  $FFFE40, $FFFE42, ... $FFFE5A -> registers 0..13

That matches the AM-1200's existing MSM5832-style clock nibbles closely enough
to model the bank as a second view of the same time/date fields. The monitor
sequence writes $FFFE5A and then reads back bit 1; open-bus $FF keeps bit 1
set forever, so this model guarantees that bit clears.
"""

from __future__ import annotations

from datetime import datetime

from .base import IODevice


class RTCDirectBank(IODevice):
    """Inferred direct-access BCD clock/date register bank."""

    _BASE_ADDR = 0xFFFE40
    _LAST_REG_OFFSET = 0x1A

    def __init__(self) -> None:
        self._regs: list[int] = [0] * 14
        self._control: int = 0
        self._sync_from_host()

    def _sync_from_host(self) -> None:
        """Snapshot host time into the direct BCD register bank."""
        now = datetime.now()
        self._regs[0] = now.second % 10
        self._regs[1] = now.second // 10
        self._regs[2] = now.minute % 10
        self._regs[3] = now.minute // 10
        self._regs[4] = now.hour % 10
        self._regs[5] = now.hour // 10
        self._regs[6] = (now.weekday() + 1) % 7  # Python Mon=0, device Sun=0
        self._regs[7] = now.day % 10
        self._regs[8] = now.day // 10
        self._regs[9] = now.month % 10
        self._regs[10] = now.month // 10
        yr = now.year % 100
        self._regs[11] = yr % 10
        self._regs[12] = yr // 10
        self._regs[13] = self._control & 0x0F

    def _decode_reg(self, address: int) -> int | None:
        addr = address & 0xFFFFFF
        offset = addr - self._BASE_ADDR
        if offset < 0 or offset > self._LAST_REG_OFFSET or offset & 1:
            return None
        return offset // 2

    def read(self, address: int, size: int) -> int:
        reg = self._decode_reg(address)
        if reg is None:
            return 0
        if reg == 13:
            # Native code polls bit 1 here; keep it clear so the handshake can
            # complete instead of spinning on open-bus $FF.
            return self._control & 0xFD
        return self._regs[reg] & 0x0F

    def write(self, address: int, size: int, value: int) -> None:
        reg = self._decode_reg(address)
        if reg is None:
            return
        if reg == 13:
            self._control = value & 0xFF
            self._sync_from_host()
            return
        self._regs[reg] = value & 0x0F
