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
        # 68000-style 6-byte frame: just SR + PC, no format/vector word
        #   Push PC (longword)
        cpu.a[7] = (cpu.a[7] - 4) & 0xFFFFFFFF
        cpu.bus.write_long(cpu.a[7], stacked_pc & 0xFFFFFFFF)

        #   Push SR (word)
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

    if not cpu.use_68000_frames:
        # Pop format/vector word (68010 only)
        format_vector = cpu.bus.read_word(cpu.a[7])
        cpu.a[7] = (cpu.a[7] + 2) & 0xFFFFFFFF

        # Verify format nibble (top 4 bits) = 0 (short frame)
        frame_format = (format_vector >> 12) & 0xF
        if frame_format != 0:
            # Format error — trigger format error exception (vector 14)
            execute_exception(cpu, 14)
            return 50

    # Restore SR (handles supervisor/user SP swap)
    cpu.set_sr(new_sr)
    cpu.pc = new_pc & 0xFFFFFF

    return 20
