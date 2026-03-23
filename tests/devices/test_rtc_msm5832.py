from __future__ import annotations

from datetime import datetime as real_datetime

import alphasim.devices.rtc_msm5832 as rtc_mod
from alphasim.devices.rtc_msm5832 import RTC_MSM5832


def _patch_now(monkeypatch, values):
    seq = iter(values)

    class FakeDateTime:
        @classmethod
        def now(cls):
            return next(seq)

    monkeypatch.setattr(rtc_mod, "datetime", FakeDateTime)


def test_hold_snapshots_only_on_rising_edge(monkeypatch) -> None:
    _patch_now(
        monkeypatch,
        [
            real_datetime(2026, 3, 20, 12, 34, 56),
            real_datetime(2031, 9, 8, 7, 6, 5),
            real_datetime(2044, 1, 2, 3, 4, 5),
        ],
    )
    rtc = RTC_MSM5832()

    # First HOLD assertion snapshots the second fake time.
    rtc.write(0xFFFE04, 1, 0x4D)
    assert rtc._hold_active is True
    assert rtc._regs[12] == 3  # year tens from 2031

    # Rewriting another HOLD command must not resnapshot while HOLD stays high.
    rtc.write(0xFFFE04, 1, 0x5C)
    assert rtc._regs[12] == 3

    # Dropping HOLD and asserting it again should take a fresh snapshot.
    rtc.write(0xFFFE04, 1, 0x00)
    assert rtc._hold_active is False
    rtc.write(0xFFFE04, 1, 0x4D)
    assert rtc._regs[12] == 4  # year tens from 2044


def test_observed_hold_then_year_tens_read(monkeypatch) -> None:
    _patch_now(
        monkeypatch,
        [
            real_datetime(2026, 3, 20, 12, 34, 56),
            real_datetime(2026, 3, 20, 12, 34, 56),
        ],
    )
    rtc = RTC_MSM5832()

    # Observed native service sequence writes reg 13 under HOLD/READ, then
    # reg 12 under HOLD/READ and reads the data nibble.
    rtc.write(0xFFFE04, 1, 0x4D)
    rtc.write(0xFFFE04, 1, 0x5C)

    assert rtc.read(0xFFFE05, 1) == 0x02
