"""SCSI bus interface for Alpha Micro AM-1200 at $FFFFC8-$FFFFC9.

This implements the raw SCSI bus interface used by the OS disk driver
(SCZ.DVR). It is a SEPARATE device from the WD1002-compatible SASI
controller at $FFFFE0-$FFFFE7, which is used only by the boot ROM.

The SCSI controller is at $FFFFC8-$FFFFC9, a distinct address range from
the MC6840 timer at $FFFE10-$FFFE1F.  The OS driver loads A5=$FFFFC8
from the DDT (Device Descriptor Table) at init time.

Register map (byte-accessible):
    $FFFFC8 (A5+0): Control/Status register
        Read:  Bus phase in bits 0-4, handshake signals
        Write: Bus line assertion (selection, attention, etc.)
    $FFFFC9 (A5+1): Data register
        Read:  Data byte from target (DATA IN phase)
        Write: Data byte to target (DATA OUT / COMMAND phase)

SCSI bus phase encoding (bits 0-4 of status register):
    Phase  0 ($00): BUS FREE — no target selected
    Phase  6 ($06): DATA OUT — initiator sends data to target
    Phase 14 ($0E): DATA IN — target sends data to initiator
    Phase 22 ($16): COMMAND — initiator sends CDB to target
    Phase 30 ($1E): MESSAGE IN — target sends message to initiator

Status register bit meanings (as read from $FFFE11):
    Bit 0: REQ — target requesting data transfer (data ready)
    Bit 1: BSY — bus is busy (selection in progress or target active)
    Bit 2: I/O direction (1 = target-to-initiator)
    Bit 3: C/D (1 = command/status, 0 = data)
    Bit 4: MSG (1 = message phase)

SCSI bus phase = MSG:C/D:I/O encoding:
    DATA OUT  = 0:0:0 + BSY|REQ = $00 + handshake bits
    DATA IN   = 0:0:1 + BSY|REQ = $04 + handshake bits
    COMMAND   = 0:1:0 + BSY|REQ = $08 + handshake bits
    STATUS    = 0:1:1 + BSY|REQ = $0C + handshake bits
    MSG OUT   = 1:0:0 + BSY|REQ = $10 + handshake bits
    MSG IN    = 1:0:1 + BSY|REQ = $14 + handshake bits

However, the AM-1200 SCZ.DVR uses a different encoding where the phase
value occupies bits 1-4 with REQ in bit 0 and BSY elsewhere. Based on
the documented driver expectations:
    DATA OUT  = $06 (phase value in status register)
    DATA IN   = $0E
    COMMAND   = $16
    MESSAGE   = $1E

Selection protocol observed from ROM/OS code:
    1. Read $FFFFC8 (check bus free)
    2. Write $00 to $FFFFC8 (negate all bus lines)
    3. Write $00 to $FFFFC9 (clear data)
    4. Write $01 to $FFFFC8 (assert BSY for selection)
    5. Write $11 to $FFFFC8 (assert BSY + ATN? for selection)
    6. Poll $FFFFC8 waiting for target response (bit 1 clear)
"""

from __future__ import annotations

import sys
from enum import IntEnum
from typing import TYPE_CHECKING

from .base import IODevice

if TYPE_CHECKING:
    pass


class SCSIPhase(IntEnum):
    """SCSI bus phases as seen in the status register."""
    BUS_FREE = 0x00
    DATA_OUT = 0x06
    DATA_IN = 0x0E
    COMMAND = 0x16
    STATUS = 0x1A   # Approximate — status phase
    MESSAGE_IN = 0x1E


