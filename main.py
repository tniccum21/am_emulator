#!/usr/bin/env python3
"""Native boot runner with live terminal output.

Boots AMOS from the real disk image and displays terminal output
in real time. Supports interactive keyboard input.

WYSE escape sequences are stripped for readability on modern terminals.
"""
import fcntl
import os
import select
import sys
import termios
import tty

sys.path.insert(0, os.path.dirname(__file__))
from tests.integration.boot_helpers import build_native_boot_system, find_boot_image


def main():
    img = find_boot_image()
    if img is None:
        print("No boot image found.", file=sys.stderr)
        sys.exit(1)

    interactive = sys.stdin.isatty()
    if interactive:
        print(f"Booting from {img.name}... (Ctrl-C twice to quit)", file=sys.stderr)
    else:
        print(f"Booting from {img.name}...", file=sys.stderr)

    cpu, bus, led, acia, sasi = build_native_boot_system(img)

    # Suppress LED and SCSI debug output
    led.write = lambda address, size, value: None  # noqa
    for _, _, dev in bus._devices:
        if hasattr(dev, '_read_count'):
            dev._read_count = 999

    # Disable ACIA TX→RX echo. The echo mechanism exists for ROM
    # terminal-detect, but once the OS is running TRMSER handles
    # echo via software. Hardware echo causes double characters.
    acia._echo_enabled = [False, False, False]

    # Wire TX output to stdout
    tx_count = 0
    in_esc = False

    def tx_cb(port, value):
        nonlocal tx_count, in_esc
        if port != 0:
            return
        tx_count += 1
        ch = value & 0x7F

        # Simple WYSE escape filter
        if ch == 0x1B:
            in_esc = True
            return
        if in_esc:
            if chr(ch).isalpha() or ch in (0x02, 0x03, 0x07):
                in_esc = False
            return
        if ch == 0x00:
            return

        sys.stdout.buffer.write(bytes([ch]))
        sys.stdout.buffer.flush()

    acia.tx_callback = tx_cb

    # Set up input handling
    input_data = b""
    old_settings = None

    if interactive:
        # Put terminal in raw mode for character-at-a-time input
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())
        # Make stdin non-blocking
        flags = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)
    else:
        input_data = sys.stdin.buffer.read()

    cpu.reset()

    max_instructions = 500_000_000
    feed_at = 5_500_000
    check_input_interval = 5000  # check for keyboard input every N instructions
    last_ctrlc = 0  # instruction count of last Ctrl-C

    try:
        for i in range(1, max_instructions + 1):
            cycles = cpu.step()
            bus.tick(cycles)

            # Feed piped input after terminal is set up
            if not interactive and i == feed_at and input_data:
                acia.send_to_port(0, input_data)

            # Poll for interactive keyboard input
            if interactive and i % check_input_interval == 0:
                try:
                    data = sys.stdin.buffer.read(64)
                    if data:
                        for b in data:
                            if b == 3:  # Ctrl-C
                                # Double Ctrl-C (within 500K instructions) = exit
                                if i - last_ctrlc < 500_000:
                                    raise KeyboardInterrupt
                                last_ctrlc = i
                                # Single Ctrl-C → send to AMOS
                                acia.send_to_port(0, b"\x03")
                                continue
                            byte = b & 0x7F
                            acia.send_to_port(0, bytes([byte]))
                except (BlockingIOError, IOError):
                    pass

            if cpu.halted:
                print(f"\nCPU halted at i={i}", file=sys.stderr)
                break
    except KeyboardInterrupt:
        pass
    finally:
        if old_settings is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    print(f"\n--- {i} instructions, {tx_count} TX bytes ---", file=sys.stderr)


if __name__ == "__main__":
    main()
