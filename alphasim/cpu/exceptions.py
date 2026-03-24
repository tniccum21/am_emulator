"""68010 exception processing.

Critical: The 68010 uses 8-byte stack frames (not 6 like the 68000).
The boot ROM TRAP #0 test depends on SP delta = 8 to detect a 68010.

68010 stack frame (format 0, pushed to supervisor stack):
    SP+6: format/vector word  = (0 << 12) | (vector_number << 2)
    SP+4: PC high word
    SP+2: PC low word
    SP+0: SR

Push order (from high to low address):
    1. Format/vector word  (SP-=2)
    2. PC longword         (SP-=4)
    3. SR word             (SP-=2)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mc68010 import MC68010

from .mc68010 import SR_SUPER, SR_IPL_MASK, SR_TRACE


def execute_exception(cpu: MC68010, vector_number: int,
                      pc_override: int | None = None) -> None:
    """Process a 68010 exception.

    Args:
        cpu: CPU instance.
        vector_number: Exception vector number (0-255).
        pc_override: If set, use this as the stacked PC instead of current PC.
            Used for bus/address errors where PC needs adjustment.
    """
    # Save current SR before modifying
    old_sr = cpu.sr

    # Switch to supervisor mode if not already
    if not cpu.supervisor:
        cpu.usp = cpu.a[7]
        cpu.sr |= SR_SUPER
        cpu.a[7] = cpu.ssp

    # Disable tracing during exception processing
    cpu.sr &= ~SR_TRACE

    # The PC to save on stack
    stacked_pc = pc_override if pc_override is not None else cpu.pc

    if cpu.use_68000_frames:
        # 68000-style 6-byte frame: just SR + PC, no format/vector word.
        #
        # Some native AM-1200 monitor helpers push a replacement SR word and
        # then execute RTE. Those helpers expect the stacked PC to sit
        # immediately below the software-pushed SR word, so vectors 8/9/26
        # need the synthetic frame laid out as [PC][SR] in memory. Other
        # compatibility-driven handlers still expect the older [SR][PC]
        # layout.
        if vector_number in {8, 9, 26}:
            cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
            cpu.bus.write_word(cpu.a[7], old_sr)
            cpu.a[7] = (cpu.a[7] - 4) & 0xFFFFFFFF
            cpu.bus.write_long(cpu.a[7], stacked_pc & 0xFFFFFFFF)
        else:
            cpu.a[7] = (cpu.a[7] - 4) & 0xFFFFFFFF
            cpu.bus.write_long(cpu.a[7], stacked_pc & 0xFFFFFFFF)
            cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
            cpu.bus.write_word(cpu.a[7], old_sr)
    else:
        # 68010 8-byte frame: SR + PC + format/vector word
        format_vector = (0 << 12) | ((vector_number & 0xFF) << 2)

        #   Push format/vector word
        cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
        cpu.bus.write_word(cpu.a[7], format_vector)

        #   Push PC (longword)
        cpu.a[7] = (cpu.a[7] - 4) & 0xFFFFFFFF
        cpu.bus.write_long(cpu.a[7], stacked_pc & 0xFFFFFFFF)

        #   Push SR (word)
        cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
        cpu.bus.write_word(cpu.a[7], old_sr)

    # Read new PC from vector table (offset by VBR on 68010)
    vector_addr = (cpu.vbr + vector_number * 4) & 0xFFFFFFFF
    new_pc = cpu.read_long(vector_addr)
    cpu.pc = new_pc & 0xFFFFFF

    # Clear stopped state
    cpu.stopped = False


def execute_bus_error(cpu: MC68010, fault_address: int, is_write: bool,
                      instruction_pc: int) -> None:
    """Process a bus error exception with 68000-style stack frame.

    Always uses the 68000 14-byte bus error frame layout regardless of the
    use_68000_frames flag.  The AM-1200 ROM bus error handlers (both the
    generic handler at $0AFA and custom handlers like the ROM checksum scan
    at $F916) were written for the 68000 frame layout and use hard-coded
    stack offsets (e.g. ADDA #$18,A7) that assume the 14-byte format.

    The 68010 format $8 (58-byte) bus error frame would place SR, PC, and
    format/vector at completely different offsets, causing the ROM handlers
    to restore garbage and crash.

    68000 bus error frame (14 bytes, pushed high-to-low):
        SP+10: PC (longword)
        SP+8:  SR (word)
        SP+6:  instruction register (word)
        SP+2:  fault address (longword)
        SP+0:  R/W + function code (word)

    Args:
        cpu: CPU instance.
        fault_address: The address that caused the bus error.
        is_write: True if the faulting access was a write.
        instruction_pc: PC of the instruction that caused the fault.
    """
    # Disable bus errors during frame push to prevent double-fault recursion
    cpu.bus.bus_error_enabled = False

    old_sr = cpu.sr

    # Switch to supervisor mode
    if not cpu.supervisor:
        cpu.usp = cpu.a[7]
        cpu.sr |= SR_SUPER
        cpu.a[7] = cpu.ssp

    cpu.sr &= ~SR_TRACE

    vector_number = 2  # bus error

    # Always use 68000-style 14-byte bus error frame.
    # Push order: PC first (highest address), then SR, instruction
    # register, access address, and finally R/W+FC (lowest address).
    #
    # Resulting stack layout:
    #   SP+0:  R/W + function code (word)
    #   SP+2:  Access address (longword)
    #   SP+6:  Instruction register (word)
    #   SP+8:  Status register (word)
    #   SP+10: Program counter (longword)

    # 1. PC (longword) — goes to SP+10
    cpu.a[7] = (cpu.a[7] - 4) & 0xFFFFFFFF
    cpu.bus.write_long(cpu.a[7], instruction_pc & 0xFFFFFFFF)

    # 2. SR (word) — goes to SP+8
    cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
    cpu.bus.write_word(cpu.a[7], old_sr)

    # 3. Instruction register (word) — goes to SP+6
    cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
    cpu.bus.write_word(cpu.a[7], 0)

    # 4. Access address (longword) — goes to SP+2
    cpu.a[7] = (cpu.a[7] - 4) & 0xFFFFFFFF
    cpu.bus.write_long(cpu.a[7], fault_address & 0xFFFFFFFF)

    # 5. R/W + function code word — goes to SP+0
    rw_fc = 0x0005  # supervisor data
    if is_write:
        rw_fc &= ~0x0010  # R/W bit clear = write
    else:
        rw_fc |= 0x0010  # R/W bit set = read
    cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
    cpu.bus.write_word(cpu.a[7], rw_fc)

    # Read new PC from vector table
    vector_addr = (cpu.vbr + vector_number * 4) & 0xFFFFFFFF
    new_pc = cpu.bus.read_long(vector_addr)
    cpu.pc = new_pc & 0xFFFFFF

    cpu.stopped = False

    # Re-enable bus errors now that frame is complete
    cpu.bus.bus_error_enabled = True


def execute_rte(cpu: MC68010) -> int:
    """Execute RTE (Return from Exception).

    Pops 68010 8-byte stack frame:
        1. SR word
        2. PC longword
        3. Format/vector word (check format nibble = 0)

    Returns cycle count.
    """
    # Pop SR
    new_sr = cpu.bus.read_word(cpu.a[7])
    cpu.a[7] = (cpu.a[7] + 2) & 0xFFFFFFFF

    # Pop PC
    new_pc = cpu.bus.read_long(cpu.a[7])
    cpu.a[7] = (cpu.a[7] + 4) & 0xFFFFFFFF

    if cpu.use_68000_frames:
        # Native AM-1200 low-memory helpers around $004EF8 patch only the low
        # byte of the stacked SR before executing RTE. Those helpers expect the
        # restored state to retain supervisor mode while resuming the wait loop
        # at $001C98/$001CAC.
        if ((((cpu.pc - 2) & 0xFFFFFF) == 0x004EFC) and
                (new_pc & 0xFFFFFF) in {0x001C98, 0x001CAC}):
            new_sr |= SR_SUPER

    if not cpu.use_68000_frames:
        # Pop format/vector word (68010 only)
        format_vector = cpu.bus.read_word(cpu.a[7])
        cpu.a[7] = (cpu.a[7] + 2) & 0xFFFFFFFF

        frame_format = (format_vector >> 12) & 0xF
        if frame_format == 0:
            pass  # short frame — nothing more to pop
        elif frame_format == 0x8:
            # Bus error long frame: skip remaining 50 bytes (25 words)
            cpu.a[7] = (cpu.a[7] + 50) & 0xFFFFFFFF
        else:
            # Unknown format — trigger format error exception (vector 14)
            execute_exception(cpu, 14)
            return 50

    # Restore SR (handles supervisor/user SP swap)
    cpu.set_sr(new_sr)
    cpu.pc = new_pc & 0xFFFFFF

    return 20
