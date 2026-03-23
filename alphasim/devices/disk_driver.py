"""Minimal SCSI disk driver injected into RAM for AM-1200 native boot.

The AM-1200 OS expects disk I/O to work through the IOINI ($A03C) LINE-A
handler.  The native IOINI handler at $1D10 queues DDTs into an I/O queue
and relies on a driver dispatch mechanism (ISR/scheduler) to call the
device driver's XFER entry point.  Without MONGEN having run, there is
no DDT, no DDB, no driver code, and no DDBCHN — so disk I/O fails.

This module provides a boot ROM extension that:
1. Installs a replacement IOINI handler as 68000 code at $B440
2. The handler performs synchronous SCSI PIO reads through the real
   (emulated) SCSI bus interface at $FFFFC8-$FFFFC9
3. Patches $1D10 (original IOINI) to JMP to our handler
4. Sets up DDT at $7038 with valid fields
5. Creates a DDB with disk geometry and links it into DDBCHN
6. Points ZSYDSK ($040C) to the system DDB

This is analogous to what MONGEN (the system generation utility) does
at system build time, plus an integrated PIO disk driver.

IOINI calling convention (from LINE-A $A03C):
    D6 >= 0: DDT-based mount/start request, A0 = DDT
    D6 <  0: DDB-based I/O request, A0 = DDB
        DDB+$08 = DDT back-pointer
        DDB+$0C = buffer address (long)
        DDB+$10 = block number (long)

After completion:
    DDT+$84 = 0 (cleared from $FFFFFFFF pending state)
    DDT+$00 bit 13 ($2000) set (runnable)
"""

from __future__ import annotations

import struct
import sys


# Where the driver + IOINI replacement code is installed
DISK_DRIVER_BASE = 0x00B440

# DDB is placed just after the driver code area
DDB_ADDR = 0x00B600

# DDT address (OS allocates this during init)
DDT_ADDR = 0x7038

# Disk I/O buffer (512 bytes for one sector)
DISK_IO_BUF = 0x00B700

# System variables
ZSYDSK = 0x040C  # System disk DDB pointer
DDBCHN = 0x0408  # DDB chain head

# SCSI bus hardware addresses (absolute short)
SCSI_CTRL = 0xFFC8   # $FFFFC8 control/status register
SCSI_DATA = 0xFFC9   # $FFFFC9 data register

# Original IOINI handler address
ORIG_IOINI = 0x1D10


