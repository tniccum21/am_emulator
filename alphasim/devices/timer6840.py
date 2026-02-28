"""MC6840 Programmable Timer Module for Alpha Micro AM-1200.

Three independent 16-bit timer channels at $FFFE10-$FFFE1F.
The MC6840 is an 8-bit device on the lower data bus (D0-D7),
so registers appear at ODD byte addresses.

Register map (odd addresses only):
    $FFFE11 (reg 0): Write = CR1 or CR3 (selected by CR2 bit 0)
    $FFFE13 (reg 1): Write = CR2, Read = Status Register
    $FFFE15 (reg 2): Write = Timer 1 MSB latch, Read = Timer 1 counter MSB
    $FFFE17 (reg 3): Write = Timer 1 LSB latch, Read = Timer 1 counter LSB
    $FFFE19 (reg 4): Write = Timer 2 MSB latch, Read = Timer 2 counter MSB
    $FFFE1B (reg 5): Write = Timer 2 LSB latch, Read = Timer 2 counter LSB
    $FFFE1D (reg 6): Write = Timer 3 MSB latch, Read = Timer 3 counter MSB
    $FFFE1F (reg 7): Write = Timer 3 LSB latch, Read = Timer 3 counter LSB

Note: Reading with RS2=0 (reg 0 or reg 1 addresses) always returns the
Status Register.

Control Register 1/3 bits:
    Bit 0: Output enable
    Bit 1: Interrupt enable
    Bits 2-4: Operating mode
    Bit 6: Internal/external clock (0=external, 1=internal)
    Bit 7: CR1 only — enable all timers; CR3 only — prescale/reset

Control Register 2 bits:
    Bit 0: CR10 — register select (0=CR3, 1=CR1 for reg 0 writes)
    Bits 1-6: Timer 2 control (mirrors CR1/CR3 bit layout shifted)
    Bit 7: Prescale

Status Register bits (read at reg 0 or reg 1):
    Bit 0: Timer 1 interrupt flag
    Bit 1: Timer 2 interrupt flag
    Bit 2: Timer 3 interrupt flag
    Bit 7: Any interrupt (OR of bits 0-2, masked by interrupt enables)

Flag clearing: Interrupt flags are cleared by reading the Status Register
followed by reading the counter of the flagged timer. IACK does NOT clear
flags — the ISR must perform the two-step read sequence.

Clock: 1 MHz input → 1 tick per microsecond.
CPU clock ≈ 8 MHz → ~8 CPU cycles per timer tick.
"""

from __future__ import annotations

import sys
from .base import IODevice

# CPU-to-timer clock ratio (8 MHz CPU / 1 MHz timer)
CPU_TIMER_RATIO = 8


