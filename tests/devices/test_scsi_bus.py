"""Tests for the low-memory SCSI alias handshake at $FFFFC8-$FFFFC9."""

from __future__ import annotations

from alphasim.devices.scsi_bus import SCSIBusInterface, SCSIPhase


class _DummyTarget:
    def read_sectors(self, lba: int, count: int) -> bytes:
        return bytes(count * 512)


def test_selection_requires_second_handshake_before_command_phase() -> None:
    scsi = SCSIBusInterface()
    scsi.target = _DummyTarget()

    # First 00/00/01/11 handshake leaves the interface in the observed
    # monitor-side pre-command response state ($14), not yet in COMMAND.
    scsi.write(0xFFFFC8, 1, 0x00)
    scsi.write(0xFFFFC9, 1, 0x00)
    scsi.write(0xFFFFC8, 1, 0x01)
    scsi.write(0xFFFFC8, 1, 0x11)

    assert scsi.read(0xFFFFC8, 1) == 0x14
    assert scsi._selection_response_pending is True
    assert scsi._phase == SCSIPhase.BUS_FREE
    assert scsi._cdb_index == 0

    # The clear-data write in the pending stage must not be consumed as CDB[0].
    scsi.write(0xFFFFC8, 1, 0x00)
    scsi.write(0xFFFFC9, 1, 0x00)
    assert scsi._cdb_index == 0

    # The second 01/11 handshake enters the real COMMAND phase.
    scsi.write(0xFFFFC8, 1, 0x01)
    scsi.write(0xFFFFC8, 1, 0x11)

    assert scsi._selection_response_pending is False
    assert scsi._phase == SCSIPhase.COMMAND
    assert scsi.read(0xFFFFC8, 1) == 0x16
    assert scsi._cdb_index == 0


def test_pending_dma_irq_reports_level_two() -> None:
    scsi = SCSIBusInterface()

    assert scsi.get_interrupt_level() == 0

    scsi._irq_pending = True

    assert scsi.get_interrupt_level() == 2