def assemble_disk_driver() -> bytes:
    """Assemble 68000 IOINI replacement + SCSI PIO read driver.

    Layout at DISK_DRIVER_BASE ($B440):
        +$00: IOINI replacement entry point
              - If disk I/O (D6 < 0 and DDB+$08 matches our DDT): do SCSI PIO
              - If DDT mount (D6 >= 0 and A0 = our DDT): mark complete
              - Otherwise: execute original IOINI handler code
        +$xx: SCSI_READ_BLOCK subroutine (position computed dynamically)
    """
    code = bytearray(512)
    pos = 0
    # Patch slots: (code_offset, label_name) — filled in pass 2
    patches: list[tuple[int, str, str]] = []  # (offset, label, type)
    labels: dict[str, int] = {}

    def emit(words):
        nonlocal pos
        for w in words:
            struct.pack_into(">H", code, pos, w & 0xFFFF)
            pos += 2

    def label(name):
        labels[name] = pos

    def branch_w(opcode, target_label):
        """Emit a Bcc.W with placeholder displacement, record for patching."""
        patches.append((pos, target_label, "bcc_w"))
        emit([opcode, 0x0000])

    def bsr_w(target_label):
        """Emit BSR.W with placeholder displacement."""
        patches.append((pos, target_label, "bcc_w"))
        emit([0x6100, 0x0000])

    # ========================================================
    # IOINI replacement entry point
    # ========================================================
    # MOVE.W #$2700,SR — disable interrupts
    emit([0x46FC, 0x2700])

    # .spinlock: TST.L ($04C0).W — check lock
    label("spinlock")
    emit([0x4AF8, 0x04C0])
    # BNE.S .spinlock
    emit([0x66FA])

    # TST.L D6 — check sign for mount vs I/O
    emit([0x4A86])

    # BPL.W .check_ddt_mount (D6 >= 0)
    branch_w(0x6A00, "check_ddt_mount")

    # D6 < 0: DDB I/O request. Handle ALL disk reads via SCSI PIO.
    # (The OS may use various DDBs allocated by GETMEM, all needing
    # disk service. We don't filter by DDT backpointer.)

    # === HANDLE DISK READ ===
    label("handle_disk_read")
    # MOVEM.L D0-D3/A1-A3/A5,-(SP)
    # D0-D3 = bits 15..12, A1-A3 = bits 6..4, A5 = bit 2
    emit([0x48E7, 0xF074])

    # MOVEA.L $000C(A0),A2 — buffer
    emit([0x2468, 0x000C])

    # MOVE.L $0010(A0),D0 — block number
    emit([0x2028, 0x0010])

    # ADDQ.L #1,D0 — LBA = AMOS block + 1
    emit([0x5280])

    # BSR.W SCSI_READ_BLOCK
    bsr_w("scsi_read")

    # Mark DDT complete via DDB+$08 backpointer
    # MOVEA.L $0008(A0),A1
    emit([0x2268, 0x0008])
    # MOVE.L A1,D1 — test for null
    emit([0x2209])
    # BEQ.S .skip_ddt
    patches.append((pos, "skip_ddt", "bcc_s"))
    emit([0x6700])
    # CLR.L $0084(A1) — clear pending flag
    emit([0x42A9, 0x0084])
    # ORI.W #$2000,(A1) — set runnable bit
    emit([0x0051, 0x2000])

    label("skip_ddt")
    # MOVEM.L (SP)+,D0-D3/A1-A3/A5
    # Reversed: D0..D3 = bits 0..3, A1=9, A2=10, A3=11, A5=13
    emit([0x4CDF, 0x2E0F])

    # Release lock and return
    # CLR.B ($04C0).W
    emit([0x4238, 0x04C0])
    # RTE
    emit([0x4E73])

    # ========================================================
    # .check_ddt_mount (D6 >= 0)
    # Handle ALL mount requests immediately — simulates what
    # the disk driver ISR does after hardware completes.
    # The OS passes A0 = DDT/JCB structure; we set JOBCUR,
    # mark complete, and return.
    # ========================================================
    label("check_ddt_mount")
    # MOVE.L A0,($041C).W — set JOBCUR = A0
    emit([0x21C8, 0x041C])
    # CLR.L $0084(A0) — mark I/O complete
    emit([0x42A8, 0x0084])
    # ORI.W #$2000,(A0) — set runnable bit
    emit([0x0050, 0x2000])
    # Release lock
    emit([0x4238, 0x04C0])
    # RTE
    emit([0x4E73])

    # ========================================================
    # .run_original — not our device, fall through to original
    # ========================================================
    label("run_original")
    # We already did the preamble (SR, lock), so jump past it
    # JMP $001D1A
    emit([0x4EF9, 0x0000, 0x1D1A])

    # ========================================================
    # SCSI_READ_BLOCK subroutine
    # ========================================================
    # Align to word boundary (already aligned)
    label("scsi_read")

    # Input: D0.L = LBA, A2 = buffer
    # Clobbers: D1, D2

    # --- SELECT target ---
    # MOVE.B #$00,($FFC8).W — BUS_FREE
    emit([0x11FC, 0x0000, SCSI_CTRL])
    # MOVE.B #$01,($FFC8).W — SELECT target 0
    emit([0x11FC, 0x0001, SCSI_CTRL])

    # .wait_sel: MOVE.B ($FFC8).W,D1
    label("wait_sel")
    emit([0x1238, SCSI_CTRL])
    # CMPI.B #$11,D1
    emit([0x0C01, 0x0011])
    # BNE.S .wait_sel
    emit([0x66F6])

    # --- COMMAND phase ---
    emit([0x11FC, 0x0016, SCSI_CTRL])

    # READ(6) CDB byte 0: opcode $08
    emit([0x11FC, 0x0008, SCSI_DATA])

    # CDB byte 1: LBA high (bits 16-20)
    # MOVE.L D0,D1
    emit([0x2200])
    # SWAP D1
    emit([0x4841])
    # ANDI.B #$1F,D1
    emit([0x0201, 0x001F])
    # MOVE.B D1,($FFC9).W
    emit([0x11C1, SCSI_DATA])

    # CDB byte 2: LBA mid (bits 8-15)
    emit([0x2200])      # MOVE.L D0,D1
    emit([0xE089])      # LSR.L #8,D1
    emit([0x11C1, SCSI_DATA])

    # CDB byte 3: LBA low (bits 0-7)
    emit([0x11C0, SCSI_DATA])

    # CDB byte 4: block count = 1
    emit([0x11FC, 0x0001, SCSI_DATA])

    # CDB byte 5: control = 0
    emit([0x11FC, 0x0000, SCSI_DATA])

    # --- DATA IN phase ---
    emit([0x11FC, 0x000E, SCSI_CTRL])

    # Read 512 bytes
    emit([0x343C, 0x01FF])   # MOVE.W #511,D2
    label("read_loop")
    emit([0x14F8, SCSI_DATA])  # MOVE.B ($FFC9).W,(A2)+
    emit([0x51CA, 0xFFFC])     # DBRA D2,.read_loop

    # --- STATUS phase ---
    emit([0x11FC, 0x001E, SCSI_CTRL])
    emit([0x1238, SCSI_DATA])  # read status

    # --- MESSAGE IN ---
    emit([0x1238, SCSI_DATA])  # read message

    # --- BUS FREE ---
    emit([0x11FC, 0x0000, SCSI_CTRL])

    # RTS
    emit([0x4E75])

    # ========================================================
    # Pass 2: resolve branch displacements
    # ========================================================
    for patch_pos, target_label, patch_type in patches:
        target = labels[target_label]
        if patch_type == "bcc_w":
            # Bcc.W: displacement from PC+2 (word after opcode)
            disp = target - (patch_pos + 2)
            struct.pack_into(">h", code, patch_pos + 2, disp)
        elif patch_type == "bcc_s":
            # Bcc.S: displacement in low byte of opcode word
            disp = target - (patch_pos + 2)
            assert -128 <= disp <= 127, (
                f"short branch out of range: {target_label} disp={disp}")
            code[patch_pos + 1] = disp & 0xFF

    return bytes(code[:pos])


