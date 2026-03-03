"""MC6850 ACIA serial port for Alpha Micro AM-1200.

Three ACIA chips provide serial ports 0-2:
    Port 0 (console): $FFFE20 (status/control), $FFFE22 (data)
                       Also accessible at $FFFFC8/$FFFFC9 (HW.SER alias)
    Port 1:           $FFFE24 (status/control), $FFFE26 (data)
    Port 2:           $FFFE30 (status/control), $FFFE32 (data)

Main ACIA ports ($FFFE20-$FFFE32) are on the upper data bus (D8-D15),
so registers appear at EVEN byte addresses with A1 as register select.

HW.SER alias ($FFFFC8-$FFFFC9) uses A0 as register select:
    $FFFFC8 (A0=0): status (read) / control (write)
    $FFFFC9 (A0=1): receive data (read) / transmit data (write)

Status Register (read):
    Bit 0: RDRF -- Receive Data Register Full
    Bit 1: TDRE -- Transmit Data Register Empty
    Bit 2: DCD  -- Data Carrier Detect (~DCD floats HIGH on AM-1200, bit=1)
    Bit 3: CTS  -- Clear To Send (active low on hardware)
    Bit 4: FE   -- Framing Error
    Bit 5: OVRN -- Receiver Overrun
    Bit 6: PE   -- Parity Error
    Bit 7: IRQ  -- Interrupt Request

Control Register (write):
    Bits 0-1: Counter Divide Select (00=div1, 01=div16, 10=div64, 11=Master Reset)
    Bits 2-4: Word Select (data bits, parity, stop bits)
    Bits 5-6: Transmit Control (RTS, TX IRQ enable)
    Bit 7:    Receive Interrupt Enable

MC6850 TX is double-buffered:
    - Transmit Data Register (TDR): holds next byte to transmit
    - Transmit Shift Register (TSR): currently shifting out on the line
    TDRE reflects whether TDR is empty (ready for new data).
    When TDR is written while TSR is active, TDR buffers the byte.
    When TSR finishes, TDR contents move to TSR automatically.
"""

from __future__ import annotations

import sys
from collections import deque

from .base import IODevice

# Approximate character transmit time in CPU cycles.
# At 9600 baud, 10 bits/char, 8 MHz CPU: ~8333 cycles.
TX_CHAR_CYCLES = 8000

