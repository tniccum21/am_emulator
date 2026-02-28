"""Abstract base class for all I/O devices."""

from abc import ABC, abstractmethod


class IODevice(ABC):
    """Base class for memory-mapped I/O devices.

    All devices respond to byte-level reads and writes at their mapped addresses.
    The memory bus handles word-level byte-swap; devices only see byte operations.
    """

    @abstractmethod
    def read(self, address: int, size: int) -> int:
        """Read from device register.

        Args:
            address: Absolute address being accessed.
            size: 1 for byte. Word/long accesses are decomposed by the bus.

        Returns:
            Byte value (0-255).
        """

    @abstractmethod
    def write(self, address: int, size: int, value: int) -> None:
        """Write to device register.

        Args:
            address: Absolute address being accessed.
            size: 1 for byte.
            value: Byte value (0-255).
        """

    def tick(self, cycles: int) -> None:
        """Advance device state by the given number of CPU cycles."""

    def get_interrupt_level(self) -> int:
        """Return current interrupt request level (0 = none, 1-7 = IPL)."""
        return 0

    def get_interrupt_vector(self) -> int:
        """Return the vector number this device provides during IACK.

        The AM-1200 uses vectored interrupts. Each device provides its
        vector number during the interrupt acknowledge cycle:
          - ACIA (IPL 1) → vector 64
          - SASI (IPL 2) → vector 65
          - Timer (IPL 3) → vector 66

        Returns 0 to use autovector (vector = 24 + level) as fallback.
        """
        return 0

    def acknowledge_interrupt(self, level: int) -> None:
        """Called when the CPU acknowledges an interrupt at the given level.

        Devices should clear their interrupt request in response.
        """
