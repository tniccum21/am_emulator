"""Minimal serial driver injected into RAM for AM-1200 native boot.

The AM-1200 OS expects a terminal driver at the vector ($0462).  During
cold boot, the monitor installs a dummy stub at $632C that clears CCR
and returns without performing any I/O.  The real driver module (e.g.
ATRS.DVR) is loaded later by AMOSL.INI's SET TRM command — but loading
AMOSL.INI itself requires terminal input, creating a chicken-and-egg
problem.

On real hardware, the ROM self-test path configures the ACIA and does
direct hardware I/O for initial terminal detect.  On SCSI-boot systems
(DIP=$0A), the ROM skips self-test entirely, so no firmware-level serial
I/O occurs.

This module injects a minimal 68000 serial driver into high RAM that
provides basic TX/RX functionality via the MC6850 ACIA at $FFFE20.
It models what a boot ROM extension or early driver loader would do.

The driver is installed after the OS is loaded but before terminal
detect runs, by patching the driver vector at $0462.

In addition to serial I/O, the driver's init handler initializes the
free memory pool (MEMBAS/MEMSIZ) if not already set.  On real hardware,
MONGEN embeds these values into AMOSL.MON at build time.  Since the
disk image's AMOSL.MON has MEMBAS=0 (the ROM zeroes the sysvar area
during boot and the init code doesn't recalculate it), this boot ROM
extension provides the equivalent initialization.  This is analogous
to a hardware bootstrap circuit or boot ROM overlay that configures
system memory parameters.

Driver calling convention (observed from OS code):
    D7 = operation code or character to transmit
    A4 = device descriptor
    A6 = driver entry point (from $0462)
    Returns with CCR: Z=0 normally, Z=1 on specific conditions

For the terminal handler at $6C72:
    D7 = $0D (CR) — transmit this character
    Called via JSR (A6) from $6C80

For the I/O framework at $62E0-$62E6:
    D7 = $00 — status/init query
    Called via JSR (A6) from $62E4
    Z=0: "not handled" → framework calls JSR 8(A1) (device handler)
    Z=1: "handled" → framework skips device handler
    The ROM's default stub at $632C returns Z=0, allowing device
    handlers (disk, terminal) at desc+$08 to run.  Returning Z=1
    here blocks ALL device-specific I/O including SCSI disk reads.
"""

from __future__ import annotations

import struct
import sys


# ACIA register addresses (absolute short addressing in 68000 code)
ACIA_STATUS = 0xFE20   # $FFFFFE20 via absolute short
ACIA_DATA = 0xFE22     # $FFFFFE22 via absolute short

# RAM address where the driver code is injected.
# $00B800 is in a gap between the OS system area ($0000-$00B7FF) and
# the disk-loaded OS image ($00F000+).  This region is unused after boot.
DRIVER_BASE = 0x00B800

# The driver vector in system memory
DRIVER_VECTOR = 0x0462


