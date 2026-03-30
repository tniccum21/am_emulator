"""Loop accelerators for known CPU-intensive patterns.

These detect tight loops and compute their result directly instead of
executing thousands of iterations.  Attached as a trace_hook on the CPU.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mc68010 import MC68010
    from ..bus.memory_bus import MemoryBus


class LoopAccelerator:
    """Detects and accelerates known loop patterns.

    Patterns handled:
    1. Division by repeated subtraction:
       ADDQ.L #1,Dn / SUB.L Dm,Dk / BPL back
    2. DBcc countdown loops:
       DBF Dn,back  (opcode $51C8-$51CF)
    3. SUBQ+BNE delay loops:
       SUBQ.x #n,Dn / BNE back
    """

    def __init__(self, bus: MemoryBus, enabled: bool = True):
        self.bus = bus
        self.enabled = enabled
        # For SUBQ+BNE detection (needs consecutive same-PC hits)
        self._prev_pc = -1
        self._prev2_pc = -1
        self._loop_pc = -1
        self._loop_count = 0
        self._SUBQ_BNE_THRESHOLD = 10
        # Stats
        self.div_accel_count = 0
        self.dbcc_accel_count = 0
        self.subq_accel_count = 0

    def hook(self, cpu: MC68010) -> None:
        """Trace hook called before each instruction."""
        if not self.enabled:
            return

        pc = cpu.pc & 0xFFFFFF
        bus = self.bus

        # ── Division by repeated subtraction ──
        # Pattern: ADDQ.L #1,Dn ($5280+n) / SUB.L Dm,Dk ($9x8y) / BPL $xxxx
        # Check for ADDQ.L at current PC
        try:
            op1 = bus.read_word(pc)
        except Exception:
            self._update_history(pc)
            return

        if (op1 & 0xF1F8) == 0x5080:  # ADDQ.L #1-8,Dn
            try:
                op2 = bus.read_word(pc + 2)
                op3 = bus.read_word(pc + 4)
            except Exception:
                self._update_history(pc)
                return

            # SUB.L Dm,Dk: $9x80-$9x87 where x = Dk<<1|1, low 3 = Dm
            if (op2 & 0xF0C0) == 0x9080:
                # BPL back to this PC
                if (op3 & 0xFF00) == 0x6A00:
                    disp = op3 & 0xFF
                    if disp == 0:
                        try:
                            disp = bus.read_word(pc + 6)
                            if disp & 0x8000:
                                disp -= 0x10000
                        except Exception:
                            self._update_history(pc)
                            return
                    else:
                        if disp & 0x80:
                            disp -= 0x100

                    target = (pc + 6 + disp) & 0xFFFFFF  # BPL PC+2 relative
                    if target == pc:
                        addq_val = (op1 >> 9) & 7
                        if addq_val == 0:
                            addq_val = 8
                        counter_reg = op1 & 7
                        divisor_reg = op2 & 7
                        dividend_reg = (op2 >> 9) & 7

                        d_divisor = cpu.d[divisor_reg] & 0xFFFFFFFF
                        d_dividend = cpu.d[dividend_reg] & 0xFFFFFFFF

                        if d_divisor > 0 and d_dividend > d_divisor * 10:
                            iters = d_dividend // d_divisor
                            cpu.d[counter_reg] = (
                                cpu.d[counter_reg] + iters * addq_val
                            ) & 0xFFFFFFFF
                            cpu.d[dividend_reg] = (
                                d_dividend - iters * d_divisor
                            ) & 0xFFFFFFFF
                            self.div_accel_count += 1

        # ── DBcc acceleration ──
        # DBF Dn: $51C8-$51CF with 16-bit displacement
        if pc == self._prev_pc:
            if (op1 & 0xFFF8) == 0x51C8:
                reg = op1 & 7
                low16 = cpu.d[reg] & 0xFFFF
                if low16 > 1:
                    cpu.d[reg] = cpu.d[reg] & 0xFFFF0000
                    cpu.add_timing_cycles(low16 * 10)
                    self.dbcc_accel_count += 1

        # ── SUBQ+BNE delay loop acceleration ──
        # Requires 3 consecutive visits to same PC (the SUBQ instruction)
        if (
            pc == self._prev2_pc
            and self._prev_pc == pc + 2
            and pc < 0x10000
        ):
            if pc == self._loop_pc:
                self._loop_count += 1
            else:
                self._loop_pc = pc
                self._loop_count = 1

            if self._loop_count >= self._SUBQ_BNE_THRESHOLD:
                try:
                    op2 = bus.read_word(pc + 2)
                except Exception:
                    self._update_history(pc)
                    return

                # SUBQ.W or SUBQ.L #n,Dn / BNE back
                if (
                    (op1 & 0xF100) == 0x5100
                    and (op1 & 0x00C0) in (0x0040, 0x0080)
                    and (op1 & 0x0038) == 0x0000
                    and (op2 & 0xFF00) == 0x6600
                ):
                    reg = op1 & 7
                    data = (op1 >> 9) & 7
                    if data == 0:
                        data = 8
                    size = 2 if (op1 & 0x00C0) == 0x0040 else 4
                    mask = 0xFFFF if size == 2 else 0xFFFFFFFF
                    current = cpu.d[reg] & mask
                    if current > data:
                        skipped_iters = (current - 1) // data
                        cpu.d[reg] = (cpu.d[reg] & ~mask) | data
                        cpu.add_timing_cycles(skipped_iters * 14)
                        self._loop_count = 0
                        self.subq_accel_count += 1
        else:
            if pc != self._loop_pc and self._prev_pc != self._loop_pc:
                self._loop_count = 0
                self._loop_pc = -1

        self._update_history(pc)

    def _update_history(self, pc: int) -> None:
        self._prev2_pc = self._prev_pc
        self._prev_pc = pc
