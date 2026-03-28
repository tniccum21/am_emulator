"""Integration target: native system wiring exposes the inferred $FFFE40 clock bank."""

from __future__ import annotations

import pytest

from .boot_helpers import roms_available
from .boot_helpers import build_native_boot_system


@pytest.mark.skipif(
    not roms_available(),
    reason="ROM files not present",
)
def test_build_system_maps_direct_clock_bank():
    _cpu, bus, _led, _acia, _sasi = build_native_boot_system()

    bus.write_byte(0xFFFE5A, 0x03)

    assert bus.read_byte(0xFFFE5A) == 0x01
    assert 0 <= bus.read_byte(0xFFFE58) <= 9
