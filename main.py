#\!/usr/bin/env python3
"""Native boot runner with live terminal output.

Boots AMOS from the real disk image and displays terminal output
in real time. WYSE escape sequences are stripped for readability
on modern terminals.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from tests.integration.boot_helpers import build_native_boot_system, find_boot_image


def main():
    img = find_boot_image()
    if img is None:
        print("No boot image found.", file=sys.stderr)
        sys.exit(1)

    print(f"Booting from {img.name}...", file=sys.stderr)
    cpu, bus, led, acia, sasi = build_native_boot_system(img)

    # Suppress LED and SCSI debug output
    led.write = lambda address, size, value: None  # noqa
    for _, _, dev in bus._devices:
        if hasattr(dev, '_read_count'):
            dev._read_count = 999

    # Wire TX output to stdout
    tx_count = 0
    in_esc = False  # track WYSE escape sequences

    def tx_cb(port, value):
        nonlocal tx_count, in_esc
        if port != 0:
            return
        tx_count += 1
        ch = value & 0x7F

        # Simple WYSE escape filter: ESC starts a sequence,
        # ended by an alpha character or certain controls.
        if ch == 0x1B:
            in_esc = True
            return
        if in_esc:
            # Most WYSE sequences are ESC + one-or-two chars
            # Pass through cursor positioning and common ones
            if chr(ch).isalpha() or ch in (0x02, 0x03, 0x07):
                in_esc = False
            return

        if ch == 0x00:
            return  # skip nulls

        b = bytes([ch])
        sys.stdout.buffer.write(b)
        sys.stdout.buffer.flush()

    acia.tx_callback = tx_cb

    # Feed stdin to RX if available
    input_data = b""
    if not sys.stdin.isatty():
        input_data = sys.stdin.buffer.read()

    cpu.reset()

    max_instructions = 100_000_000
    feed_at = 5_500_000  # feed input after TRMDEF completes

    try:
        for i in range(1, max_instructions + 1):
            cycles = cpu.step()
            bus.tick(cycles)

            # Feed piped input after terminal is set up
            if i == feed_at and input_data:
                acia.send_to_port(0, input_data)
                print(
                    f"\n[fed {len(input_data)} bytes to port 0]",
                    file=sys.stderr,
                )

            if cpu.halted:
                print(f"\nCPU halted at i={i}", file=sys.stderr)
                break
    except KeyboardInterrupt:
        pass

    print(f"\n--- {i} instructions, {tx_count} TX bytes ---", file=sys.stderr)


if __name__ == "__main__":
    main()
