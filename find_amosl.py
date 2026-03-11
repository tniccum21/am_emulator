#!/usr/bin/env python3
"""Find AMOSL.MON on AMOS_1-3_Boot_OS.img using Alpha_Disk_Lib."""
import sys
sys.path.insert(0, "../Alpha-Python")
from lib.Alpha_Disk_Lib import AlphaDisk

disk = AlphaDisk("images/AMOS_1-3_Boot_OS.img")
dev = disk.get_logical_device(0)

ufd = dev.read_user_file_directory((1, 4))
print(f"{ufd}")
print(f"Blocks in chain: {[b.block_number for b in ufd.blocks]}")

for entry in ufd.get_active_entries():
    if "AMOSL" in entry.filename and "MON" in entry.extension:
        print(f"\n*** FOUND: {entry}")
        print(f"    first_block={entry.first_block}, blocks={entry.block_count}, size={entry.file_size}")