# Echo delay: round-trip time for terminal echo (TX time + RX time).
# Terminal receives character after TX_CHAR_CYCLES, echoes it back,
# echo arrives after another TX_CHAR_CYCLES.
ECHO_DELAY_CYCLES = TX_CHAR_CYCLES * 2


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
        self._rx_data = [0x00, 0x00, 0x00]   # Received data register ($00 after reset)
        self._rdrf = [False, False, False]    # Receive data ready
        self._ovrn = [False, False, False]    # Receiver overrun

        # Double-buffered TX: TDR (data register) + TSR (shift register)
        self._tdre = [True, True, True]       # Transmit Data Register Empty
        self._tdr = [0x00, 0x00, 0x00]        # Transmit Data Register contents
        self._tdr_full = [False, False, False] # TDR has data waiting for TSR
        self._tsr_active = [False, False, False]  # TSR is shifting a byte out
        self._tsr_countdown = [0, 0, 0]       # Cycles until TSR shift complete

        # Terminal echo: simulates a connected terminal echoing TX data.
        # Each entry is (countdown_cycles, byte_value).
        self._echo_pending: list[deque] = [deque(), deque(), deque()]
        self._echo_enabled = [True, True, True]  # All ports echo (terminal detect)

        # RX cooldown: after an echo byte is delivered to RDR, suppress TDRE
        # for a brief period.  Models the MC6850 RX shift register finishing
        # its stop-bit clock — during this time the half-duplex line is still
        # busy receiving and TDRE cannot assert.
        self._rx_cooldown = [0, 0, 0]

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
        """Map address to (port_number, 'status'|'data') or None.

        Main ACIA ports use A1 for register select (even addresses only).
        HW.SER alias uses A0 for register select:
            $FFFFC8 (A0=0) -> status/control
            $FFFFC9 (A0=1) -> data
        """
        # Main ACIA ports: even addresses, A1 selects register
        for port, (status_addr, data_addr) in self.PORT_MAP.items():
            if address == status_addr:
                return (port, "status")
            if address == data_addr:
                return (port, "data")
        # HW.SER alias for console (port 0): A0 selects register
        if 0xFFFFC8 <= address <= 0xFFFFC9:
            if address & 1:  # A0=1 -> data register
                return (0, "data")
            else:            # A0=0 -> status register
                return (0, "status")
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

    def _start_shift(self, port: int, byte_val: int, from_tdr: bool = False) -> None:
        """Start shifting a byte out of the TSR.

        Only direct CPU writes (from_tdr=False) schedule a terminal echo.
        TDR-to-TSR transfers happen automatically as the internal pipeline
        drains; echoing these would flood the RX side during port init
        sequences where multiple bytes are queued rapidly.
        """
        self._tsr_active[port] = True
        self._tsr_countdown[port] = TX_CHAR_CYCLES
        # Schedule terminal echo only for CPU-initiated transmissions
        if self._echo_enabled[port] and not from_tdr:
            self._echo_pending[port].append((ECHO_DELAY_CYCLES, byte_val))

    # -- Public interface for feeding input ---------------------------------

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

    # -- IODevice interface -------------------------------------------------

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
            # Bit 1: TDRE
            # TDRE is suppressed during RX cooldown: after receiving a byte,
            # the stop-bit phase briefly occupies the line.  This models
            # the half-duplex terminal detect turnaround where the OS
            # polls TDRE to detect that the line was busy receiving.
            if self._tdre[port] and self._rx_cooldown[port] <= 0:
                status |= 0x02
            # Bit 2: DCD -- ~DCD not connected on AM-1200 (floats HIGH, bit=1)
            status |= 0x04
            # Bit 3: CTS -- On AM-1200, ~RTS loops back to ~CTS.
            # MC6850 CTS bit directly reflects /CTS input state:
            #   /CTS LOW → CTS bit = 0 (CTS asserted)
            #   /CTS HIGH → CTS bit = 1 (CTS not asserted)
            # With ~RTS→~CTS loopback:
            #   CR5-6 = 10 → ~RTS HIGH → /CTS HIGH → CTS bit = 1
            #   CR5-6 ≠ 10 → ~RTS LOW → /CTS LOW → CTS bit = 0
            if (self._control[port] & 0x60) == 0x40:
                status |= 0x08
            # Bit 4: FE -- Framing Error.
            # The AM-1200 port driver checks for specific status patterns
            # that include FE=1 when no valid data is in the receive register.
            # This dynamic behavior (FE=1 when RDRF=0) matches what the
            # hardware presents when the serial line has noise/no data.
            if not self._rdrf[port]:
                status |= 0x10
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
                self._tdre[port] = True
                self._tdr_full[port] = False
                self._tsr_active[port] = False
                self._tsr_countdown[port] = 0
                self._echo_pending[port].clear()
                self._rx_cooldown[port] = 0
                self._trace(f"Port {port} master reset")
            elif (old_cr & 0x03) == 0x03:
                # Coming out of reset
                self._trace(f"Port {port} control = ${value:02X}")

            # NOTE: Real MC6850 does NOT flush RX state on RX IRQ enable.
            # Enabling CR bit 7 simply allows pending RDRF to generate an
            # IRQ.  Any stale echo data must be handled by the OS (e.g.
            # reading the data register to clear RDRF before enabling IRQs).
            # Previously we flushed here, but that destroyed echo/cooldown
            # state needed for the second pass through the port-init loop.

        elif reg_type == "data":
            # Transmit data — double-buffered TX
            self._tx_output[port].append(value)
            self._trace(f"Port {port} TX: ${value:02X} ({chr(value) if 0x20 <= value < 0x7F else '?'})")
            if self.tx_callback:
                self.tx_callback(port, value)

            if not self._tsr_active[port]:
                # TSR is idle: byte goes directly to shift register
                self._start_shift(port, value)
                self._tdre[port] = True  # TDR is empty (byte went straight to TSR)
            else:
                # TSR is active: byte goes to TDR (buffer)
                self._tdr[port] = value
                self._tdr_full[port] = True
                self._tdre[port] = False  # TDR is full

    def tick(self, cycles: int) -> None:
        """Advance transmit timing and deliver pending echoes."""
        for i in range(3):
            # TSR shift register countdown
            if self._tsr_active[i]:
                self._tsr_countdown[i] -= cycles
                if self._tsr_countdown[i] <= 0:
                    self._tsr_countdown[i] = 0
                    self._tsr_active[i] = False
                    # Check if TDR has a buffered byte to shift out
                    if self._tdr_full[i]:
                        self._tdr_full[i] = False
                        self._tdre[i] = True  # TDR is now empty
                        self._start_shift(i, self._tdr[i], from_tdr=True)
                    # else: TSR idle, TDRE already reflects TDR state

            # Decrement RX cooldown
            if self._rx_cooldown[i] > 0:
                self._rx_cooldown[i] -= cycles
                if self._rx_cooldown[i] < 0:
                    self._rx_cooldown[i] = 0

            # Process pending terminal echoes
            if self._echo_pending[i]:
                new_pending: deque = deque()
                for countdown, byte_val in self._echo_pending[i]:
                    countdown -= cycles
                    if countdown <= 0:
                        # Echo arrived — MC6850 overrun behavior:
                        # If RDRF is already set, the new byte is LOST and
                        # OVRN is set (real hardware has no FIFO).
                        if self._rdrf[i]:
                            self._ovrn[i] = True
                            self._trace(f"Port {i} echo OVERRUN: ${byte_val:02X} lost")
                        else:
                            self._rx_data[i] = byte_val
                            self._rdrf[i] = True
                            # Start RX cooldown — line is busy with stop-bit
                            self._rx_cooldown[i] = TX_CHAR_CYCLES
                            self._trace(f"Port {i} echo: ${byte_val:02X}")
                    else:
                        new_pending.append((countdown, byte_val))
                self._echo_pending[i] = new_pending

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
        """ACIA provides vector 64 during IACK (IPL 1 -> vector 64)."""
        return 64

    def acknowledge_interrupt(self, level: int) -> None:
        """IACK -- ACIA interrupt is level-sensitive, cleared by reading status."""
        pass  # ACIA clears IRQ when software reads status + data registers
