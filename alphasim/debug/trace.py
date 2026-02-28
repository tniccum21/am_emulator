"""Instruction trace logger for debugging."""

from __future__ import annotations

import sys
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from ..cpu.mc68010 import MC68010

from ..cpu.disassemble import disassemble_one


class TraceLogger:
    """Logs each instruction before execution."""

    def __init__(self, output: IO[str] | None = None, max_lines: int = 0):
        self.output = output or sys.stderr
        self.max_lines = max_lines
        self.line_count = 0

    def trace_hook(self, cpu: MC68010) -> None:
        """Called before each instruction executes."""
        if self.max_lines and self.line_count >= self.max_lines:
            return

        pc = cpu.pc
        try:
            text, _ = disassemble_one(cpu.bus, pc)
        except Exception:
            text = "???"

        # Format: PC  opcode  disasm  D0-D7  A0-A7  SR
        d_regs = " ".join(f"D{i}={cpu.d[i]:08X}" for i in range(8))
        a_regs = " ".join(f"A{i}={cpu.a[i]:08X}" for i in range(8))
        sr = cpu.sr

        line = f"{pc:06X}  {text:30s}  {d_regs}  {a_regs}  SR={sr:04X}\n"
        self.output.write(line)
        self.line_count += 1
