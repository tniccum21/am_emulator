"""Tests for the native PIT-style timer block at $FFFE60-$FFFE67."""

from alphasim.devices.timer8253 import Timer8253


def test_channel1_count_latch_reads_current_value_low_then_high() -> None:
    timer = Timer8253()

    timer.write(0xFFFE62, 1, 0x34)
    timer.write(0xFFFE62, 1, 0x12)
    timer.write(0xFFFE63, 1, 0x01)
    timer.tick(0x20)

    timer.write(0xFFFE66, 1, 0x40)

    low = timer.read(0xFFFE62, 1)
    high = timer.read(0xFFFE62, 1)

    assert low == 0x14
    assert high == 0x12


def test_channel2_periodic_counter_raises_level_six_irq() -> None:
    timer = Timer8253()

    timer.write(0xFFFE66, 1, 0xB6)
    timer.write(0xFFFE64, 1, 0x14)
    timer.write(0xFFFE64, 1, 0x00)

    timer.tick(0x14)

    assert timer.get_interrupt_level() == 6
    timer.acknowledge_interrupt(6)
    assert timer.get_interrupt_level() == 0
    assert timer._counter[2] == 0x14
