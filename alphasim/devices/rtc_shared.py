"""Shared emulated clock state for AM-1200 RTC views."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable


class RTCSharedState:
    """Drive both RTC register views from one emulated clock."""

    DEFAULT_CYCLES_PER_SECOND = 8_000_000

    def __init__(
        self,
        start_time: datetime | None = None,
        *,
        cycles_per_second: int = DEFAULT_CYCLES_PER_SECOND,
        time_source: Callable[[], float] | None = time.monotonic,
    ) -> None:
        self._cycles_per_second = max(1, cycles_per_second)
        self._cycle_accum = 0
        self._control_nibble = 0
        self._time_source = time_source
        self._last_tick_time = time_source() if time_source is not None else None
        self._regs = self._encode_datetime(
            (start_time or datetime.now()).replace(microsecond=0)
        )

    @staticmethod
    def _encode_datetime(now: datetime) -> list[int]:
        regs = [0] * 14
        regs[0] = now.second % 10
        regs[1] = now.second // 10
        regs[2] = now.minute % 10
        regs[3] = now.minute // 10
        regs[4] = now.hour % 10
        regs[5] = now.hour // 10
        regs[6] = (now.weekday() + 1) % 7
        regs[7] = now.day % 10
        regs[8] = now.day // 10
        regs[9] = now.month % 10
        regs[10] = now.month // 10
        yr = now.year % 100
        regs[11] = yr % 10
        regs[12] = yr // 10
        return regs

    @staticmethod
    def _decode_datetime(regs: list[int]) -> datetime | None:
        second = regs[1] * 10 + regs[0]
        minute = regs[3] * 10 + regs[2]
        hour = regs[5] * 10 + regs[4]
        day = regs[8] * 10 + regs[7]
        month = regs[10] * 10 + regs[9]
        year = 2000 + regs[12] * 10 + regs[11]
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None

    def copy_regs(self) -> list[int]:
        self._advance_from_time_source()
        regs = self._regs.copy()
        regs[13] = self._control_nibble & 0x0F
        return regs

    def read_reg(self, reg: int) -> int:
        if 0 <= reg < 14:
            return self.copy_regs()[reg] & 0x0F
        return 0

    def write_reg(self, reg: int, value: int) -> None:
        self._advance_from_time_source()
        nibble = value & 0x0F
        if not 0 <= reg < 14:
            return
        if reg == 13:
            self._control_nibble = nibble
            if self._time_source is not None:
                self._last_tick_time = self._time_source()
            return
        self._regs[reg] = nibble
        if self._time_source is not None:
            self._last_tick_time = self._time_source()

    def _advance_seconds(self, whole_seconds: int) -> None:
        if whole_seconds <= 0:
            return

        current = self._decode_datetime(self._regs)
        if current is None:
            return

        self._regs = self._encode_datetime(
            current + timedelta(seconds=whole_seconds)
        )

    def _advance_from_time_source(self) -> None:
        if self._time_source is None or self._last_tick_time is None:
            return
        now = self._time_source()
        whole_seconds = int(now - self._last_tick_time)
        if whole_seconds <= 0:
            return
        self._last_tick_time += whole_seconds
        self._advance_seconds(whole_seconds)

    def tick(self, cycles: int) -> None:
        if self._time_source is not None:
            self._advance_from_time_source()
            return

        self._cycle_accum += max(0, cycles)
        whole_seconds, self._cycle_accum = divmod(
            self._cycle_accum,
            self._cycles_per_second,
        )
        self._advance_seconds(whole_seconds)
