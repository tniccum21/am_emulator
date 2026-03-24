"""Motorola 68010 CPU core for AlphaSim.

Implements the programmer-visible register set, reset sequence,
instruction fetch/step loop, and stack operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..bus.memory_bus import MemoryBus


# Status register bit masks
SR_CARRY    = 0x0001
SR_OVERFLOW = 0x0002
SR_ZERO     = 0x0004
SR_NEGATIVE = 0x0008
SR_EXTEND   = 0x0010
SR_IPL_MASK = 0x0700
SR_SUPER    = 0x2000
SR_TRACE    = 0x8000

# Condition code masks (low 5 bits of SR)
CCR_MASK = 0x001F


class MC68010:
    """Motorola MC68010 CPU emulation core."""

    _VALID_CPU_MODELS = {"68010", "68020", "68030", "68040"}

    def __init__(self, bus: MemoryBus, cpu_model: str = "68010") -> None:
        self.bus = bus
        normalized_model = cpu_model.upper()
        if normalized_model not in self._VALID_CPU_MODELS:
            raise ValueError(
                f"Unsupported CPU model {cpu_model!r}; expected one of "
                f"{sorted(self._VALID_CPU_MODELS)}"
            )
        self.cpu_model = normalized_model

        # Data registers D0-D7
        self.d: list[int] = [0] * 8

        # Address registers A0-A7 (A7 is the active SP)
        self.a: list[int] = [0] * 8

        # Program counter
        self.pc: int = 0

        # Status register (initialized to supervisor mode, IPL=7)
        self.sr: int = 0x2700

        # Alternate stack pointers
        self.usp: int = 0  # User stack pointer
        self.ssp: int = 0  # Supervisor stack pointer

        # 68010 additions
        self.vbr: int = 0  # Vector base register
        self.cacr: int = 0  # Minimal cache-control register model for CPU probes

        # CPU state
        self.stopped: bool = False
        self.cycles: int = 0
        self.halted: bool = False

        # Instruction dispatch table (populated by opcodes module)
        self.opcode_table: list = []

        # Trace callback (set by debug module)
        self.trace_hook = None

        # Use 68000-style exception frames.  The AM-1200 ROM exception
        # handlers (bus error at $F916/$0AFA, etc.) use hard-coded stack
        # offsets that assume 68000 frame layout.  With 68010 format $0
        # or format $8 frames the handlers read SR/PC from wrong offsets
        # and crash.  Setting this True makes all exceptions push 68000-
        # style frames (6-byte normal, 14-byte bus error) and RTE pop
        # only SR+PC (6 bytes).
        self.use_68000_frames: bool = True

    def supports_control_register(self, control_register: int) -> bool:
        """Return whether MOVEC may access the given control register."""
        return control_register in {0x000, 0x001, 0x800, 0x801} or (
            control_register == 0x002 and self.cpu_model != "68010"
        )

    def read_control_register(self, control_register: int) -> int:
        """Read a supported control register."""
        if control_register == 0x000:  # SFC
            return 0
        if control_register == 0x001:  # DFC
            return 0
        if control_register == 0x002:  # CACR
            return self.cacr
        if control_register == 0x800:  # USP
            return self.usp
        if control_register == 0x801:  # VBR
            return self.vbr
        raise ValueError(f"Unsupported control register ${control_register:03X}")

    def write_control_register(self, control_register: int, value: int) -> None:
        """Write a supported control register."""
        if control_register == 0x002:  # CACR
            # Minimal model: preserve only the bits the AMOS selector probes.
            if self.cpu_model == "68020":
                self.cacr = value & 0x00000200
            elif self.cpu_model in {"68030", "68040"}:
                self.cacr = value & 0x80000200
            else:
                self.cacr = 0
            return
        if control_register == 0x800:  # USP
            self.usp = value & 0xFFFFFFFF
            return
        if control_register == 0x801:  # VBR
            self.vbr = value & 0xFFFFFFFF
            return
        if control_register in {0x000, 0x001}:  # SFC/DFC
            return
        raise ValueError(f"Unsupported control register ${control_register:03X}")

    # ── Status register helpers ──────────────────────────────────────

    @property
    def supervisor(self) -> bool:
        return bool(self.sr & SR_SUPER)

    @property
    def trace_enabled(self) -> bool:
        return bool(self.sr & SR_TRACE)

    def get_ccr(self) -> int:
        return self.sr & CCR_MASK

    def set_ccr(self, value: int) -> None:
        self.sr = (self.sr & ~CCR_MASK) | (value & CCR_MASK)

    def get_ipl_mask(self) -> int:
        return (self.sr & SR_IPL_MASK) >> 8

    def set_sr(self, value: int) -> None:
        """Set entire SR, handling supervisor/user SP swap."""
        old_super = self.supervisor
        self.sr = value & 0xFFFF
        new_super = self.supervisor
        if old_super and not new_super:
            # Leaving supervisor mode: save SSP, load USP
            self.ssp = self.a[7]
            self.a[7] = self.usp
        elif not old_super and new_super:
            # Entering supervisor mode: save USP, load SSP
            self.usp = self.a[7]
            self.a[7] = self.ssp

    # ── Condition code flag helpers ──────────────────────────────────

    def set_flag(self, flag: int, value: bool) -> None:
        if value:
            self.sr |= flag
        else:
            self.sr &= ~flag

    def get_flag(self, flag: int) -> bool:
        return bool(self.sr & flag)

    # ── Condition tests (for Bcc, Scc, DBcc) ─────────────────────────

    def test_condition(self, condition: int) -> bool:
        """Test condition code (0-15) against current CCR flags."""
        c = self.get_flag(SR_CARRY)
        v = self.get_flag(SR_OVERFLOW)
        z = self.get_flag(SR_ZERO)
        n = self.get_flag(SR_NEGATIVE)

        match condition:
            case 0:   return True            # T  (always true)
            case 1:   return False           # F  (always false)
            case 2:   return not c and not z  # HI (high)
            case 3:   return c or z          # LS (low or same)
            case 4:   return not c           # CC (carry clear)
            case 5:   return c               # CS (carry set)
            case 6:   return not z           # NE (not equal)
            case 7:   return z               # EQ (equal)
            case 8:   return not v           # VC (overflow clear)
            case 9:   return v               # VS (overflow set)
            case 10:  return not n           # PL (plus)
            case 11:  return n               # MI (minus)
            case 12:  return n == v          # GE (greater or equal)
            case 13:  return n != v          # LT (less than)
            case 14:  return n == v and not z  # GT (greater than)
            case 15:  return n != v or z     # LE (less or equal)
            case _:   return False

    # ── Memory access (through bus) ──────────────────────────────────

    def read_byte(self, address: int) -> int:
        return self.bus.read_byte(address & 0xFFFFFF)

    def read_word(self, address: int) -> int:
        return self.bus.read_word(address & 0xFFFFFF)

    def read_long(self, address: int) -> int:
        return self.bus.read_long(address & 0xFFFFFF)

    def write_byte(self, address: int, value: int) -> None:
        self.bus.write_byte(address & 0xFFFFFF, value)

    def write_word(self, address: int, value: int) -> None:
        self.bus.write_word(address & 0xFFFFFF, value)

    def write_long(self, address: int, value: int) -> None:
        self.bus.write_long(address & 0xFFFFFF, value)

    # ── Instruction fetch ────────────────────────────────────────────

    def fetch_word(self) -> int:
        """Fetch a 16-bit word at PC and advance PC by 2."""
        w = self.read_word(self.pc)
        self.pc = (self.pc + 2) & 0xFFFFFF
        return w

    def fetch_long(self) -> int:
        """Fetch a 32-bit long at PC and advance PC by 4."""
        hi = self.fetch_word()
        lo = self.fetch_word()
        return (hi << 16) | lo

    # ── Stack operations ─────────────────────────────────────────────

    def push_word(self, value: int) -> None:
        self.a[7] = (self.a[7] - 2) & 0xFFFFFFFF
        self.write_word(self.a[7], value & 0xFFFF)

    def push_long(self, value: int) -> None:
        self.a[7] = (self.a[7] - 4) & 0xFFFFFFFF
        self.write_long(self.a[7], value & 0xFFFFFFFF)

    def pop_word(self) -> int:
        value = self.read_word(self.a[7])
        self.a[7] = (self.a[7] + 2) & 0xFFFFFFFF
        return value

    def pop_long(self) -> int:
        value = self.read_long(self.a[7])
        self.a[7] = (self.a[7] + 4) & 0xFFFFFFFF
        return value

    # ── Register access helpers ──────────────────────────────────────

    def get_d(self, reg: int, size: int) -> int:
        """Get data register value masked to size (1=byte, 2=word, 4=long)."""
        v = self.d[reg]
        if size == 1:
            return v & 0xFF
        if size == 2:
            return v & 0xFFFF
        return v & 0xFFFFFFFF

    def set_d(self, reg: int, size: int, value: int) -> None:
        """Set data register — only affects the low bytes for byte/word sizes."""
        if size == 1:
            self.d[reg] = (self.d[reg] & 0xFFFFFF00) | (value & 0xFF)
        elif size == 2:
            self.d[reg] = (self.d[reg] & 0xFFFF0000) | (value & 0xFFFF)
        else:
            self.d[reg] = value & 0xFFFFFFFF

    def get_a(self, reg: int) -> int:
        return self.a[reg] & 0xFFFFFFFF

    def set_a(self, reg: int, value: int) -> None:
        self.a[reg] = value & 0xFFFFFFFF

    # ── Reset ────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Perform hardware reset sequence.

        Activates phantom ROM, reads SSP from $000000 and PC from $000004,
        sets supervisor mode with IPL=7.
        """
        self.sr = 0x2700  # Supervisor, IPL=7
        self.stopped = False
        self.halted = False
        self.vbr = 0
        self.cacr = 0

        # Activate phantom ROM overlay for vector reads
        self.bus.activate_phantom()

        # Read initial SSP and PC from vectors
        self.ssp = self.read_long(0x000000)
        self.a[7] = self.ssp
        self.pc = self.read_long(0x000004)

        # Phantom served its purpose — deactivate
        self.bus.deactivate_phantom()

    # ── Interrupt checking ───────────────────────────────────────────

    def check_interrupts(self) -> int:
        """Check for pending interrupts and process if appropriate.

        Returns cycle cost if an interrupt was taken, 0 otherwise.
        68000 interrupt priority: level 7 (NMI) is always accepted,
        levels 1-6 accepted if > current IPL mask.

        AM-1200 uses vectored interrupts: the device provides the vector
        number during IACK. Falls back to autovector if device returns 0.
        """
        ipl = self.bus.get_highest_interrupt()
        if ipl == 0:
            return 0

        current_mask = self.get_ipl_mask()

        # Level 7 (NMI) is always accepted; others must exceed mask
        if ipl < 7 and ipl <= current_mask:
            return 0

        # Accept the interrupt — device provides vector during IACK
        from .exceptions import execute_exception

        device_vector = self.bus.acknowledge_interrupt(ipl)

        # Use device-provided vector, fall back to autovector
        vector = device_vector if device_vector else (24 + ipl)

        # execute_exception saves cpu.sr BEFORE modifying it, so we must
        # call it BEFORE updating the IPL mask. This matches real 68010
        # behavior: the OLD SR (with original IPL) gets pushed to the stack.
        execute_exception(self, vector)

        # Now set new IPL mask in the live SR (after old SR was stacked)
        self.sr = (self.sr & ~SR_IPL_MASK) | (ipl << 8)

        # Wake from STOP if needed
        self.stopped = False

        return 44  # approximate interrupt acknowledge + exception cycles

    # ── Step (main execution loop) ───────────────────────────────────

    def step(self) -> int:
        """Execute one instruction. Returns cycle count."""
        if self.halted:
            return 4

        # Check for pending interrupts (even when stopped)
        int_cost = self.check_interrupts()
        if int_cost:
            self.cycles += int_cost
            return int_cost

        if self.stopped:
            return 4  # idle cycles while waiting for interrupt

        # Trace hook for debugging
        if self.trace_hook:
            self.trace_hook(self)

        # Save instruction PC before fetch (needed for bus error frame)
        instruction_pc = self.pc

        # Fetch and execute — catch bus errors from any memory access
        from ..bus.memory_bus import BusError
        try:
            # Fetch opcode word
            opword = self.fetch_word()

            # Dispatch
            if self.opcode_table:
                handler = self.opcode_table[opword]
                if handler is not None:
                    cost = handler(self, opword)
                    self.cycles += cost
                    return cost

            # Unimplemented opcode — trigger illegal instruction exception
            self.pc = (self.pc - 2) & 0xFFFFFF  # back up PC to point at bad opcode
            from .exceptions import execute_exception
            execute_exception(self, 4)  # vector 4 = illegal instruction
            cost = 34
            self.cycles += cost
            return cost

        except BusError as e:
            from .exceptions import execute_bus_error
            # Use current PC (past fetched words), not instruction_pc.
            # The AM-1200 ROM/OS bus error handlers were written for 68000
            # where bus error does NOT restart the faulted instruction —
            # the stacked PC points past the instruction.  Using the
            # already-advanced PC matches that expectation and prevents
            # infinite restart loops (e.g. ROM checksum scan past ROM end).
            execute_bus_error(self, e.address, e.is_write, self.pc)
            cost = 126  # approximate bus error exception processing cycles
            self.cycles += cost
            return cost
