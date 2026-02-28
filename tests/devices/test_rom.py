"""Tests for ROM device — EPROM interleaving and verification."""

import pytest
from pathlib import Path
from alphasim.devices.rom import ROM

ROM_DIR = Path(__file__).parent.parent.parent / "roms"
ROM_EVEN = ROM_DIR / "AM-178-01-B05.BIN"
ROM_ODD = ROM_DIR / "AM-178-00-B05.BIN"


@pytest.mark.skipif(
    not (ROM_EVEN.exists() and ROM_ODD.exists()),
    reason="ROM files not present"
)
class TestROM:
    def setup_method(self):
        self.rom = ROM(ROM_EVEN, ROM_ODD)

    def test_rom_size(self):
        assert len(self.rom.data) == 0x4000

    def test_reset_vectors_via_word_swap(self):
        """Verify interleaved data produces correct vectors through word-swap."""
        # Simulate word-swap read (what the bus does)
        def read_word(offset):
            return (self.rom.data[offset + 1] << 8) | self.rom.data[offset]

        def read_long(offset):
            return (read_word(offset) << 16) | read_word(offset + 2)

        ssp = read_long(0)
        pc = read_long(4)
        assert ssp == 0x00032400, f"SSP=${ssp:08X}"
        assert pc == 0x00800018, f"PC=${pc:08X}"

    def test_first_opcode(self):
        """First instruction at $800018 should be MOVE.B ($11FC)."""
        offset = 0x0018  # $800018 - $800000
        opcode = (self.rom.data[offset + 1] << 8) | self.rom.data[offset]
        assert opcode == 0x11FC, f"Got ${opcode:04X}"

    def test_read_at_rom_address(self):
        """ROM.read() returns raw physical bytes at $800000+ addresses."""
        # First byte at $800000
        b = self.rom.read(0x800000, 1)
        assert isinstance(b, int)
        assert 0 <= b <= 255

    def test_write_ignored(self):
        """Writes to ROM are silently ignored."""
        original = self.rom.data[0]
        self.rom.write(0x800000, 1, 0xFF)
        assert self.rom.data[0] == original