def assemble_driver() -> bytes:
    """Assemble minimal MC68000 serial driver with memory pool init.

    Entry: D7 = char to TX (for TX calls) or 0 (for status/init)
           A4 = device descriptor
    Exit:  CCR set appropriately (Z=0 = "not handled by this driver")

    Layout:
        +$00: TX entry — poll TDRE, write D7 to ACIA data, return
        +$18: Init/status handler — init MEMBAS if needed, return Z=0

    The driver vector at $0462 points to +$00 (TX entry), which is
    the entry point used by the terminal handler and I/O framework.
    The TX entry handles both TX (D7 != 0) and status/init (D7 = 0).

    For D7=0 (init/status), returning Z=0 is critical: the I/O
    framework at $62E6 uses BEQ to decide whether to call the
    device-specific handler at JSR 8(A1).  Z=0 allows the disk
    handler at desc+$08 to run, which issues actual SCSI commands.

    The init handler also checks MEMBAS ($0430) and if zero,
    initializes the free memory pool.  On real hardware, MONGEN
    sets MEMBAS in the AMOSL.MON image at build time.  Since this
    disk image has MEMBAS=0 (ROM zeroes sysvars and init doesn't
    recalculate), the boot ROM extension provides equivalent setup.

    Memory layout assumed:
        MEMBAS = $C000  (past driver area ending ~$BC40)
        MEMSIZ = $0E0000 (below TCB at $0E0000)
        Free block at $C000: next=0, size=$D4000
    """
    code = bytearray(128)
    pos = 0

    def emit(words):
        nonlocal pos
        for w in words:
            struct.pack_into(">H", code, pos, w & 0xFFFF)
            pos += 2

    # +$00: Main entry point (TX / status)
    # TST.B D7          ; Is this a TX request (D7 != 0)?
    emit([0x4A07])                          # +$00: TST.B D7         (2 bytes)
    # BEQ.S to init handler at +$18         ; displacement = $18 - ($02+2) = $14
    emit([0x6714])                          # +$02: BEQ.S +$14       (2 bytes)

    # TX path: poll ACIA TDRE (bit 1 of status), then write D7
    # tx_poll (+$04):
    emit([0x1C38, ACIA_STATUS])             # +$04: MOVE.B ($FE20).W,D6  (4 bytes)
    emit([0x0806, 0x0001])                  # +$08: BTST #1,D6           (4 bytes)
    emit([0x67F6])                          # +$0C: BEQ.S tx_poll (-10)  (2 bytes)

    # MOVE.B D7,($FE22).W  ; Write char to ACIA data register
    emit([0x11C7, ACIA_DATA])               # +$0E: MOVE.B D7,($FE22).W (4 bytes)
    # MOVE #0,CCR          ; Clear all flags (Z=0 = normal return)
    emit([0x44FC, 0x0000])                  # +$12: MOVE #0,CCR          (4 bytes)
    # RTS
    emit([0x4E75])                          # +$16: RTS                  (2 bytes)

    # +$18: Init/status handler (D7=0)
    assert pos == 0x18, f"Init handler at wrong offset: {pos:#x}"

    # Check if MEMBAS is already set — if so, skip memory init
    # TST.L ($0430).W       ; Check MEMBAS
    emit([0x4AB8, 0x0430])                  # +$18: TST.L ($0430).W      (4 bytes)
    # BNE.S done            ; displacement calculated below
    emit([0x6620])                          # +$1C: BNE.S +$20 → $3E    (2 bytes)

    # Set MEMBAS = $C000 (past ZSYDSK driver area)
    # MOVE.L #$0000C000,($0430).W
    emit([0x21FC, 0x0000, 0xC000, 0x0430])  # +$1E: 8 bytes

    # Set MEMSIZ = $0E0000 (below TCB space)
    # MOVE.L #$000E0000,($0438).W
    emit([0x21FC, 0x000E, 0x0000, 0x0438])  # +$26: 8 bytes

    # Initialize free memory block header at $C000
    # CLR.L ($C000).L       ; next pointer = 0 (end of free list)
    emit([0x42B9, 0x0000, 0xC000])          # +$2E: 6 bytes

    # MOVE.L #$0D4000,($C004).L  ; block size = $0E0000 - $C000
    emit([0x23FC, 0x000D, 0x4000,
          0x0000, 0xC004])                  # +$34: 10 bytes

    # done (+$3E):
    assert pos == 0x3E, f"Done label at wrong offset: {pos:#x}"
    # MOVE #0,CCR          ; Set Z=0 ("not handled" — lets device handler run)
    emit([0x44FC, 0x0000])                  # +$3E: MOVE #0,CCR          (4 bytes)
    # RTS
    emit([0x4E75])                          # +$42: RTS                  (2 bytes)

    return bytes(code[:pos])


def install_serial_driver(bus: object) -> None:
    """Inject minimal serial driver into RAM and patch driver vector.

    Called after OS load is complete (after SCSI boot) but before
    terminal detect runs.

    Args:
        bus: MemoryBus instance
    """
    driver_code = assemble_driver()

    # Write driver code to high RAM
    for i, byte in enumerate(driver_code):
        bus.write_byte(DRIVER_BASE + i, byte)

    # Patch driver vector at $0462 to point to our driver
    bus.write_long(DRIVER_VECTOR, DRIVER_BASE)

    sys.stderr.write(
        f"[DRV] Serial driver installed at ${DRIVER_BASE:06X} "
        f"({len(driver_code)} bytes), vector ${DRIVER_VECTOR:04X}\n"
    )
