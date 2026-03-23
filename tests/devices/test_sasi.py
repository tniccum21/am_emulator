"""Tests for the boot ROM SASI controller model."""

from alphasim.devices.sasi import SASIController


class _RecordingTarget:
    def __init__(self) -> None:
        self.read_calls: list[tuple[int, int]] = []

    def read_sectors(self, lba: int, count: int) -> bytes:
        self.read_calls.append((lba, count))
        return bytes(512 * count)


def test_read_sector_uses_full_16bit_cylinder() -> None:
    sasi = SASIController()
    sasi.target = _RecordingTarget()

    sasi.write(SASIController.BASE + 2, 1, 0x03)  # sector
    sasi.write(SASIController.BASE + 3, 1, 0x5A)  # cylinder low
    sasi.write(SASIController.BASE + 5, 1, 0x02)  # cylinder high
    sasi.write(SASIController.BASE + 6, 1, 0xE1)  # drive/head
    sasi.write(SASIController.BASE + 0, 1, 0x18)  # READ SECTOR

    assert sasi.target.read_calls == [(12045, 1)]


def test_read_sector_head_bit_selects_odd_lba() -> None:
    sasi = SASIController()
    sasi.target = _RecordingTarget()

    sasi.write(SASIController.BASE + 2, 1, 0x01)
    sasi.write(SASIController.BASE + 3, 1, 0x00)
    sasi.write(SASIController.BASE + 5, 1, 0x00)
    sasi.write(SASIController.BASE + 6, 1, 0xF1)
    sasi.write(SASIController.BASE + 0, 1, 0x18)

    assert sasi.target.read_calls == [(2, 1)]


def test_register_aliases_track_boot_address_fields() -> None:
    sasi = SASIController()

    sasi.write(SASIController.BASE + 2, 1, 0x08)
    sasi.write(SASIController.BASE + 3, 1, 0x6C)
    sasi.write(SASIController.BASE + 5, 1, 0x01)

    assert sasi._sector_number == 0x08
    assert sasi._cylinder_low == 0x6C
    assert sasi._cylinder_high == 0x01
    assert sasi._sct == 0x08
    assert sasi._sno == 0x6C
    assert sasi._cyh == 0x01
