"""Minimal PIT-style timer block at $FFFE60-$FFFE67 for native AMOSL paths."""

from __future__ import annotations

from .base import IODevice


class Timer8253(IODevice):
    """Subset of the native timer/counter block used by AMOSL.MON.

    The loaded monitor uses three data ports:
      $FFFE60 counter 0 data
      $FFFE62 counter 1 data
      $FFFE64 counter 2 data

    with adjacent control/start bytes at $FFFE61 / $FFFE63 and a shared
    latch/control register at $FFFE66.

    The only behavior needed on the current native path is:
      - counter 2: periodic level-6 autovector source
      - counter 1: one-shot countdown read/write in low-byte/high-byte order
      - counter 0: same byte protocol as counter 1 for later scheduler code
      - control writes $00 / $40: latch counter 0 / 1 for two-byte reads
    """

    BASE = 0xFFFE60

    def __init__(self) -> None:
        self._reload = [0, 0, 0]
        self._counter = [0, 0, 0]
        self._enabled = [False, False, False]
        self._write_low_next = [True, True, True]
        self._latched_read = [None, None, None]
        self._read_low_next = [True, True, True]
        self._control = 0x00
        self._interrupt_pending = False

    def _channel_for_data_port(self, address: int) -> int | None:
        if address == 0xFFFE60:
            return 0
        if address == 0xFFFE62:
            return 1
        if address == 0xFFFE64:
            return 2
        return None

    def _set_counter_value(self, channel: int, value: int) -> None:
        value &= 0xFFFF
        self._reload[channel] = value
        self._counter[channel] = value if value != 0 else 0x10000

    def read(self, address: int, size: int) -> int:
        channel = self._channel_for_data_port(address & 0xFFFFFF)
        if channel is None:
            return 0x00

        value = self._latched_read[channel]
        if value is None:
            value = self._counter[channel]

        if self._read_low_next[channel]:
            self._read_low_next[channel] = False
            return value & 0xFF

        self._read_low_next[channel] = True
        self._latched_read[channel] = None
        return (value >> 8) & 0xFF

    def write(self, address: int, size: int, value: int) -> None:
        address &= 0xFFFFFF
        value &= 0xFF

        channel = self._channel_for_data_port(address)
        if channel is not None:
            if self._write_low_next[channel]:
                self._reload[channel] = (self._reload[channel] & 0xFF00) | value
                self._write_low_next[channel] = False
                return

            self._reload[channel] = (self._reload[channel] & 0x00FF) | (value << 8)
            self._set_counter_value(channel, self._reload[channel])
            self._write_low_next[channel] = True
            if channel == 2:
                self._enabled[channel] = True
            return

        if address == 0xFFFE61:
            self._enabled[0] = bool(value & 0x01)
            if not self._enabled[0]:
                self._counter[0] = self._reload[0] if self._reload[0] != 0 else 0x10000
            return

        if address == 0xFFFE63:
            self._enabled[1] = bool(value & 0x01)
            if not self._enabled[1]:
                self._counter[1] = self._reload[1] if self._reload[1] != 0 else 0x10000
            return

        if address == 0xFFFE66:
            self._control = value
            if value == 0x00:
                self._latched_read[0] = self._counter[0]
                self._read_low_next[0] = True
            elif value == 0x40:
                self._latched_read[1] = self._counter[1]
                self._read_low_next[1] = True
            return

    def tick(self, cycles: int) -> None:
        for channel in range(3):
            if not self._enabled[channel]:
                continue

            remaining = self._counter[channel]
            if remaining <= 0:
                continue

            remaining -= cycles

            while remaining <= 0:
                if channel == 2:
                    self._interrupt_pending = True
                    reload = self._reload[channel] if self._reload[channel] != 0 else 0x10000
                    remaining += reload
                else:
                    remaining = 0
                    self._enabled[channel] = False
                    break

            self._counter[channel] = remaining

    def get_interrupt_level(self) -> int:
        return 6 if self._interrupt_pending else 0

    def get_interrupt_vector(self) -> int:
        return 0

    def acknowledge_interrupt(self, level: int) -> None:
        self._interrupt_pending = False
