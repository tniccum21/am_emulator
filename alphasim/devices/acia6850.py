"""MC6850 ACIA serial port for Alpha Micro AM-1200.

Three ACIA chips provide serial ports 0-2:
    Port 0 (console): $FFFE20 (status/control), $FFFE22 (data)
    Port 1:           $FFFE24 (status/control), $FFFE26 (data)
    Port 2:           $FFFE30 (status/control), $FFFE32 (data)

The 6850 is an 8-bit device on the upper data bus (D8-D15),
so registers appear at EVEN byte addresses.

Status Register (read):
    Bit 0: RDRF — Receive Data Register Full
    Bit 1: TDRE — Transmit Data Register Empty
    Bit 2: DCD  — Data Carrier Detect (active low on hardware)
    Bit 3: CTS  — Clear To Send (active low on hardware)
    Bit 4: FE   — Framing Error
    Bit 5: OVRN — Receiver Overrun
    Bit 6: PE   — Parity Error
    Bit 7: IRQ  — Interrupt Request

Control Register (write):
    Bits 0-1: Counter Divide Select (00=÷1, 01=÷16, 10=÷64, 11=Master Reset)
    Bits 2-4: Word Select (data bits, parity, stop bits)
    Bits 5-6: Transmit Control (RTS, TX IRQ enable)
    Bit 7:    Receive Interrupt Enable
"""

from __future__ import annotations

import sys
from collections import deque

from .base import IODevice


class ACIA6850(IODevice):
    """MC6850 ACIA handling ports 0-2 at $FFFE20-$FFFE32."""

    BASE = 0xFFFE20

    # Port address mapping: (status_addr, data_addr)
    PORT_MAP = {
        0: (0xFFFE20, 0xFFFE22),
        1: (0xFFFE24, 0xFFFE26),
        2: (0xFFFE30, 0xFFFE32),
    }

    def __init__(self, debug: bool = False) -> None:
        self._debug = debug

        # Per-port state
        self._control = [0x00, 0x00, 0x00]  # Control registers
        self._tx_data = [0x00, 0x00, 0x00]  # Last transmitted byte
        self._rx_data = [0x00, 0x00, 0x00]  # Received data register
        self._rdrf = [False, False, False]    # Receive data ready
        self._ovrn = [False, False, False]    # Receiver overrun

        # Receive queue (for feeding input to the port)
        self._rx_queue: list[deque] = [deque(), deque(), deque()]

        # Transmit output capture (list of bytes sent by the CPU)
        self._tx_output: list[list[int]] = [[], [], []]

        # Transmit callback (called with port, byte when CPU sends data)
        self.tx_callback = None

    def _trace(self, msg: str) -> None:
        if self._debug:
            print(f"[ACIA] {msg}", file=sys.stderr)

    def _addr_to_port_reg(self, address: int) -> tuple[int, str] | None:
        """Map address to (port_number, 'status'|'data') or None."""
        for port, (status_addr, data_addr) in self.PORT_MAP.items():
            if address == status_addr:
                return (port, "status")
            if address == data_addr:
                return (port, "data")
        return None

    def _is_master_reset(self, port: int) -> bool:
        """Check if port is in master reset state (CR1:CR0 = 11)."""
        return (self._control[port] & 0x03) == 0x03

    def _rx_irq_enabled(self, port: int) -> bool:
        """Check if receive interrupt is enabled (CR bit 7)."""
        return bool(self._control[port] & 0x80)

    def _tx_irq_enabled(self, port: int) -> bool:
        """Check if transmit interrupt is enabled (CR bits 5-6 = 01)."""
        return (self._control[port] & 0x60) == 0x20

    # ── Public interface for feeding input ──────────────────────────

    def send_to_port(self, port: int, data: bytes) -> None:
        """Feed received data to a port (from host terminal to emulated system)."""
        for b in data:
            self._rx_queue[port].append(b)
        # If receive register is empty, load next byte
        if not self._rdrf[port] and self._rx_queue[port]:
            self._rx_data[port] = self._rx_queue[port].popleft()
            self._rdrf[port] = True

    def get_tx_output(self, port: int) -> list[int]:
        """Get and clear transmitted bytes from a port."""
        data = self._tx_output[port]
        self._tx_output[port] = []
        return data

    # ── IODevice interface ──────────────────────────────────────────

    def read(self, address: int, size: int) -> int:
        result = self._addr_to_port_reg(address)
        if result is None:
            return 0xFF

        port, reg_type = result

        if reg_type == "status":
            if self._is_master_reset(port):
                return 0x00

            status = 0x00
            # Bit 0: RDRF
            if self._rdrf[port]:
                status |= 0x01
            # Bit 1: TDRE — always ready to transmit
            status |= 0x02
            # Bit 2: DCD — carrier detect (0 = carrier present)
            # Bit 3: CTS — clear to send (0 = clear)
            # Bit 5: OVRN
            if self._ovrn[port]:
                status |= 0x20
            # Bit 7: IRQ
            if ((self._rdrf[port] and self._rx_irq_enabled(port)) or
                    self._tx_irq_enabled(port)):
                status |= 0x80
            return status

        if reg_type == "data":
            # Read receive data register
            data = self._rx_data[port]
            self._rdrf[port] = False
            self._ovrn[port] = False
            # Load next byte from queue if available
            if self._rx_queue[port]:
                self._rx_data[port] = self._rx_queue[port].popleft()
                self._rdrf[port] = True
            self._trace(f"Port {port} RX: ${data:02X} ({chr(data) if 0x20 <= data < 0x7F else '?'})")
            return data

        return 0xFF

    def write(self, address: int, size: int, value: int) -> None:
        result = self._addr_to_port_reg(address)
        if result is None:
            return

        port, reg_type = result
        value &= 0xFF

        if reg_type == "status":
            # Writing to status address = control register
            old_cr = self._control[port]
            self._control[port] = value

            if (value & 0x03) == 0x03:
                # Master reset
                self._rdrf[port] = False
                self._ovrn[port] = False
                self._trace(f"Port {port} master reset")
            elif (old_cr & 0x03) == 0x03:
                # Coming out of reset
                self._trace(f"Port {port} control = ${value:02X}")

        elif reg_type == "data":
            # Transmit data
            self._tx_data[port] = value
            self._tx_output[port].append(value)
            self._trace(f"Port {port} TX: ${value:02X} ({chr(value) if 0x20 <= value < 0x7F else '?'})")
            if self.tx_callback:
                self.tx_callback(port, value)

    def tick(self, cycles: int) -> None:
        pass

    def get_interrupt_level(self) -> int:
        """ACIA generates IPL 1 interrupts."""
        for port in range(3):
            if self._is_master_reset(port):
                continue
            if self._rdrf[port] and self._rx_irq_enabled(port):
                return 1
            if self._tx_irq_enabled(port):
                return 1
        return 0

    def get_interrupt_vector(self) -> int:
        """ACIA provides vector 64 during IACK (IPL 1 → vector 64)."""
        return 64

    def acknowledge_interrupt(self, level: int) -> None:
        """IACK — ACIA interrupt is level-sensitive, cleared by reading status."""
        pass  # ACIA clears IRQ when software reads status + data registers
