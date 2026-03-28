"""Tests for MC6840 PTM interrupt gating."""

from alphasim.devices.timer6840 import CPU_TIMER_RATIO, Timer6840


def _write_timer1_reload(timer: Timer6840, value: int) -> None:
    timer.write(Timer6840.BASE + 0x05, 1, (value >> 8) & 0xFF)
    timer.write(Timer6840.BASE + 0x07, 1, value & 0xFF)


def _write_timer2_reload(timer: Timer6840, value: int) -> None:
    timer.write(Timer6840.BASE + 0x09, 1, (value >> 8) & 0xFF)
    timer.write(Timer6840.BASE + 0x0B, 1, value & 0xFF)


def test_masked_underflow_sets_flag_without_irq() -> None:
    timer = Timer6840()

    # Timer 1: Enable clock, IRQ disabled, counting enabled.
    timer.write(Timer6840.BASE + 0x01, 1, 0x02)
    _write_timer1_reload(timer, 0x0001)

    timer.tick(CPU_TIMER_RATIO)

    assert timer._irq_flag == [True, False, False]
    assert timer.get_interrupt_level() == 0


def test_enabling_flagged_timer_raises_interrupt_edge() -> None:
    timer = Timer6840()

    # First underflow with IRQ masked.
    timer.write(Timer6840.BASE + 0x01, 1, 0x02)
    _write_timer1_reload(timer, 0x0001)
    timer.tick(CPU_TIMER_RATIO)
    assert timer.get_interrupt_level() == 0

    # Enabling IRQ with the flag already set should assert the composite line.
    timer.write(Timer6840.BASE + 0x01, 1, 0x42)

    assert timer.get_interrupt_level() == 6


def test_timer2_irq_uses_cr2_bit6_for_native_wait_sequence() -> None:
    timer = Timer6840()

    # Native AMOS has already released the PTM from preset via CR1=$00
    # before it later arms timer 2 with reload 0 and CR2=$41.
    timer.write(Timer6840.BASE + 0x01, 1, 0x00)
    _write_timer2_reload(timer, 0x0000)
    timer.write(Timer6840.BASE + 0x03, 1, 0x41)

    timer.tick(CPU_TIMER_RATIO * 0x10000)

    assert timer._irq_flag[1] is True
    assert timer.get_interrupt_level() == 6
