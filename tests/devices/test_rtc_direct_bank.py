from __future__ import annotations

from datetime import datetime as real_datetime

import alphasim.devices.rtc_direct_bank as rtc_direct_mod
from alphasim.devices.rtc_direct_bank import RTCDirectBank


def _patch_now(monkeypatch, values):
    seq = iter(values)

    class FakeDateTime:
        @classmethod
        def now(cls):
            return next(seq)

    monkeypatch.setattr(rtc_direct_mod, "datetime", FakeDateTime)


def test_direct_bank_exposes_date_fields_on_even_offsets(monkeypatch) -> None:
    _patch_now(
        monkeypatch,
        [
            real_datetime(2026, 3, 20, 12, 34, 56),
        ],
    )
    rtc = RTCDirectBank()

    # The observed native reads hit registers 6..12 at even offsets
    # $4C/$4E/$50/$52/$54/$56/$58.
    assert rtc.read(0xFFFE4C, 1) == 5  # weekday (Friday)
    assert rtc.read(0xFFFE4E, 1) == 0  # day ones
    assert rtc.read(0xFFFE50, 1) == 2  # day tens
    assert rtc.read(0xFFFE52, 1) == 3  # month ones
    assert rtc.read(0xFFFE54, 1) == 0  # month tens
    assert rtc.read(0xFFFE56, 1) == 6  # year ones
    assert rtc.read(0xFFFE58, 1) == 2  # year tens


def test_control_write_snapshots_and_clears_busy_bit(monkeypatch) -> None:
    _patch_now(
        monkeypatch,
        [
            real_datetime(2026, 3, 20, 12, 34, 56),
            real_datetime(2031, 9, 8, 7, 6, 5),
        ],
    )
    rtc = RTCDirectBank()

    rtc.write(0xFFFE5A, 1, 0x03)

    # Control/status read keeps bit 1 clear so the native poll can complete.
    assert rtc.read(0xFFFE5A, 1) == 0x01
    # The write also snapshots a fresh set of date digits for the following
    # direct reads.
    assert rtc.read(0xFFFE52, 1) == 9  # month ones
    assert rtc.read(0xFFFE56, 1) == 1  # year ones
    assert rtc.read(0xFFFE58, 1) == 3  # year tens


def test_odd_offsets_read_as_zero_and_do_not_decode_as_registers() -> None:
    rtc = RTCDirectBank()

    assert rtc.read(0xFFFE41, 1) == 0
    rtc.write(0xFFFE41, 1, 0xFF)
    assert rtc.read(0xFFFE41, 1) == 0
