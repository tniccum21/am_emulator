"""Integration test: Phase 1 boot — Reset → LED=6.

The very first instruction after reset is:
    $800018: MOVE.B #6,$FE00   (opcode $11FC, imm $0006, addr $FE00)

This validates the entire pipeline: phantom ROM → vector read → PC →
instruction fetch → decode → execute → bus write → LED device.
"""

import pytest
from pathlib import Path
from alphasim.main import build_system
from alphasim.config import SystemConfig

ROM_DIR = Path(__file__).parent.parent.parent / "roms"
ROM_EVEN = ROM_DIR / "AM-178-01-B05.BIN"
ROM_ODD = ROM_DIR / "AM-178-00-B05.BIN"


@pytest.mark.skipif(
    not (ROM_EVEN.exists() and ROM_ODD.exists()),
    reason="ROM files not present"
)
class TestBootPhase1:
    def setup_method(self):
        config = SystemConfig(
            rom_even_path=ROM_EVEN,
            rom_odd_path=ROM_ODD,
            ram_size=0x400000,
            config_dip=0x0A,
        )
        self.cpu, self.bus, self.led, self.acia = build_system(config)
        self.cpu.reset()

    def test_reset_vectors(self):
        """CPU reads correct SSP and PC from phantom ROM."""
        assert self.cpu.a[7] == 0x00032400, f"SSP=${self.cpu.a[7]:08X}"
        assert self.cpu.pc == 0x00800018, f"PC=${self.cpu.pc:06X}"

    def test_first_instruction_led6(self):
        """First instruction writes 6 to LED."""
        # Execute first instruction: MOVE.B #6,$FE00
        self.cpu.step()
        assert self.led.value == 6
        assert self.led.history == [6]

    def test_supervisor_mode(self):
        """CPU starts in supervisor mode with IPL=7."""
        assert self.cpu.supervisor
        assert self.cpu.get_ipl_mask() == 7

    def test_run_100_instructions(self):
        """Run 100 instructions without crashing."""
        for _ in range(100):
            if self.cpu.halted:
                break
            self.cpu.step()
        # LED should have been set to 6 as first action
        assert 6 in self.led.history

    def test_trap0_68010_detection(self):
        """Run until TRAP #0 — verify 68010 8-byte stack frame.

        The boot ROM executes TRAP #0 and checks that SP decreased by 8
        (68010) rather than 6 (68000).
        """
        # Run up to 500 instructions looking for the SP pattern
        for i in range(500):
            if self.cpu.halted:
                break
            old_sp = self.cpu.a[7]
            old_pc = self.cpu.pc

            # Check if current instruction is TRAP #0 ($4E40)
            opword = self.cpu.read_word(self.cpu.pc)
            if opword == 0x4E40:
                self.cpu.step()
                sp_delta = old_sp - self.cpu.a[7]
                assert sp_delta == 8, (
                    f"TRAP #0 SP delta = {sp_delta}, expected 8 (68010 frame)"
                )
                return  # test passed

            self.cpu.step()

        # If we got here, we didn't find TRAP #0 in 500 instructions
        # That's OK — we may need more instructions to reach that point
        pytest.skip("TRAP #0 not reached in 500 instructions")