class SCSIBusInterface(IODevice):
    """Raw SCSI bus interface at $FFFFC8-$FFFFC9."""

    BASE = 0xFFFFC8

    def __init__(self, debug: bool = False) -> None:
        self._debug = debug
        self.trace_callback = None

        # Bus state
        self._phase = SCSIPhase.BUS_FREE
        self._bsy = False       # Bus busy
        self._req = False       # Target requesting transfer
        self._selecting = False  # Selection in progress
        self._selection_response_pending = False

        # Command Descriptor Block buffer
        self._cdb: bytearray = bytearray(12)
        self._cdb_index = 0
        self._cdb_expected = 0  # Expected CDB length

        # Data transfer buffer
        self._data_buffer: bytes | bytearray = b""
        self._data_index = 0

        # Status/message bytes
        self._status_byte = 0x00  # SCSI status (0 = GOOD)
        self._message_byte = 0x00  # Message In (0 = COMMAND COMPLETE)

        # State machine position within status/message phase
        self._status_sent = False
        self._message_sent = False

        # Activity counter for debugging
        self._read_count = 0

        # Disk target (set externally — provides read_sectors/write_sectors)
        self.target: object | None = None

        # DMA support — bus and cpu references set externally after construction
        self._dma_bus: object | None = None  # MemoryBus for DMA writes
        self._dma_cpu: object | None = None  # MC68010 for reading DMA address regs

        # Interrupt support
        self._irq_pending = False
        self._irq_delay = 0  # Countdown cycles before IRQ fires

        # Selection state tracking
        self._sel_step = 0  # Tracks write sequence during selection

    def _trace(self, msg: str) -> None:
        if self._debug:
            print(f"[SCSI-BUS] {msg}", file=sys.stderr)

    def _emit_trace(self, msg: str) -> None:
        if self.trace_callback is not None:
            self.trace_callback(msg)

    # ── IODevice interface ─────────────────────────────────────────

    def read(self, address: int, size: int) -> int:
        reg = address - self.BASE

        if reg == 0:
            # Status/control register
            value = self._build_status()
            self._emit_trace(
                f"SCSI R CTRL -> ${value:02X} phase={self._phase.name} "
                f"req={int(self._req)} sel={self._sel_step}"
            )
            return value

        if reg == 1:
            # Data register — depends on current phase
            value = self._read_data()
            self._emit_trace(
                f"SCSI R DATA -> ${value:02X} phase={self._phase.name} "
                f"idx={self._data_index}"
            )
            return value

        return 0x00

    def write(self, address: int, size: int, value: int) -> None:
        reg = address - self.BASE
        value &= 0xFF

        if reg == 0:
            # Control register — bus line assertion
            self._emit_trace(
                f"SCSI W CTRL <- ${value:02X} phase={self._phase.name} "
                f"sel={self._sel_step}"
            )
            self._write_control(value)
        elif reg == 1:
            # Data register — depends on current phase
            self._emit_trace(
                f"SCSI W DATA <- ${value:02X} phase={self._phase.name} "
                f"idx={self._cdb_index if self._phase == SCSIPhase.COMMAND else self._data_index}"
            )
            self._write_data(value)

    def tick(self, cycles: int) -> None:
            if self._irq_delay > 0:
                self._irq_delay -= cycles
                if self._irq_delay <= 0:
                    self._irq_delay = 0
                    self._irq_pending = True
                    self._trace("DMA IRQ delay elapsed → IRQ pending")
                    self._emit_trace("SCSI IRQ pending")

    def get_interrupt_level(self) -> int:
        return 5 if self._irq_pending else 0

    def get_interrupt_vector(self) -> int:
        # AM-1200 SCSI uses autovectored level-5 interrupt.
        # Return 0 so the CPU uses the autovector (vector 29, address $074).
        # The driver installs its ISR at $074 during the I/O handler setup.
        return 0

    def acknowledge_interrupt(self, level: int) -> None:
        self._emit_trace(
            f"SCSI IRQ ack level={level} vector={self.get_interrupt_vector() or (24 + level)}"
        )
        self._irq_pending = False

    # ── Status register ────────────────────────────────────────────

    def _build_status(self) -> int:
        """Build status register value from current bus state.

        AM-1200 SCSI status register encoding:
            Bit 0: unused (always 0 in phase values)
            Bit 1: BSY (bus active)
            Bit 2: BSY (bus active, secondary — always set when active)
            Bit 3: I/O direction (1 = target-to-initiator)
            Bit 4: C/D (1 = command/status)

        Phase values (driver compares masked status against these exactly):
            $06 = DATA OUT  (C/D=0, I/O=0, BSY=1)
            $0E = DATA IN   (C/D=0, I/O=1, BSY=1)
            $16 = COMMAND   (C/D=1, I/O=0, BSY=1)
            $1E = STATUS / MESSAGE IN (C/D=1, I/O=1, BSY=1)

        STATUS and MESSAGE IN share the same hardware encoding ($1E)
        because the AM-1200 has no MSG bit in the status register.
        The driver reads them as sequential bytes from the data register.

        Bit 0 is a data-ready signal, separate from the phase encoding.
        It is set when the target has a byte ready (STATUS, MESSAGE_IN,
        DATA_IN phases) but NOT during COMMAND phase, where the driver
        compares the masked status against exact phase values ($16, $1E).
        """
        if self._phase == SCSIPhase.BUS_FREE:
            if self._selection_response_pending:
                return 0x14
            return 0x00

        # Map internal phase to hardware encoding
        if self._phase == SCSIPhase.STATUS:
            # STATUS shares encoding with MESSAGE_IN on AM-1200 hardware
            phase_val = int(SCSIPhase.MESSAGE_IN)
        else:
            phase_val = int(self._phase)

        # Bit 0 = data-ready signal (NOT part of phase encoding).
        # Set during target-to-initiator phases when data is available.
        # Must be CLEAR during COMMAND phase so driver's phase detection
        # (CMP.B #$16) matches exactly.
        if self._req and self._phase != SCSIPhase.COMMAND:
            phase_val |= 0x01

        return phase_val

    # ── Control register writes ────────────────────────────────────

    def _write_control(self, value: int) -> None:
        """Handle control register writes for bus selection/reset."""
        self._trace(f"CTRL write ${value:02X} (phase={self._phase.name}, sel_step={self._sel_step})")

        if self._selection_response_pending:
            if value == 0x00:
                self._sel_step = 1
                self._trace("Selection response acknowledged")
                return

            if self._sel_step == 1 and value == 0x01:
                self._sel_step = 2
                self._trace("Selection retry: BSY asserted")
                return

            if self._sel_step == 2 and (value == 0x11 or value == 0x01):
                self._selection_response_pending = False
                self._enter_command_phase()
                return

        if value == 0x00:
            # Negate all bus lines — start of selection or bus release
            if self._phase == SCSIPhase.BUS_FREE:
                self._sel_step = 1
            elif self._message_sent:
                # After message phase complete, release bus
                self._phase = SCSIPhase.BUS_FREE
                self._bsy = False
                self._req = False
                self._selecting = False
                self._sel_step = 0
                self._status_sent = False
                self._message_sent = False
                self._trace("Bus released → BUS FREE")
            return

        if self._sel_step == 1 and value == 0x01:
            # Assert BSY for selection (step 2)
            self._sel_step = 2
            self._bsy = True
            self._selecting = True
            self._trace("Selection: BSY asserted")
            return

        if self._sel_step == 2 and (value == 0x11 or value == 0x01):
            # Assert BSY + ATN (or just BSY again) — complete selection
            self._sel_step = 3
            # Target responds: enter COMMAND phase
            self._complete_selection()
            return

        # $80 control write during DATA_IN/DATA_OUT = DMA start
        if value == 0x80 and self._phase in (SCSIPhase.DATA_IN, SCSIPhase.DATA_OUT):
            self._start_dma()
            return

        # Any other write during active bus — could be ACK or other signal
        # After reading a data byte, the initiator may pulse ACK
        if self._phase in (SCSIPhase.DATA_IN, SCSIPhase.STATUS, SCSIPhase.MESSAGE_IN):
            # Treat any control write as ACK — advance to next byte/phase
            pass

    def _complete_selection(self) -> None:
        """Target responds to selection.

        The low-memory monitor path expects an intermediate status value
        ($14) after the first 00/00/01/11 handshake, then repeats the same
        handshake before the device enters real COMMAND phase and accepts
        CDB bytes.
        """
        if self.target is None:
            # No target — stay bus free (selection timeout)
            self._phase = SCSIPhase.BUS_FREE
            self._bsy = False
            self._selecting = False
            self._sel_step = 0
            self._trace("Selection FAILED — no target")
            self._emit_trace("SCSI selection failed")
            return

        self._selection_response_pending = True
        self._phase = SCSIPhase.BUS_FREE
        self._bsy = False
        self._req = False
        self._selecting = False
        self._sel_step = 0
        self._trace("Selection acknowledged -> pending command handshake")
        self._emit_trace("SCSI selection acknowledged -> pending command")

    def _enter_command_phase(self) -> None:
        """Enter COMMAND phase after the monitor's second selection handshake."""
        self._phase = SCSIPhase.COMMAND
        self._bsy = True
        self._req = True  # Target asserts REQ — ready for CDB bytes
        self._selecting = False
        self._cdb = bytearray(12)
        self._cdb_index = 0
        self._cdb_expected = 0
        self._trace("Selection OK → COMMAND phase")
        self._emit_trace("SCSI selection complete -> COMMAND")

    # ── Data register reads ────────────────────────────────────────

    def _read_data(self) -> int:
        """Read data byte from target (DATA IN, STATUS, or MESSAGE phase)."""
        if self._phase == SCSIPhase.DATA_IN:
            if self._data_index < len(self._data_buffer):
                byte = self._data_buffer[self._data_index]
                self._data_index += 1
                if self._data_index >= len(self._data_buffer):
                    # All data transferred — move to STATUS phase
                    self._phase = SCSIPhase.STATUS
                    self._req = True
                    self._status_sent = False
                    self._trace(f"DATA IN complete ({len(self._data_buffer)} bytes) → STATUS")
                return byte
            return 0x00

        if self._phase == SCSIPhase.STATUS:
            if not self._status_sent:
                self._status_sent = True
                # After status byte read, move to MESSAGE IN
                self._phase = SCSIPhase.MESSAGE_IN
                self._req = True
                self._message_sent = False
                self._trace(f"STATUS byte ${self._status_byte:02X} → MESSAGE IN")
                return self._status_byte
            return 0x00

        if self._phase == SCSIPhase.MESSAGE_IN:
            if not self._message_sent:
                self._message_sent = True
                # After message byte read, bus goes free
                # (initiator must write $00 to control to release)
                self._req = False
                self._trace(f"MESSAGE byte ${self._message_byte:02X} → done")
                # Automatically go bus free after message
                self._phase = SCSIPhase.BUS_FREE
                self._bsy = False
                self._sel_step = 0
                self._status_sent = False
                self._message_sent = False
                return self._message_byte
            return 0x00

        return 0x00

    # ── Data register writes ───────────────────────────────────────

    def _write_data(self, value: int) -> None:
        """Write data byte to target (COMMAND or DATA OUT phase)."""
        if self._phase == SCSIPhase.COMMAND:
            self._cdb[self._cdb_index] = value
            self._cdb_index += 1

            # Determine CDB length from group code (first byte)
            if self._cdb_index == 1:
                group = (value >> 5) & 0x07
                if group == 0:
                    self._cdb_expected = 6   # Group 0: 6-byte CDB
                elif group <= 2:
                    self._cdb_expected = 10  # Group 1-2: 10-byte CDB
                else:
                    self._cdb_expected = 12  # Group 3+: 12-byte CDB
                self._trace(f"CDB[0]=${value:02X} → {self._cdb_expected}-byte CDB")

            if self._cdb_index >= self._cdb_expected:
                # Complete CDB received — execute command
                self._execute_command()
            return

        if self._phase == SCSIPhase.DATA_OUT:
            if self._data_index < len(self._data_buffer):
                # Writing to mutable buffer
                if isinstance(self._data_buffer, bytearray):
                    self._data_buffer[self._data_index] = value
                self._data_index += 1
                if self._data_index >= len(self._data_buffer):
                    # All data written — execute the pending write
                    self._complete_write()
            return

        # During selection, data writes set target ID
        if self._selection_response_pending:
            self._trace(f"Selection-response data ignore: ${value:02X}")
            return

        if self._sel_step >= 1:
            self._trace(f"Selection data: ${value:02X}")

    # ── Command execution ──────────────────────────────────────────

    def _execute_command(self) -> None:
        """Execute the received SCSI command."""
        opcode = self._cdb[0]
        self._trace(f"EXECUTE: CDB={' '.join(f'${b:02X}' for b in self._cdb[:self._cdb_expected])}")
        self._emit_trace(
            "SCSI CDB " + " ".join(f"{b:02X}" for b in self._cdb[:self._cdb_expected])
        )

        if opcode == 0x00:
            # TEST UNIT READY
            self._status_byte = 0x00  # GOOD
            self._message_byte = 0x00
            self._phase = SCSIPhase.STATUS
            self._req = True
            self._status_sent = False
            self._trace("TEST UNIT READY → GOOD")

        elif opcode == 0x03:
            # REQUEST SENSE — return 18 bytes of sense data
            sense = bytearray(18)
            sense[0] = 0x70  # Current errors
            sense[7] = 10    # Additional sense length
            self._data_buffer = bytes(sense)
            self._data_index = 0
            self._phase = SCSIPhase.DATA_IN
            self._req = True
            self._trace("REQUEST SENSE → 18 bytes DATA IN")

        elif opcode == 0x08:
            # READ(6) — Group 0 read command
            lba = (((self._cdb[1] & 0x1F) << 16) |
                   (self._cdb[2] << 8) | self._cdb[3])
            count = self._cdb[4]
            if count == 0:
                count = 256  # Per SCSI spec, 0 means 256 sectors
            self._do_read(lba, count)

        elif opcode == 0x0A:
            # WRITE(6) — Group 0 write command
            lba = (((self._cdb[1] & 0x1F) << 16) |
                   (self._cdb[2] << 8) | self._cdb[3])
            count = self._cdb[4]
            if count == 0:
                count = 256
            self._data_buffer = bytearray(count * 512)
            self._data_index = 0
            self._pending_write_lba = lba
            self._pending_write_count = count
            self._phase = SCSIPhase.DATA_OUT
            self._req = True
            self._trace(f"WRITE(6): LBA={lba} count={count} → DATA OUT")

        elif opcode == 0x28:
            # READ(10)
            lba = ((self._cdb[2] << 24) | (self._cdb[3] << 16) |
                   (self._cdb[4] << 8) | self._cdb[5])
            count = (self._cdb[7] << 8) | self._cdb[8]
            if count == 0:
                count = 1
            self._do_read(lba, count)

        elif opcode == 0x2A:
            # WRITE(10)
            lba = ((self._cdb[2] << 24) | (self._cdb[3] << 16) |
                   (self._cdb[4] << 8) | self._cdb[5])
            count = (self._cdb[7] << 8) | self._cdb[8]
            if count == 0:
                count = 1
            # Prepare buffer for DATA OUT phase
            self._data_buffer = bytearray(count * 512)
            self._data_index = 0
            self._pending_write_lba = lba
            self._pending_write_count = count
            self._phase = SCSIPhase.DATA_OUT
            self._req = True
            self._trace(f"WRITE(10): LBA={lba} count={count} → DATA OUT")

        elif opcode == 0x1B:
            # START/STOP UNIT (used as FORMAT by SCZ.DVR doc, but benign)
            self._status_byte = 0x00
            self._message_byte = 0x00
            self._phase = SCSIPhase.STATUS
            self._req = True
            self._status_sent = False
            self._trace("START/STOP UNIT → GOOD")

        else:
            # Unknown command — return CHECK CONDITION
            self._trace(f"UNKNOWN opcode ${opcode:02X} → CHECK CONDITION")
            self._status_byte = 0x02  # CHECK CONDITION
            self._message_byte = 0x00
            self._phase = SCSIPhase.STATUS
            self._req = True
            self._status_sent = False

    def _do_read(self, lba: int, count: int) -> None:
        """Execute READ: read sectors from disk backend."""
        self._read_count += 1
        if self._read_count <= 20:
            sys.stderr.write(
                f"[SCSI] READ LBA={lba} cnt={count} "
                f"(#{self._read_count})\n"
            )
        self._trace(f"READ: LBA={lba} count={count}")
        self._emit_trace(f"SCSI READ lba={lba} count={count}")

        if self.target is not None:
            data = self.target.read_sectors(lba, count)
            if data is not None:
                self._data_buffer = data
                self._status_byte = 0x00  # GOOD
            else:
                self._data_buffer = bytes(count * 512)
                self._status_byte = 0x02  # CHECK CONDITION
        else:
            self._data_buffer = bytes(count * 512)
            self._status_byte = 0x00

        self._data_index = 0
        self._message_byte = 0x00
        self._phase = SCSIPhase.DATA_IN
        self._req = True
        self._trace(f"READ(10): {len(self._data_buffer)} bytes → DATA IN")

    def _complete_write(self) -> None:
        """Complete a WRITE command after all data received."""
        lba = getattr(self, '_pending_write_lba', 0)
        count = getattr(self, '_pending_write_count', 1)
        self._trace(f"WRITE(10) complete: LBA={lba} count={count}")

        if self.target is not None and hasattr(self.target, 'write_sectors'):
            self.target.write_sectors(lba, self._data_buffer)
            self._status_byte = 0x00
        else:
            self._status_byte = 0x02
        self._emit_trace(f"SCSI WRITE complete lba={lba} count={count}")

        self._message_byte = 0x00
        self._phase = SCSIPhase.STATUS
        self._req = True
        self._status_sent = False

    # ── DMA transfer ──────────────────────────────────────────────

    def _start_dma(self) -> None:
        """Handle $80 control write — hardware DMA transfer.

        The AM-1200 SCSI controller performs DMA transfers between the SCSI
        bus and system memory.  When the driver writes $80 to the control
        register during DATA_IN or DATA_OUT phase, the hardware transfers
        the entire data buffer and then generates a level-2 interrupt.

        The DMA target address is taken from CPU register A2 (buffer pointer)
        which the driver loads from the device descriptor before triggering DMA.
        """
        if self._dma_bus is None or self._dma_cpu is None:
            self._trace("DMA requested but no bus/cpu reference — skipping")
            self._irq_pending = True
            return

        cpu = self._dma_cpu
        bus = self._dma_bus

        if self._phase == SCSIPhase.DATA_IN:
            # Transfer data from SCSI buffer to system RAM.
            # DMA writes raw physical bytes to memory.
            # Uses dma_write_byte() for physical (non-swapped) access,
            # matching the PDP-11 byte ordering in physical memory.
            dma_addr = cpu.a[2] & 0xFFFFFF  # 24-bit address from A2
            nbytes = len(self._data_buffer) - self._data_index
            self._trace(f"DMA IN: {nbytes} bytes → ${dma_addr:06X}")
            self._emit_trace(f"SCSI DMA IN bytes={nbytes} addr=${dma_addr:06X}")

            for i in range(nbytes):
                byte = self._data_buffer[self._data_index + i]
                bus.dma_write_byte(dma_addr + i, byte)

            self._data_index = len(self._data_buffer)

        elif self._phase == SCSIPhase.DATA_OUT:
            # Transfer data from system RAM to SCSI buffer.
            dma_addr = cpu.a[2] & 0xFFFFFF
            nbytes = len(self._data_buffer) - self._data_index
            self._trace(f"DMA OUT: {nbytes} bytes ← ${dma_addr:06X}")
            self._emit_trace(f"SCSI DMA OUT bytes={nbytes} addr=${dma_addr:06X}")

            if isinstance(self._data_buffer, bytearray):
                for i in range(nbytes):
                    self._data_buffer[self._data_index + i] = bus.dma_read_byte(
                        dma_addr + i
                    )
            self._data_index = len(self._data_buffer)
            # Complete the write operation
            self._complete_write()
            # _complete_write sets phase to STATUS; override to BUS_FREE
            # since the ISR will handle cleanup

        # DMA complete — transition to STATUS phase and schedule interrupt.
        # On real hardware, after DMA the SCSI bus goes through STATUS →
        # MESSAGE IN → BUS FREE.  The driver polls for STATUS phase bits
        # after the ISR returns, then reads status/message bytes to complete
        # the SCSI protocol.
        nbytes_transferred = len(self._data_buffer) - self._data_index
        if self._phase in (SCSIPhase.DATA_IN, SCSIPhase.DATA_OUT):
            nbytes_transferred = len(self._data_buffer)  # all transferred above
        self._phase = SCSIPhase.STATUS
        self._req = True
        self._status_sent = False
        self._message_sent = False
        # ~4 cycles per byte at SCSI-1 speed, minimum 200 cycles
        dma_cycles = max(200, nbytes_transferred * 4)
        self._irq_delay = dma_cycles
        self._trace(f"DMA complete → STATUS phase, IRQ delayed {dma_cycles} cycles")
        self._emit_trace(
            f"SCSI DMA complete status=${self._status_byte:02X} irq_delay={dma_cycles}"
        )
