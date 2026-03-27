# AlphaSim AM-1200 Emulator — Project Status

Last updated: 2026-03-26

## Overview

MC68010 emulator for the Alpha Microsystems AM-1200 computer. Boots AMOS operating system from ROM + SCSI disk image.

## Phase Summary

- **Phase 1**: COMPLETE — CPU core, ROM boot, all 68010 instructions, LED=6
- **Phase 2**: COMPLETE — Disk bootstrap, AMOS OS loaded from SCSI disk, LED=14
- **Phase 3**: COMPLETE — Timer ISR crash fixed (MC6840 bugs #8-19), OS idle loop stable
- **Phase 4**: IN PROGRESS — Terminal I/O, command execution, native boot

## Phase 4 — Two Parallel Tracks

### Python Bypass (patch_driver_v7.py) — WORKING
Fully functional but cheats: Python intercepts LINE-A system calls and does the work instead of the OS.

- 11 commands working: VER DAT TIM DIR TYPE MEM DEV FRE HEL BYE LOG
- Synchronous I/O bypass ($A03C/$A03E intercepts)
- Python command dispatch (JMP(A5) at $4BE6)
- AMOSL.INI injection via $A008 intercept
- SYSMSG.USA decoded from disk (Python $A0EC intercept)

```bash
# Test:
printf 'VER\nDIR\nBYE\n' | python3 patch_driver_v7.py 2>/dev/null
```

### Native Boot (main.py) — MAJOR PROGRESS

The goal is for the OS to boot and run commands using its own code, with only hardware emulated in Python.

#### What ACTUALLY works (real hardware emulation)
1. **CPU core**: All 68010 instructions execute correctly
2. **ROM boot**: Loads AMOS from disk via real SCSI emulation (WD1002 + SCSI bus)
3. **Hardware devices**: Timer (MC6840 + PIT 8253), LED, DIP switch, ACIA (6850), SASI/SCSI, RTC (MSM5832)
4. **OS loads and runs**: Scheduler runs, LINE-A dispatch works, timer ISR fires
5. **SCSI bus interface**: Single-stage selection, COMMAND/DATA/STATUS/MESSAGE phases, DMA with level-5 interrupt completion
6. **OS disk I/O**: 207 native SCSI reads including AMOSL.INI at LBA 3335
7. **User-mode context**: JCB+$7C (USP) set to $7AC2, MOVE A6,USP at $003740 executes
8. **Scheduler dispatch**: WAKE/SCHED bit-13 cycle, timer-driven reschedule, IOGET/TIMSET/IOWAIT chain

#### Current Native Frontier

The OS now completes its full initialization sequence on the real boot image (`AMOS_1-3_Boot_OS.img`):

1. ROM boot loads monitor from disk (78 SASI reads, LBA 0-3326)
2. Monitor creates JCB at $7038, clears memory, sets up job fields
3. RTC initialization via LINE-A $A07A
4. **Disk mount via LINE-A $A0AA** — reads 207 blocks via SCSI bus including:
   - TEST UNIT READY → GOOD
   - READ(10) LBA=1 (disk label)
   - MFD reads (LBA 79, 340, 634, 868, 1227)
   - **AMOSL.INI at LBA 3335** ← first native read of the INI file!
5. $A0AA returns → code reaches $0036F4
6. Skip path at $003720 (JCB+$18=0) → **$003740: MOVE A6,USP**
7. USP = $7AC2, JCB+$7C = $7AC2

#### What COMINT does after dispatch

The scheduler DOES dispatch to user mode. COMINT runs at $3E8xxx and
processes AMOSL.INI commands:

- `:T` — trace mode enabled
- `JOBS 5` → 8 JOBBLD calls (5 jobs + JOBALC + system overhead)
- `JOBALC TOM,TOM2` → job name allocation
- `TRMDEF TRM1,AM1000=0:19200,WYSE,...` → ACIA port 0 configured:
  - $03 (reset) → $95 (RX IRQ, 8N1) → $B5 (TX+RX IRQ)
  - TRMATT ($A038) called, allocates terminal channel $182E
- 19 FIND calls, 35 FETCH calls, 66 TTYOUT calls
- AM1000.IDV loaded from DSK0:[1,6] (LBA 1275)
- WYSE.TDV loaded from DSK0:[1,6] (LBA 3329)
- TCB created at $856E with T.IHW=$FFFE20, T.JLK=$7038
- Console identification test at $3E878C passes

**Current blocker**: The AM1000 interface driver never calls the COMINT
monitor call to register T.INC (input char routine) and T.OTC (output
char routine) in the TCB. Without these interrupt-level callbacks:
- Received ACIA characters are discarded (handler at $88D4 skips on
  T.INC=0)
- Output chain can't start (no T.OTC for TINIT to invoke)
- No terminal attachment occurs → JOBTRM stays zero
- VER's TTY call puts the job into terminal output wait (Tw) forever

The job enters Tw at i=5,265,340 (JOBSTS bit 2) and never wakes up.

#### Key Hardware Fixes (2026-03-25/26)

1. **Vector-8 frame layout**: Restored normal 68000 [SR][PC] for privilege violations (was incorrectly reversed for vectors 8/9)
2. **RTE supervisor preservation**: Extended to helpers at $003DDA/$003DE2
3. **SCSI bus selection**: Changed from two-stage to single-stage handshake — the 68010 monitor sends $00/$01/$11 once and expects COMMAND phase immediately
4. **SCSI DMA interrupt level**: Changed from level 2 (ROM stub at $B9C) to level 5 (monitor's completion ISR at $006C18)

These two SCSI fixes unblocked the entire native I/O chain:
- Before: zero OS-level SCSI reads after ROM boot
- After: 207 OS-level SCSI reads, AMOSL.INI loaded, user context initialized

## Test Baseline

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/ -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'
# Expected: 60 passed, 1 skipped, 1 deselected
```
