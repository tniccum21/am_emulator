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

from .base import IODevice
from .rtc_shared import RTCSharedState


class RTCDirectBank(IODevice):
    """Inferred direct-access BCD clock/date register bank."""

    _BASE_ADDR = 0xFFFE40
    _LAST_REG_OFFSET = 0x1A

    def __init__(
        self,
        shared_state: RTCSharedState | None = None,
        *,
        tick_owner: bool = True,
    ) -> None:
        self._clock = shared_state or RTCSharedState()
        self._tick_owner = tick_owner
        self._control: int = 0

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
        return self._clock.read_reg(reg) & 0x0F

    def write(self, address: int, size: int, value: int) -> None:
        reg = self._decode_reg(address)
        if reg is None:
            return
        if reg == 13:
            self._control = value & 0xFF
            return
        self._clock.write_reg(reg, value & 0x0F)

    def tick(self, cycles: int) -> None:
        if self._tick_owner:
            self._clock.tick(cycles)
