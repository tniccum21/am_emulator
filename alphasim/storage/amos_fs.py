"""Minimal AMOS filesystem reader for extracting files from disk images.

Reads files from an AMOS V1.4 filesystem on a DiskImage.  Used to
pre-load system files (like SYSMSG.USA) into emulated RAM as dev spec
entries — equivalent to what MONGEN sets up at system generation time.

AMOS filesystem layout:
    MFD (block 1):  8-byte entries — PPN(word) + UFD_block(word) + pad(4)
    UFD blocks:     2-byte link header, then 12-byte file entries
    File entry:     name1(w) name2(w) ext(w) attr(w) size(w) start_block(w)
    Data blocks:    2-byte link (next block, 0=end) + 510 bytes data

Block addressing:  AMOS block N → DiskImage LBA N+1
Word byte order:   PDP-11 little-endian (low byte first on disk)
"""

from __future__ import annotations

from .disk_image import DiskImage


def _read_word_le(data: bytes, off: int) -> int:
    """Read a PDP-11 little-endian 16-bit word from raw disk bytes."""
    return (data[off + 1] << 8) | data[off]


def _rad50_encode(s: str) -> int:
    """Encode a 3-character string to RAD50."""
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
    result = 0
    for ch in s.upper().ljust(3)[:3]:
        idx = chars.find(ch)
        if idx < 0:
            idx = 0
        result = result * 40 + idx
    return result


def read_file(disk: DiskImage, ppn: tuple[int, int],
              filename: str, extension: str) -> bytes | None:
    """Read a file from the AMOS filesystem on a disk image.

    Args:
        disk: DiskImage to read from
        ppn: Account number as (project, programmer), e.g. (1, 4)
        filename: 6-character filename, e.g. "SYSMSG"
        extension: 3-character extension, e.g. "USA"

    Returns:
        Raw file data bytes (block headers stripped), or None if not found.
    """
    # Encode search name
    name1 = _rad50_encode(filename[:3])
    name2 = _rad50_encode(filename[3:6]) if len(filename) > 3 else 0
    ext = _rad50_encode(extension[:3])
    target_ppn = (ppn[0] << 8) | ppn[1]

    # Read MFD at AMOS block 1 (LBA 2)
    mfd = disk.read_sector(2)
    if mfd is None:
        return None

    # Find UFD for the requested account
    ufd_block = 0
    for off in range(0, 504, 8):
        entry_ppn = _read_word_le(mfd, off)
        if entry_ppn == 0:
            break
        if entry_ppn == target_ppn:
            ufd_block = _read_word_le(mfd, off + 2)
            break

    if ufd_block == 0:
        return None

    # Walk UFD block chain to find the file
    start_block = 0
    block = ufd_block
    for _ in range(200):  # safety limit
        sector = disk.read_sector(block + 1)
        if sector is None:
            break
        link = _read_word_le(sector, 0)

        # Scan 12-byte entries starting at offset 2
        off = 2
        while off + 12 <= 512:
            w0 = _read_word_le(sector, off)
            w1 = _read_word_le(sector, off + 2)
            w2 = _read_word_le(sector, off + 4)
            if w0 == 0 and w1 == 0:
                break
            if w0 == name1 and w1 == name2 and w2 == ext:
                start_block = _read_word_le(sector, off + 10)
                break
            off += 12

        if start_block:
            break
        if link == 0:
            break
        block = link

    if start_block == 0:
        return None

    # Follow file data block chain
    data = bytearray()
    block = start_block
    for _ in range(2000):  # safety limit
        sector = disk.read_sector(block + 1)
        if sector is None:
            break
        link = _read_word_le(sector, 0)
        data.extend(sector[2:])  # 510 data bytes
        if link == 0:
            break
        block = link

    return bytes(data)
