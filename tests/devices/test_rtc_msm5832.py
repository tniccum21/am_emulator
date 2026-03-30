from __future__ import annotations

from alphasim.devices.rtc_msm5832 import RTC_MSM5832
from alphasim.devices.rtc_shared import RTCSharedState
from datetime import datetime


def _make_time_source(start: float = 0.0):
    state = {"now": start}

    def now() -> float:
        return state["now"]

    return state, now


def test_hold_freezes_visible_time_until_release() -> None:
    clock, now = _make_time_source()
    shared = RTCSharedState(
        start_time=datetime(2026, 3, 20, 12, 34, 56),
        time_source=now,
    )
    rtc = RTC_MSM5832(shared)

    rtc.write(0xFFFE04, 1, 0x10)  # READ + reg 0
    assert rtc.read(0xFFFE05, 1) == 6

    rtc.write(0xFFFE04, 1, 0x10 | 0x40)  # HOLD + READ + reg 0
    clock["now"] += 2.0
    rtc.tick(0)
    assert rtc.read(0xFFFE05, 1) == 6

    rtc.write(0xFFFE04, 1, 0x10)  # release HOLD
    assert rtc.read(0xFFFE05, 1) == 8


def test_write_updates_shared_time_registers() -> None:
    clock, now = _make_time_source()
    shared = RTCSharedState(
        start_time=datetime(2026, 3, 20, 12, 34, 56),
        time_source=now,
    )
    rtc = RTC_MSM5832(shared)

    rtc.write(0xFFFE04, 1, 0x40 | 0x10 | 0x00)
    rtc.write(0xFFFE05, 1, 9)
    rtc.write(0xFFFE04, 1, 0x10 | 0x00)
    assert rtc.read(0xFFFE05, 1) == 9

    clock["now"] += 1.0
    rtc.tick(0)
    assert rtc.read(0xFFFE05, 1) == 0


def test_observed_hold_then_year_tens_read() -> None:
    _clock, now = _make_time_source()
    shared = RTCSharedState(
        start_time=datetime(2026, 3, 20, 12, 34, 56),
        time_source=now,
    )
    rtc = RTC_MSM5832(shared)

    rtc.write(0xFFFE04, 1, 0x4D)
    rtc.write(0xFFFE04, 1, 0x5C)

    assert rtc.read(0xFFFE05, 1) == 0x02
