"""ROM device — loads interleaved EPROM pair into physical memory layout.

Alpha Micro ROM chip wiring (with byte-lane swap at bus interface):
  ROM01 (AM-178-01-B05) → system D7-D0  → after swap → CPU D15-D8 (high byte)
  ROM00 (AM-178-00-B05) → system D15-D8 → after swap → CPU D7-D0  (low byte)

Physical memory layout (what the bus sees before swap):
  phys[even] = ROM00 byte  (becomes CPU low byte after word-swap)
  phys[odd]  = ROM01 byte  (becomes CPU high byte after word-swap)
"""

from pathlib import Path
from .base import IODevice


class ROM(IODevice):
    """Boot ROM from interleaved EPROM pair.

    Data is stored in physical layout.  The memory bus applies the
    word-level byte-swap on reads so the CPU sees correct opcodes/data.
    """

    ROM_SIZE = 0x4000  # 16KB combined

    def __init__(self, even_path: Path, odd_path: Path):
        """Load and interleave the EPROM pair.

        Args:
            even_path: Path to AM-178-01-B05.BIN (high byte to CPU after swap).
            odd_path:  Path to AM-178-00-B05.BIN (low byte to CPU after swap).
        """
        rom01 = even_path.read_bytes()  # CPU high byte (after swap)
        rom00 = odd_path.read_bytes()   # CPU low byte (after swap)

        if len(rom01) != 8192 or len(rom00) != 8192:
            raise ValueError(
                f"EPROM files must be 8192 bytes each "
                f"(got {len(rom01)}, {len(rom00)})"
            )

        # Physical layout: ROM00 at even addresses, ROM01 at odd addresses.
        # After the bus word-swap, ROM01 becomes the CPU high byte and
        # ROM00 becomes the CPU low byte — matching the chip labels.
        self.data = bytearray(self.ROM_SIZE)
        for i in range(8192):
            self.data[2 * i]     = rom00[i]  # physical even = CPU low
            self.data[2 * i + 1] = rom01[i]  # physical odd  = CPU high

        self._verify()

    def _verify(self) -> None:
        """Verify the interleaved ROM produces correct reset vectors."""
        # Word-swap read: (phys_odd << 8) | phys_even
        def read_word(offset: int) -> int:
            return (self.data[offset + 1] << 8) | self.data[offset]

        def read_long(offset: int) -> int:
            return (read_word(offset) << 16) | read_word(offset + 2)

        ssp = read_long(0)
        pc = read_long(4)

        if ssp != 0x00032400:
            raise ValueError(
                f"ROM SSP verification failed: got ${ssp:08X}, "
                f"expected $00032400"
            )
        if pc != 0x00800018:
            raise ValueError(
                f"ROM PC verification failed: got ${pc:08X}, "
                f"expected $00800018"
            )

    def read(self, address: int, size: int) -> int:
        offset = address - 0x800000
        if 0 <= offset < self.ROM_SIZE:
            return self.data[offset]
        return 0xFF

    def write(self, address: int, size: int, value: int) -> None:
        pass  # ROM is read-only
