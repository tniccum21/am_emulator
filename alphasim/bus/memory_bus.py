"""Memory bus with Alpha Micro word-level byte-swap.

Alpha Micro hardware swaps byte lanes between the 68000 data bus and
system RAM / peripheral bus at the WORD level.  This preserves PDP-11
style word layout in physical memory while being transparent to the CPU
for word-sized operations.

Word read:  CPU gets (phys[addr+1] << 8) | phys[addr]   (swap within word)
Word write: phys[addr] = cpu_low_byte, phys[addr+1] = cpu_high_byte
Byte read:  CPU gets phys[addr] directly                 (no swap)
Byte write: phys[addr] = value directly                  (no swap)

Devices store and return raw physical bytes.  The bus applies the
word-level swap for .W and .L accesses only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..devices.base import IODevice


class MemoryBus:
    """Address-decoding memory bus with word-level byte-swap.

    Address map (24-bit, active bits A23-A0):
        $000000–$3FFFFF  RAM (up to 4MB)
        $800000–$803FFF  Boot ROM (16KB)
        $FFFE00          LED (write-only, abs short $FE00)
        $FFFE03          Config DIP (read-only, abs short $FE03)
        $FFFFE0–$FFFFE7  SASI controller
        ...              other I/O via registered ranges
    """

    def __init__(self) -> None:
        self._ram: IODevice | None = None
        self._rom: IODevice | None = None
        self._devices: list[tuple[int, int, IODevice]] = []  # (start, end, device)

        # Phantom ROM state: overlays ROM onto $000000-$000007 during reset
        # so the CPU can read SSP and PC vectors from ROM.
        # Disables after both vectors are read (8 bytes) or on any write.
        self._phantom_active: bool = False

    # ── Device registration ──────────────────────────────────────────

    def set_ram(self, ram: IODevice) -> None:
        self._ram = ram

    def set_rom(self, rom: IODevice) -> None:
        self._rom = rom

    def register_device(self, start: int, end: int, device: IODevice) -> None:
        """Register an I/O device for address range [start, end] inclusive."""
        self._devices.append((start, end, device))

    # ── Phantom ROM ──────────────────────────────────────────────────

    def activate_phantom(self) -> None:
        """Activate phantom ROM overlay on reset."""
        self._phantom_active = True

    def _is_phantom_read(self, address: int) -> bool:
        """Check if this read should come from phantom ROM.

        Returns True if the address is in the vector area ($000000-$000007)
        and the phantom is still active.  Does NOT change phantom state —
        phantom is disabled by deactivate_phantom() or write.
        """
        if not self._phantom_active:
            return False
        return address <= 0x000007

    def deactivate_phantom(self) -> None:
        """Explicitly disable phantom ROM (called after vector reads)."""
        self._phantom_active = False

    def _phantom_write_disable(self, address: int) -> None:
        """Any write disables phantom ROM."""
        if self._phantom_active:
            self._phantom_active = False

    # ── Raw byte-level access (no swap) ──────────────────────────────

    def _read_byte_physical(self, address: int) -> int:
        """Read a single byte from the physical address space."""
        address &= 0xFFFFFF  # 24-bit masking

        # Phantom ROM: vector reads from ROM overlay
        if self._is_phantom_read(address):
            rom_addr = 0x800000 | (address & 0x7)
            return self._rom.read(rom_addr, 1) if self._rom else 0xFF

        # I/O devices (check first — they may overlap RAM range)
        for start, end, device in self._devices:
            if start <= address <= end:
                return device.read(address, 1)

        # ROM: $800000–$803FFF
        if 0x800000 <= address <= 0x803FFF:
            return self._rom.read(address, 1) if self._rom else 0xFF

        # RAM: $000000–(ram_size-1)
        if self._ram is not None:
            return self._ram.read(address, 1)

        # Unmapped
        return 0xFF

    def _write_byte_physical(self, address: int, value: int) -> None:
        """Write a single byte to the physical address space."""
        address &= 0xFFFFFF

        self._phantom_write_disable(address)

        # I/O devices
        for start, end, device in self._devices:
            if start <= address <= end:
                device.write(address, 1, value & 0xFF)
                return

        # ROM (writes ignored)
        if 0x800000 <= address <= 0x803FFF:
            return

        # RAM
        if self._ram is not None:
            self._ram.write(address, 1, value & 0xFF)
            return

        # Unmapped — silently ignored

    # ── CPU-facing access (with word-swap) ───────────────────────────

    def read_byte(self, address: int) -> int:
        """CPU byte read — no swap, returns physical byte directly."""
        return self._read_byte_physical(address)

    def read_word(self, address: int) -> int:
        """CPU word read — word-level byte-swap.

        68000 word accesses always use even addresses (A0 masked on bus).
        Reads two physical bytes and swaps them:
            CPU value = (phys[addr+1] << 8) | phys[addr]
        """
        address &= ~1  # 68000 masks A0 for word/long bus cycles
        lo = self._read_byte_physical(address)
        hi = self._read_byte_physical(address + 1)
        return (hi << 8) | lo

    def read_long(self, address: int) -> int:
        """CPU long read — two word reads (high word first)."""
        address &= ~1  # ensure even alignment
        hi_word = self.read_word(address)
        lo_word = self.read_word(address + 2)
        return (hi_word << 16) | lo_word

    def write_byte(self, address: int, value: int) -> None:
        """CPU byte write — no swap, stores to physical address directly."""
        self._write_byte_physical(address, value)

    def write_word(self, address: int, value: int) -> None:
        """CPU word write — word-level byte-swap.

        68000 word accesses always use even addresses (A0 masked on bus).
        CPU high byte → phys[addr+1], CPU low byte → phys[addr]
        """
        address &= ~1  # 68000 masks A0 for word/long bus cycles
        self._write_byte_physical(address, value & 0xFF)
        self._write_byte_physical(address + 1, (value >> 8) & 0xFF)

    def write_long(self, address: int, value: int) -> None:
        """CPU long write — two word writes (high word first)."""
        address &= ~1  # ensure even alignment
        self.write_word(address, (value >> 16) & 0xFFFF)
        self.write_word(address + 2, value & 0xFFFF)

    # ── Device ticking ───────────────────────────────────────────────

    def tick(self, cycles: int) -> None:
        """Advance all registered devices by the given number of cycles."""
        for _, _, device in self._devices:
            device.tick(cycles)

    def get_highest_interrupt(self) -> int:
        """Poll all devices and return the highest pending interrupt level."""
        highest = 0
        for _, _, device in self._devices:
            level = device.get_interrupt_level()
            if level > highest:
                highest = level
        return highest

    def acknowledge_interrupt(self, level: int) -> int:
        """Notify all devices that an interrupt at the given level was accepted.

        This corresponds to the hardware IACK cycle. The device provides
        its vector number (AM-1200 uses vectored interrupts).

        Returns the vector number from the device, or 0 for autovector.
        """
        vector = 0
        for _, _, device in self._devices:
            if device.get_interrupt_level() == level:
                device.acknowledge_interrupt(level)
                vec = device.get_interrupt_vector()
                if vec:
                    vector = vec
        return vector
