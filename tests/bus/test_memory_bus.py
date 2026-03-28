"""Tests for memory bus word-level byte-swap."""

import pytest
from alphasim.bus.memory_bus import MemoryBus
from alphasim.devices.ram import RAM


class TestByteSwap:
    """Verify the Alpha Micro word-level byte-swap model."""

    def setup_method(self):
        self.bus = MemoryBus()
        self.ram = RAM(0x1000)
        self.bus.set_ram(self.ram)

    def test_byte_read_write_no_swap(self):
        """Byte operations pass through without swap."""
        self.bus.write_byte(0x100, 0xAB)
        assert self.bus.read_byte(0x100) == 0xAB
        # Verify raw RAM matches
        assert self.ram.data[0x100] == 0xAB

    def test_word_swap(self):
        """Word read/write applies byte-swap within word."""
        # Write CPU word $1234 to address $100
        self.bus.write_word(0x100, 0x1234)
        # Physical layout: phys[$100] = $34 (low), phys[$101] = $12 (high)
        assert self.ram.data[0x100] == 0x34
        assert self.ram.data[0x101] == 0x12
        # Read back as word: (phys[$101] << 8) | phys[$100] = $1234
        assert self.bus.read_word(0x100) == 0x1234

    def test_long_swap(self):
        """Long read/write applies word-swap to each word."""
        self.bus.write_long(0x100, 0xDEADBEEF)
        # High word $DEAD at $100: phys[$100]=$AD, phys[$101]=$DE
        assert self.ram.data[0x100] == 0xAD
        assert self.ram.data[0x101] == 0xDE
        # Low word $BEEF at $102: phys[$102]=$EF, phys[$103]=$BE
        assert self.ram.data[0x102] == 0xEF
        assert self.ram.data[0x103] == 0xBE
        # Read back
        assert self.bus.read_long(0x100) == 0xDEADBEEF

    def test_byte_within_word(self):
        """Writing individual bytes, then reading as word.

        AM-1200 byte writes go to physical address directly (no swap).
        Word read applies swap: (phys[addr+1] << 8) | phys[addr].
        So phys[0x200]=0x01 (low byte), phys[0x201]=0x02 (high byte),
        word = (0x02 << 8) | 0x01 = 0x0201.
        """
        self.bus.write_byte(0x200, 0x01)
        self.bus.write_byte(0x201, 0x02)
        # Word read: (phys[$201] << 8) | phys[$200] = $0201
        assert self.bus.read_word(0x200) == 0x0201

    def test_24bit_masking(self):
        """Addresses are masked to 24 bits."""
        self.bus.write_byte(0x100, 0x42)
        assert self.bus.read_byte(0x01000100) == 0x42


class TestPhantomROM:
    """Test phantom ROM overlay on reset."""

    def setup_method(self):
        self.bus = MemoryBus()
        self.ram = RAM(0x1000)
        self.bus.set_ram(self.ram)

    def test_phantom_deactivated_by_default(self):
        """Without activation, reads from $000000 come from RAM."""
        assert self.bus.read_byte(0) == 0

    def test_phantom_stays_active_until_deactivated(self):
        """Phantom serves all reads to vector area until explicitly disabled."""
        from alphasim.devices.base import IODevice

        class MockROM(IODevice):
            def read(self, address, size):
                return 0xAA
            def write(self, address, size, value):
                pass

        rom = MockROM()
        self.bus.set_rom(rom)
        self.bus.activate_phantom()

        # All reads to vector area come from ROM
        assert self.bus.read_byte(0) == 0xAA
        assert self.bus.read_byte(4) == 0xAA
        assert self.bus.read_byte(7) == 0xAA
        # Explicitly deactivate
        self.bus.deactivate_phantom()
        # Now reads come from RAM
        assert self.bus.read_byte(0) == 0

    def test_phantom_write_disables(self):
        """Any write to vector area disables phantom."""
        from alphasim.devices.base import IODevice

        class MockROM(IODevice):
            def read(self, address, size):
                return 0xBB
            def write(self, address, size, value):
                pass

        rom = MockROM()
        self.bus.set_rom(rom)
        self.bus.activate_phantom()

        # Write to vector area
        self.bus.write_byte(0, 0x00)
        # Phantom should be disabled now
        assert self.bus.read_byte(0) == 0x00  # from RAM

    def test_phantom_only_affects_vector_area(self):
        """Phantom only overlays $000000-$000007."""
        from alphasim.devices.base import IODevice

        class MockROM(IODevice):
            def read(self, address, size):
                return 0xCC
            def write(self, address, size, value):
                pass

        rom = MockROM()
        self.bus.set_rom(rom)
        self.bus.activate_phantom()

        # Read outside vector area — from RAM, not ROM
        assert self.bus.read_byte(0x10) == 0
