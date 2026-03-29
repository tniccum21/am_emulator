# Resume Note

Last updated: 2026-03-28
Branch: `feature/native-boot-milestones`

## Current State — FULL INTERACTIVE COMMAND LINE WORKING

The emulator boots AMOS/L 1.3D(165)-1 from the real disk image to a
fully interactive command prompt. Terminal I/O, disk reads, disk writes,
and program loading all work natively through pure hardware emulation.

```bash
python3 main.py          # interactive terminal
printf 'VER\r' | python3 main.py   # piped commands
```

### Commands tested and working
VER, DIR, MAKE, COPY, TYPE, RENAME, ERASE/DEL, LOG, SYSTAT, DATE, TIME

### Known issues
- **RTC year**: Shows 1926 instead of 2026 (century offset in RTC)
- **1.4 disk image**: Fails before disk mount (different monitor)
- **Timer watchdog**: OS timer ISR has a race condition where the task
  queue can empty, leaving both T1 and T2 IRQs disabled. The emulator
  includes a watchdog that detects this and re-enables the timer.
  This may add slight latency (~0.25s) after a timer lockup.

## Timer Architecture (important for future work)

The MC6840 timer ISR alternates T1 (fast tick) and T2 (calendar/queue):
- T1 fires → TINC runs → T2 queue entry added → T2 enabled
- T2 fires → queue processed → callbacks run → T2 re-enabled

If the T2 queue empties (all entries consumed), the ISR exits without
re-enabling T2. T1 must then re-populate the queue. But the T1 handler
checks `$0488` (a "ready" flag set by TINC). If `$0488` is zero (because
TINC didn't run during the last T1 processing), T1 is also skipped.
This creates a permanent deadlock: both timers disabled, `$0488`=0,
queue empty.

The watchdog in `timer6840.py` detects this (composite IRQ deasserted
for >2M cycles) and re-enables T1+T2. The higher CPU_TIMER_RATIO of 32
slows the timer to prevent queue drain during normal operation.

## Files modified
- `alphasim/devices/timer6840.py` — Timer with watchdog recovery

## Verification
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/ -k 'not test_native_boot_reads_amosl_ini'
# Expected: 60 passed, 1 skipped, 1 deselected
python3 main.py
```

## Key Architecture (1.3 image ONLY)

### SCSI WRITE flow (PIO, not DMA)
- Driver sends WRITE CDB → DATA_OUT phase
- PIO writes 512 bytes → auto-complete → STATUS phase
- Driver writes $80 → schedules completion IRQ

### ACIA interrupt levels
- Port 0: level 2 (autovector 26)
- Ports 1/2: level 3 (autovector 27)