class Timer6840(IODevice):
    """MC6840 PTM at $FFFE10-$FFFE1F (odd byte addresses)."""

    BASE = 0xFFFE10

    def __init__(self, debug: bool = False) -> None:
        self._debug = debug

        # Control registers
        self._cr1 = 0x00  # Timer 1 control
        self._cr2 = 0x01  # Timer 2 control (bit 0 = CR10 = 1 default)
        self._cr3 = 0x00  # Timer 3 control

        # Timer latches (loaded on LSB write)
        self._latch = [0x0000, 0x0000, 0x0000]  # Timers 1, 2, 3

        # Timer counters (decremented by clock)
        self._counter = [0xFFFF, 0xFFFF, 0xFFFF]

        # MSB latch staging (written on MSB write, committed on LSB write)
        self._msb_staging = [0x00, 0x00, 0x00]

        # Interrupt flags (set on underflow)
        self._irq_flag = [False, False, False]

        # Two-step flag clearing: flags that were set at last status read
        # Reading status "arms" these; reading the counter clears them.
        self._clearing_armed = [False, False, False]

        # Timer enabled state
        self._timers_enabled = False

        # Cycle accumulator for sub-tick counting
        self._cycle_accum = 0

    def _trace(self, msg: str) -> None:
        if self._debug:
            print(f"[TIMER] {msg}", file=sys.stderr)

    # ── Interrupt enable helpers ────────────────────────────────────

    def _irq_enabled(self, timer: int) -> bool:
        """Check if interrupt is enabled for a timer channel."""
        if timer == 0:
            return bool(self._cr1 & 0x02)
        elif timer == 1:
            # Timer 2 interrupt enable is CR2 bit 2
            # (CR2 bits 1-6 mirror CR1/CR3 bits 0-5 for timer 2,
            #  so CR1 bit 1 = IRQ enable maps to CR2 bit 2)
            return bool(self._cr2 & 0x04)
        else:
            return bool(self._cr3 & 0x02)

    # ── IODevice interface ──────────────────────────────────────────

    def read(self, address: int, size: int) -> int:
        reg = (address - self.BASE) >> 1  # Convert byte offset to register index

        # MC6840 is on the lower data bus (D0-D7) at odd addresses.
        # Even addresses are not active — return 0 (bus pull-down).
        if (address & 1) == 0:
            return 0x00

        if reg in (0, 1):
            # Status register — MC6840 returns status for any read with RS2=0
            status = 0
            if self._irq_flag[0]:
                status |= 0x01
            if self._irq_flag[1]:
                status |= 0x02
            if self._irq_flag[2]:
                status |= 0x04
            # Bit 7: composite IRQ (any enabled flag set)
            if ((self._irq_flag[0] and self._irq_enabled(0)) or
                (self._irq_flag[1] and self._irq_enabled(1)) or
                (self._irq_flag[2] and self._irq_enabled(2))):
                status |= 0x80
            # Arm flag clearing for currently-set flags
            for i in range(3):
                if self._irq_flag[i]:
                    self._clearing_armed[i] = True
            self._trace(f"Status read: ${status:02X} (armed={self._clearing_armed})")
            return status

        if reg in (2, 3, 4, 5, 6, 7):
            # Timer counter reads
            timer = (reg - 2) >> 1  # 0, 0, 1, 1, 2, 2
            # Complete two-step flag clearing if armed
            if self._clearing_armed[timer]:
                self._irq_flag[timer] = False
                self._clearing_armed[timer] = False
                self._trace(f"Timer {timer+1} flag cleared (two-step read)")
            if reg & 1:
                # LSB
                return self._counter[timer] & 0xFF
            else:
                # MSB
                return (self._counter[timer] >> 8) & 0xFF

        return 0x00

    def write(self, address: int, size: int, value: int) -> None:
        reg = (address - self.BASE) >> 1
        value &= 0xFF

        # Only respond to odd addresses
        if (address & 1) == 0:
            return

        if reg == 0:
            # CR1 or CR3 (selected by CR2 bit 0)
            if self._cr2 & 0x01:
                self._cr1 = value
                self._trace(f"CR1 = ${value:02X}")
                # CR1 bit 7: timer system preset.  0 = counting, 1 = held
                self._timers_enabled = not bool(value & 0x80)
            else:
                self._cr3 = value
                self._trace(f"CR3 = ${value:02X}")
                if value & 0x80:
                    # Timer reset
                    self._trace("Timer reset")
                    self._counter = [0xFFFF, 0xFFFF, 0xFFFF]
                    self._irq_flag = [False, False, False]
                    self._clearing_armed = [False, False, False]

        elif reg == 1:
            self._cr2 = value
            self._trace(f"CR2 = ${value:02X}")

        elif reg in (2, 4, 6):
            # Timer MSB latch (staging)
            timer = (reg - 2) >> 1
            self._msb_staging[timer] = value
            self._trace(f"Timer {timer+1} MSB staging = ${value:02X}")

        elif reg in (3, 5, 7):
            # Timer LSB latch — commits the full 16-bit value
            timer = (reg - 2) >> 1
            msb = self._msb_staging[timer]
            self._latch[timer] = (msb << 8) | value
            self._counter[timer] = self._latch[timer]
            self._irq_flag[timer] = False
            self._clearing_armed[timer] = False
            self._trace(f"Timer {timer+1} loaded = ${self._latch[timer]:04X}")

    def tick(self, cycles: int) -> None:
        """Advance timer counters by elapsed CPU cycles."""
        if not self._timers_enabled:
            return

        self._cycle_accum += cycles
        timer_ticks = self._cycle_accum // CPU_TIMER_RATIO
        if timer_ticks == 0:
            return
        self._cycle_accum %= CPU_TIMER_RATIO

        for i in range(3):
            # MC6840: counter value 0 counts as 65536 (full 16-bit period).
            # The counter decrements from N to 1, then flags on the transition
            # to 0.  A loaded value of 0 means a full 65536-tick period.
            count = self._counter[i] if self._counter[i] > 0 else 0x10000

            new = count - timer_ticks

            if new <= 0:
                # Underflow occurred — counter crossed zero
                self._irq_flag[i] = True
                # Reload from latch (continuous mode)
                reload = self._latch[i] if self._latch[i] > 0 else 0x10000
                # Handle multiple underflows in one tick batch
                remainder = (-new) % reload
                self._counter[i] = (reload - remainder) & 0xFFFF
                if self._irq_enabled(i):
                    self._trace(f"Timer {i+1} underflow → IRQ")
            else:
                self._counter[i] = new & 0xFFFF

    def get_interrupt_level(self) -> int:
        """MC6840 PTM generates IPL 3 interrupts."""
        for i in range(3):
            if self._irq_flag[i] and self._irq_enabled(i):
                return 3
        return 0

    def get_interrupt_vector(self) -> int:
        """Timer provides vector 66 during IACK (IPL 3 → vector 66)."""
        return 66

    def acknowledge_interrupt(self, level: int) -> None:
        """IACK cycle — MC6840 does NOT clear flags on IACK.

        The ISR must clear flags via the two-step sequence:
        read status register, then read the flagged timer's counter.
        """
        self._trace("IACK: acknowledged (flags NOT cleared)")