def build_ddb(ddt_addr: int = DDT_ADDR) -> bytes:
    """Build a DDB (Disk Data Block) with V1.4C disk geometry.

    DDB layout (from bypass analysis):
        +$00: Link to next DDB in DDBCHN (long, 0 = end)
        +$04: DDT pointer or flags (long)
        +$08: DDT back-pointer (long)
        +$0C: DK.BPS = 512 (long) — bytes per sector
        +$10: DK.SPT = 32 (long) — sectors per track
        +$14: DK.SPC = 16 (long) — sectors per cylinder
        +$20: DK.MFD = 1 (long) — MFD block number
        +$24: DK.BMP = 2 (long) — bitmap block number
        +$28: DK.PAR = 0 (long) — partition offset
        +$2C: DK.SIZ = 61531 (long) — total blocks
        +$7C: I/O buffer pointer (long)
    """
    ddb = bytearray(0x80)  # 128 bytes

    # +$00: link (will be set when linking into chain)
    struct.pack_into(">L", ddb, 0x00, 0)

    # +$04: DDT pointer
    struct.pack_into(">L", ddb, 0x04, ddt_addr)

    # +$08: DDT back-pointer
    struct.pack_into(">L", ddb, 0x08, ddt_addr)

    # Disk geometry (V1.4C bootable image)
    struct.pack_into(">L", ddb, 0x0C, 512)      # BPS
    struct.pack_into(">L", ddb, 0x10, 32)        # SPT
    struct.pack_into(">L", ddb, 0x14, 16)        # SPC
    struct.pack_into(">L", ddb, 0x20, 1)         # MFD
    struct.pack_into(">L", ddb, 0x24, 2)         # BMP
    struct.pack_into(">L", ddb, 0x28, 0)         # PAR
    struct.pack_into(">L", ddb, 0x2C, 61531)     # SIZ

    # +$7C: I/O buffer
    struct.pack_into(">L", ddb, 0x7C, DISK_IO_BUF)

    return bytes(ddb)


