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

Control Register bits common to all three timers:
    Bit 1: Clock source (0=external clock input, 1=Enable clock)
    Bit 2: Counting mode (0=16-bit, 1=dual 8-bit)
    Bits 3-5: Operating mode
    Bit 6: Interrupt enable
    Bit 7: Output enable

Control Register unique bit 0 meanings:
    CR1 bit 0: Timer system preset (0=counting, 1=held/reset)
    CR2 bit 0: Register select for reg 0 writes (0=CR3, 1=CR1)
    CR3 bit 0: Timer 3 prescale (0=off, 1=divide by 8)

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

AM-1200 interrupt routing:
    The MC6840 drives IPL level 6 via autovector (vector 30 at $078).
    Interrupt is edge-triggered: pending is set on any flag 0→1 transition
    and cleared by IACK. This allows one interrupt per timer underflow
    rather than continuous re-interruption while a flag stays set.
"""

from __future__ import annotations

import sys
from .base import IODevice

# CPU-to-timer clock ratio.
# The AM-1200 uses an 8 MHz CPU and 1 MHz timer input, giving a
# nominal ratio of 8.  However, the emulated CPU's instruction cycle
# costs are lower than real 68010 hardware (avg ~6.5 vs ~10-12 cycles),
# making the timer fire proportionally more often per instruction.
# A higher ratio prevents the timer ISR from running faster than the OS
# scheduler can replenish the timer task queue — which causes a
# permanent timer lockup if the queue drains completely.
CPU_TIMER_RATIO = 32


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

        # Edge-triggered interrupt: set on flag 0→1 transition,
        # cleared by acknowledge_interrupt (IACK).
        self._interrupt_pending = False

        # Timer enabled state
        self._timers_enabled = False

        # Cycle accumulator for sub-tick counting
        self._cycle_accum = 0

        # Timer watchdog — detects when all timer IRQs are disabled
        # (timer lockup) and performs hardware recovery by clearing
        # stale flags and re-enabling Timer 1 + Timer 2.  This simulates
        # external recovery circuitry on the AM-1200.
        self._wd_cycles = 0
        self._wd_threshold = 2_000_000  # ~2M CPU cycles of inactivity (~0.25s)
        self._wd_armed = False
        self._wd_arm_count = 0


    def _trace(self, msg: str) -> None:
        if self._debug:
            print(f"[TIMER] {msg}", file=sys.stderr)

    # ── Clock source and interrupt enable helpers ─────────────────

    def _uses_internal_clock(self, timer: int) -> bool:
        """Check if a timer is configured for the PTM Enable clock."""
        if timer == 0:
            return bool(self._cr1 & 0x02)
        elif timer == 1:
            return bool(self._cr2 & 0x02)
        else:
            return bool(self._cr3 & 0x02)

    def _irq_enabled(self, timer: int) -> bool:
        """Check if interrupt is enabled for a timer channel."""
        if timer == 0:
            return bool(self._cr1 & 0x40)
        elif timer == 1:
            return bool(self._cr2 & 0x40)
        else:
            return bool(self._cr3 & 0x40)

    def _composite_irq_asserted(self) -> bool:
        """Return whether any flagged timer is currently interrupt-enabled."""
        return any(self._irq_flag[i] and self._irq_enabled(i) for i in range(3))

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

        irq_before = self._composite_irq_asserted()

        if reg == 0:
            # CR1 or CR3 (selected by CR2 bit 0)
            if self._cr2 & 0x01:
                old_cr1 = self._cr1
                self._cr1 = value
                self._trace(f"CR1 = ${value:02X}")
                # CR1 bit 0: timer system preset. 0 = counting, 1 = held/reset
                # On 1→0 transition: simultaneously load all counters from latches
                was_preset = bool(old_cr1 & 0x01)
                now_preset = bool(value & 0x01)
                if was_preset and not now_preset:
                    for i in range(3):
                        self._counter[i] = self._latch[i]
                        self._irq_flag[i] = False
                        self._clearing_armed[i] = False
                    self._cycle_accum = 0
                    self._trace(f"Preset release: counters loaded from latches "
                                f"[${self._latch[0]:04X}, ${self._latch[1]:04X}, ${self._latch[2]:04X}]")
                self._timers_enabled = not now_preset
            else:
                self._cr3 = value
                self._trace(f"CR3 = ${value:02X}")
                # CR3 bit 0 controls the Timer 3 prescaler, not run/hold.

        elif reg == 1:
            self._cr2 = value
            self._trace(f"CR2 = ${value:02X}")

        elif reg in (2, 4, 6):
            # Timer MSB latch (staging)
            timer = (reg - 2) >> 1
            self._msb_staging[timer] = value
            self._trace(f"Timer {timer+1} MSB staging = ${value:02X}")

        elif reg in (3, 5, 7):
            # Timer LSB latch — commits the full 16-bit value.
            # Loading the counter also clears the interrupt flag for this
            # timer, preventing stale flags from causing immediate
            # re-interruption when the ISR re-enables the timer.
            timer = (reg - 2) >> 1
            msb = self._msb_staging[timer]
            self._latch[timer] = (msb << 8) | value
            self._counter[timer] = self._latch[timer]
            self._irq_flag[timer] = False
            self._clearing_armed[timer] = False
            self._trace(f"Timer {timer+1} loaded = ${self._latch[timer]:04X}")

        if not irq_before and self._composite_irq_asserted():
            self._interrupt_pending = True
            self._trace("IRQ pending: composite line asserted by control change")

    def tick(self, cycles: int) -> None:
        """Advance timer counters by elapsed CPU cycles.

        All timers are decremented regardless of clock source setting.
        The AM-1200 provides a 1 MHz external clock to the MC6840,
        matching the internal clock rate, so both sources tick at the
        same frequency (~1 tick per 8 CPU cycles).
        """
        if not self._timers_enabled:
            return

        self._cycle_accum += cycles
        timer_ticks = self._cycle_accum // CPU_TIMER_RATIO
        if timer_ticks == 0:
            return
        self._cycle_accum %= CPU_TIMER_RATIO

        for i in range(3):
            irq_before = self._composite_irq_asserted()

            # MC6840: counter value 0 counts as 65536 (full 16-bit period).
            # The counter decrements from N to 1, then flags on the transition
            # to 0.  A loaded value of 0 means a full 65536-tick period.
            count = self._counter[i] if self._counter[i] > 0 else 0x10000

            new = count - timer_ticks

            if new <= 0:
                # Underflow occurred — counter crossed zero
                self._irq_flag[i] = True
                # Edge-triggered AM-1200 routing: assert only when the enabled
                # composite IRQ line rises, not for masked timer flags.
                if not irq_before and self._composite_irq_asserted():
                    self._interrupt_pending = True
                    self._trace(f"Timer {i+1} underflow → IRQ pending")
                pass  # Flag set; edge/level logic above handles interrupt
                # Reload from latch (continuous mode)
                reload = self._latch[i] if self._latch[i] > 0 else 0x10000
                # Handle multiple underflows in one tick batch
                remainder = (-new) % reload
                self._counter[i] = (reload - remainder) & 0xFFFF
            else:
                self._counter[i] = new & 0xFFFF

        # Timer watchdog: if the composite IRQ has been deasserted
        # (all timer IRQs disabled) for too long, perform recovery.
        if self._composite_irq_asserted():
            self._wd_cycles = 0
            if not self._wd_armed:
                self._wd_arm_count += 1
                if self._wd_arm_count >= 50:
                    self._wd_armed = True
        elif self._wd_armed and self._timers_enabled:
            self._wd_cycles += cycles
            if self._wd_cycles >= self._wd_threshold:
                self._wd_cycles = 0
                # Recovery: clear flags so next underflow creates a
                # clean 0→1 transition, and enable T1 + T2 IRQs.
                for j in range(3):
                    self._irq_flag[j] = False
                    self._clearing_armed[j] = False
                self._cr1 |= 0x40  # Enable T1 IRQ
                self._cr2 = (self._cr2 & 0x3F) | 0x40  # Enable T2 IRQ, preserve other bits
                self._trace("Watchdog recovery: flags cleared, T1+T2 IRQs enabled")

    def get_interrupt_level(self) -> int:
        """MC6840 PTM generates IPL 6 interrupts in the AM-1200.

        Edge-triggered: returns level 6 only when an interrupt is newly
        pending (composite flag+enable transitioned from off to on).
        Cleared by IACK.
        """
        if self._interrupt_pending:
            return 6
        return 0

    def get_interrupt_vector(self) -> int:
        """MC6840 is a 6800-family peripheral using VPA autovector.

        Return 0 to indicate autovector mode — the CPU will use
        vector 24 + IPL level (= vector 30 for level 6).
        """
        return 0

    def acknowledge_interrupt(self, level: int) -> None:
        """IACK cycle — acknowledge interrupt.

        The interrupt flags themselves are NOT cleared by IACK.
        The ISR must clear flags via the two-step sequence:
        read status register, then read the flagged timer's counter.

        Clears the edge-triggered pending state. The interrupt flags
        themselves are NOT cleared by IACK — the ISR must clear them
        via the two-step sequence (read status, then read counter).
        """
        self._interrupt_pending = False
        self._trace("IACK: acknowledged")
