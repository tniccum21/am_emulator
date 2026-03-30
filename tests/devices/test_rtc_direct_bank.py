from __future__ import annotations

from alphasim.devices.rtc_direct_bank import RTCDirectBank
from alphasim.devices.rtc_msm5832 import RTC_MSM5832
from alphasim.devices.rtc_shared import RTCSharedState
from datetime import datetime


def _make_time_source(start: float = 0.0):
    state = {"now": start}

    def now() -> float:
        return state["now"]

    return state, now


def test_direct_bank_exposes_date_fields_on_even_offsets() -> None:
    _clock, now = _make_time_source()
    rtc = RTCDirectBank(
        RTCSharedState(
            start_time=datetime(2026, 3, 20, 12, 34, 56),
            time_source=now,
        )
    )

    # The observed native reads hit registers 6..12 at even offsets
    # $4C/$4E/$50/$52/$54/$56/$58.
    assert rtc.read(0xFFFE4C, 1) == 5  # weekday (Friday)
    assert rtc.read(0xFFFE4E, 1) == 0  # day ones
    assert rtc.read(0xFFFE50, 1) == 2  # day tens
    assert rtc.read(0xFFFE52, 1) == 3  # month ones
    assert rtc.read(0xFFFE54, 1) == 0  # month tens
    assert rtc.read(0xFFFE56, 1) == 6  # year ones
    assert rtc.read(0xFFFE58, 1) == 2  # year tens


def test_control_write_clears_busy_bit_without_stalling_clock() -> None:
    clock, now = _make_time_source()
    shared = RTCSharedState(
        start_time=datetime(2026, 3, 20, 12, 34, 56),
        time_source=now,
    )
    rtc = RTCDirectBank(shared)

    rtc.write(0xFFFE5A, 1, 0x03)

    # Control/status read keeps bit 1 clear so the native poll can complete.
    assert rtc.read(0xFFFE5A, 1) == 0x01

    clock["now"] += 1.0
    rtc.tick(0)
    assert rtc.read(0xFFFE40, 1) == 7


def test_odd_offsets_read_as_zero_and_do_not_decode_as_registers() -> None:
    rtc = RTCDirectBank()

    assert rtc.read(0xFFFE41, 1) == 0
    rtc.write(0xFFFE41, 1, 0xFF)
    assert rtc.read(0xFFFE41, 1) == 0


def test_direct_bank_and_msm_view_share_one_clock() -> None:
    clock, now = _make_time_source()
    shared = RTCSharedState(
        start_time=datetime(2026, 3, 20, 12, 34, 56),
        time_source=now,
    )
    rtc = RTC_MSM5832(shared)
    direct = RTCDirectBank(shared, tick_owner=False)

    clock["now"] += 1.0
    rtc.tick(0)

    rtc.write(0xFFFE04, 1, 0x10 | 0x00)
    assert rtc.read(0xFFFE05, 1) == 7
    assert direct.read(0xFFFE40, 1) == 7