def build_ddt(ddb_addr: int = DDB_ADDR,
              driver_addr: int = DISK_DRIVER_BASE) -> bytes:
    """Build a DDT (Device Descriptor Table) with driver references.

    DDT layout (138 bytes = $8A):
        +$00: DD.STS — status word (set runnable bit $2000)
        +$02: DD.FLG — flags
        +$04: Pointer to DDB
        +$06: DD.DRV — driver code address
        +$0E: Dispatch table pointer (used by OS for JSR offset calls)
        +$14: DD.INT — interrupt handler address
        +$34: DD.NAM — device name (RAD50 "DSK")
        +$78: Queue link (next DDT in I/O queue, 0 = end)
        +$84: I/O pending flag (0 = idle, $FFFFFFFF = pending)
    """
    ddt = bytearray(0x8A)  # 138 bytes

    # +$00: DD.STS — set runnable ($2000) so scheduler can process
    struct.pack_into(">H", ddt, 0x00, 0x2000)

    # +$02: DD.FLG — PIO mode (no interrupt-driven flag)
    struct.pack_into(">H", ddt, 0x02, 0x0000)

    # +$04: Pointer to system DDB
    struct.pack_into(">L", ddt, 0x04, ddb_addr)

    # +$06: DD.DRV — driver code address
    struct.pack_into(">L", ddt, 0x06, driver_addr)

    # +$0E: Dispatch pointer (point to driver for JSR offset calls)
    struct.pack_into(">L", ddt, 0x0E, driver_addr)

    # +$14: DD.INT — interrupt handler (same as driver entry)
    struct.pack_into(">L", ddt, 0x14, driver_addr)

    # +$34: DD.NAM — "DSK" in RAD50
    # RAD50 encoding: D=4, S=19, K=11 → (4*40+19)*40+11 = 7451 = $1D1B
    # Second word: spaces → $0000
    struct.pack_into(">H", ddt, 0x34, 0x1D1B)
    struct.pack_into(">H", ddt, 0x36, 0x0000)

    # +$78: Queue link — 0 (not in queue)
    struct.pack_into(">L", ddt, 0x78, 0)

    # +$84: I/O pending — 0 (idle)
    struct.pack_into(">L", ddt, 0x84, 0)

    return bytes(ddt)


def install_disk_driver(bus) -> None:
    """Install SCSI disk driver, DDT, DDB, and patch IOINI handler.

    Called after OS load is complete (LED=$0E) but before the first
    IOINI ($A03C) call.  This simulates what MONGEN would set up.

    Args:
        bus: MemoryBus instance
    """
    # 1. Install 68000 driver code at DISK_DRIVER_BASE
    driver_code = assemble_disk_driver()
    for i in range(0, len(driver_code) - 1, 2):
        word = (driver_code[i] << 8) | driver_code[i + 1]
        bus.write_word(DISK_DRIVER_BASE + i, word)

    sys.stderr.write(
        f"[DSK] Driver installed at ${DISK_DRIVER_BASE:06X} "
        f"({len(driver_code)} bytes)\n"
    )

    # 2. Install DDB at DDB_ADDR
    ddb_data = build_ddb()
    for i in range(0, len(ddb_data) - 1, 2):
        word = (ddb_data[i] << 8) | ddb_data[i + 1]
        bus.write_word(DDB_ADDR + i, word)

    sys.stderr.write(
        f"[DSK] DDB at ${DDB_ADDR:06X}: BPS=512 SPT=32 "
        f"SPC=16 MFD=1 BMP=2 SIZ=61531\n"
    )

    # 3. Install DDT at DDT_ADDR
    ddt_data = build_ddt()
    for i in range(0, len(ddt_data) - 1, 2):
        word = (ddt_data[i] << 8) | ddt_data[i + 1]
        bus.write_word(DDT_ADDR + i, word)

    sys.stderr.write(
        f"[DSK] DDT at ${DDT_ADDR:06X}: DRV=${DISK_DRIVER_BASE:06X} "
        f"NAM=DSK\n"
    )

    # 4. Link DDB into DDBCHN
    bus.write_long(DDBCHN, DDB_ADDR)

    # 5. Set ZSYDSK to point to system disk DDB
    bus.write_long(ZSYDSK, DDB_ADDR)

    sys.stderr.write(
        f"[DSK] DDBCHN=${DDB_ADDR:06X} ZSYDSK=${DDB_ADDR:06X}\n"
    )

    # 6. Patch IOINI handler at $1D10 to JMP to our replacement
    # Original $1D10: 46FC 2700 (MOVE.W #$2700,SR)
    #          $1D14: 4AF8 04C0 (TST.L ($04C0).W)
    # Replace with: JMP $00B440 = 4EF9 0000 B440 (6 bytes)
    bus.write_word(ORIG_IOINI, 0x4EF9)
    bus.write_word(ORIG_IOINI + 2, (DISK_DRIVER_BASE >> 16) & 0xFFFF)
    bus.write_word(ORIG_IOINI + 4, DISK_DRIVER_BASE & 0xFFFF)

    sys.stderr.write(
        f"[DSK] Patched IOINI at ${ORIG_IOINI:06X} → "
        f"JMP ${DISK_DRIVER_BASE:06X}\n"
    )

    # 7. Clear the I/O buffer area
    for i in range(0, 512, 2):
        bus.write_word(DISK_IO_BUF + i, 0)
