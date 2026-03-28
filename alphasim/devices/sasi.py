"""SASI/SCSI disk controller for Alpha Micro AM-1200.

This implements the TTL-based SASI controller at $FFFFE0-$FFFFE7.
The controller presents a WD1002-compatible register interface to the
CPU, translating register commands to disk operations on the backend.

Register map (byte-accessible at $FFFFE0-$FFFFE7):
    $FFFFE0 (reg 0): Status (R) / Command (W)
    $FFFFE1 (reg 1): Error (R) / Write precomp (W)
    $FFFFE2 (reg 2): Sector number
    $FFFFE3 (reg 3): Cylinder low
    $FFFFE4 (reg 4): PIO data port
    $FFFFE5 (reg 5): Cylinder high
    $FFFFE6 (reg 6): SDH — drive/head select; bit 1 = controller ready (R)
    $FFFFE7 (reg 7): Status (R) / Command (W)

Command codes written to reg 0:
    $0C: RESTORE (recalibrate to track 0)
    $18: READ SECTOR
    $58: RECALIBRATE
    $84: PIO START (data available at reg 4)

Command codes written to reg 7:
    $80: BUS RESET
    $81: DATA TRANSFER START

The boot ROM's SCINI code (Domain D) uses this controller as follows:
1. Write SDH for target selection, write $0C to reg 0 → target responds
2. Recalibrate via $58/$0C commands to reg 0
3. Set CHS in regs 2/3/5/6, write $18 to reg 0 → read sector
4. Write $81 to reg 7 → data transfer, $84 to reg 0 → PIO start
5. Read 513 bytes from reg 4 (PIO data port)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import IODevice

if TYPE_CHECKING:
    pass


class SASIController(IODevice):
    """WD1002-compatible SASI/SCSI controller at $FFFFE0-$FFFFE7."""

    BASE = 0xFFFFE0

    def __init__(self, debug: bool = False) -> None:
        self._debug = debug

        # WD1002 register latches used by the ROM boot path.
        self._sector_number = 0x00
        self._cylinder_low = 0x00
        self._cylinder_high = 0x00
        self._sdh = 0x00

        # Legacy aliases used by older diagnostics in this repo.
        self._sct = 0x00
        self._sno = 0x00
        self._cyh = 0x00

        # Controller state
        self._ready = False       # SDH bit 1: controller ready
        self._error_bits = 0x00   # Reg 0 read: status/error flags
        self._err_reg = 0x00      # Reg 1: error register

        # PIO data buffer
        self._data_buffer: bytes = b""
        self._data_index = 0

        # Disk target (set externally)
        self.target: object | None = None

        # Interrupt pending
        self._irq_pending = False

    # ── IODevice interface ─────────────────────────────────────────

    def _trace(self, msg: str) -> None:
        if self._debug:
            import sys
            print(f"[SASI] {msg}", file=sys.stderr)

    def read(self, address: int, size: int) -> int:
        """Read from SASI register."""
        reg = address - self.BASE

        if reg == 0:
            # Status register — error/status bits
            return self._error_bits

        if reg == 1:
            # Error register
            return self._err_reg

        if reg == 4:
            # PIO data port — return next byte from sector buffer
            if self._data_index < len(self._data_buffer):
                val = self._data_buffer[self._data_index]
                self._data_index += 1
                return val
            return 0x00

        if reg == 6:
            # SDH register — stored value with ready bit 1 overlay
            val = self._sdh
            if self._ready:
                val |= 0x02
            return val

        return 0x00

    def write(self, address: int, size: int, value: int) -> None:
        """Write to SASI register."""
        reg = address - self.BASE
        value &= 0xFF

        if reg == 0:
            self._exec_dat_command(value)
        elif reg == 1:
            self._err_reg = value
        elif reg == 2:
            self._sector_number = value
            self._sct = value
        elif reg == 3:
            self._cylinder_low = value
            self._sno = value
        elif reg == 5:
            self._cylinder_high = value
            self._cyh = value
        elif reg == 6:
            self._sdh = value
            self._ready = False  # SDH write clears ready
        elif reg == 7:
            self._exec_sts_command(value)

    def tick(self, cycles: int) -> None:
        pass

    def get_interrupt_level(self) -> int:
        """SASI interrupts at IPL level 2."""
        return 2 if self._irq_pending else 0

    def get_interrupt_vector(self) -> int:
        """SASI provides vector 65 during IACK (IPL 2 → vector 65)."""
        return 65

    # ── Command handling ──────────────────────────────────────────

    def acknowledge_interrupt(self, level: int) -> None:
        """IACK — clear the pending interrupt."""
        self._irq_pending = False

    def _exec_dat_command(self, cmd: int) -> None:
        """Handle command byte written to reg 0 (DAT)."""
        self._irq_pending = False  # New command cancels stale interrupt
        if cmd == 0x18:
            # READ SECTOR — compute LBA from CHS registers, read data
            self._do_read_sector()
            self._irq_pending = True  # Signal command completion
        elif cmd == 0x0C or cmd == 0x58:
            # RESTORE / RECALIBRATE — seek complete
            self._irq_pending = True
        elif cmd == 0x84:
            # PIO START — reset data index for byte transfer via reg 4
            self._data_index = 0

        # All commands set ready (controller acknowledged)
        self._ready = True

    def _exec_sts_command(self, cmd: int) -> None:
        """Handle command byte written to reg 7 (STS)."""
        if cmd == 0x80:
            # BUS RESET
            self._reset()
            return

        # Other commands ($81 = data transfer, etc.) just set ready
        self._ready = True

    def _do_read_sector(self) -> None:
        """Execute READ SECTOR: CHS from registers → disk read.

        The ROM's sector-read engine (L03D8) encodes AMOS logical sectors
        into CHS values:
          physical = logical // 2  (via ASRW)
          head     = logical % 2   (carry from ASRW → SDH bit 4)
          track    = physical // SPT  (via DIV)
          sector   = physical % SPT + 1  (1-based)

        We reverse this to recover the logical sector, then add 1 because
        the AMOS disk image reserves LBA 0 (logical sector N → image LBA N+1).
        """
        track = ((self._cylinder_high << 8) | self._cylinder_low)
        sector = self._sector_number
        head = (self._sdh >> 4) & 1
        spt = 10
        physical = track * spt + (max(sector, 1) - 1)
        lba = physical * 2 + head + 1

        self._trace(
            f"READ SECTOR: cyl={track} sec={sector} head={head} → LBA={lba}"
        )

        if self.target is not None:
            data = self.target.read_sectors(lba, 1)
            if data is not None:
                self._data_buffer = data
                self._error_bits = 0x00
            else:
                self._data_buffer = bytes(512)
                self._error_bits = 0x01  # Error flag
        else:
            # No target — return zeros, no error (allows boot ROM to proceed)
            self._data_buffer = bytes(512)
            self._error_bits = 0x00

        self._data_index = 0

    def _reset(self) -> None:
        """Reset the controller."""
        self._sdh = 0x00
        self._ready = False
        self._error_bits = 0x00
        self._err_reg = 0x00
        self._sector_number = 0x00
        self._cylinder_low = 0x00
        self._cylinder_high = 0x00
        self._sct = 0x00
        self._sno = 0x00
        self._cyh = 0x00
        self._data_buffer = b""
        self._data_index = 0
        self._irq_pending = False
