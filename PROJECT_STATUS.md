# AlphaSim AM-1200 Emulator — Project Status

Last updated: 2026-03-27

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

### Native Boot (main.py) — TERMINAL OUTPUT WORKING

The OS boots and produces terminal output using its own code with only
hardware emulated in Python. No LINE-A intercepts, no memory patches,
no OS structure writes — the CPU executes native AMOS code and the
ACIA hardware emulation delivers characters to the terminal.

```bash
# Run native boot with live terminal output:
python3 main.py
```

#### What works natively (2026-03-27)

1. **CPU core**: All 68010 instructions execute correctly
2. **ROM boot**: Loads AMOS from disk via real SCSI emulation (WD1002 + SCSI bus)
3. **Hardware devices**: Timer (MC6840 + PIT 8253), LED, DIP switch, ACIA (6850), SASI/SCSI, RTC (MSM5832)
4. **OS loads and runs**: Scheduler, LINE-A dispatch, timer ISR, async disk I/O
5. **SCSI bus interface**: Single-stage selection, COMMAND/DATA/STATUS/MESSAGE phases, DMA with level-5 interrupt completion
6. **OS disk I/O**: Hundreds of native SCSI reads including AMOSL.INI
7. **AMOSL.INI processing**: :T, JOBS 5, JOBALC, TRMDEF (two terminals)
8. **Terminal drivers loaded**: AM1000.IDV (serial port) + WYSE.TDV (display) from disk
9. **ACIA interrupt-driven output**: TX interrupts dequeue characters from TRMSER output buffer, write to ACIA data register, trigger next interrupt
10. **Terminal output**: AMOS boot banner, license agreement, WYSE escape sequences — 2322+ bytes of real terminal data

#### Native boot output

The OS displays the Alpha Microsystems logo (block graphics via WYSE
escape sequences), the "System not available" diagnostic, and the
full AMOS software license agreement. It then continues processing
AMOSL.INI (second TRMDEF for port 2).

#### ACIA fixes that enabled terminal output (2026-03-27)

Three fixes to the MC6850 ACIA emulation unblocked the entire terminal
output chain:

1. **DCD=0 (carrier present)**: The AM1000.IDV ISR (from source
   `am1000.m68`) checks DCD before TDRE in GINTRP. With DCD=1 (the
   previous default), the ISR treated every TX interrupt as a "false
   interrupt" and skipped output processing entirely. On a real
   AM-1000 with directly-connected terminals, ~DCD is tied LOW
   (carrier always present), so DCD bit = 0 is correct.

2. **TX IRQ latch preserved during RX reads**: The INPR handler reads
   the ACIA data register to get received characters. This read must
   NOT clear the TX IRQ pending latch, or the output chain dies after
   processing echo bytes. The latch is only cleared on "dismiss"
   reads (RDRF was not set — the INFI handler at $0088CA).

3. **Remove RX cooldown TDRE suppression**: A timing mechanism
   designed for terminal-detect echo suppressed TDRE in the status
   register after receiving echo bytes. This caused the ISR to see
   status=$80 (no TDRE), fall through to INFI, and kill the TX chain.
   TDRE must always reflect the true transmit-ready state.

#### Earlier key fixes

1. **SCSI bus selection**: Single-stage handshake (monitor sends $00/$01/$11 once)
2. **SCSI DMA interrupt level**: Level 5 (monitor's ISR at $006C18, not ROM stub)
3. **Vector-8 frame layout**: Normal [SR][PC] for privilege violations
4. **ACIA interrupt level**: Level 2, autovectored (vector 26)
5. **Exception frame for vector 26**: Normal [SR][PC] (not reversed)

#### Current frontier

- Terminal output works through the AMOS boot banner and license text
- Processing AMOSL.INI: reaches second TRMDEF (TRM2, port 2)
- Next: VER, PARITY, DEVTBL, BITMAP, SYSTEM commands
- No interactive input yet (RDRF injection untested with new ACIA)
- Need more instructions (>50M) to complete full INI processing

## Test Baseline

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/ -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'
# Expected: 60 passed, 1 skipped, 1 deselected
```
