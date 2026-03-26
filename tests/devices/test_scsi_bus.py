"""Tests for the low-memory SCSI alias handshake at $FFFFC8-$FFFFC9."""

from __future__ import annotations

from alphasim.devices.scsi_bus import SCSIBusInterface, SCSIPhase


class _DummyTarget:
    def read_sectors(self, lba: int, count: int) -> bytes:
        return bytes(count * 512)


def test_selection_enters_command_phase_directly() -> None:
    scsi = SCSIBusInterface()
    scsi.target = _DummyTarget()

    # The 00/01/11 selection handshake enters COMMAND phase directly.
    # Both the 68010 and 68020 monitor drivers expect REQ asserted
    # immediately after selection so they can send CDB bytes.
    scsi.write(0xFFFFC8, 1, 0x00)
    scsi.write(0xFFFFC9, 1, 0x00)
    scsi.write(0xFFFFC8, 1, 0x01)
    scsi.write(0xFFFFC8, 1, 0x11)

    assert scsi._phase == SCSIPhase.COMMAND
    assert scsi.read(0xFFFFC8, 1) == 0x16  # COMMAND phase + REQ
    assert scsi._cdb_index == 0
    assert scsi._cdb_index == 0


def test_pending_dma_irq_reports_level_two() -> None:
    scsi = SCSIBusInterface()

    assert scsi.get_interrupt_level() == 0

    scsi._irq_pending = True

    assert scsi.get_interrupt_level() == 2
