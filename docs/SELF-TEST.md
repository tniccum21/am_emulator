# ROM Self-Test in AlphaSim

The AM-178 boot ROM includes a built-in hardware diagnostic ("System Self-Test") that tests memory, timer, serial ports, disk controllers, and other peripherals. AlphaSim can run this self-test to verify emulation correctness.

Important status note:

- The self-test serial path is now better understood than when this document was
  first written.
- The ROM self-test `LED=5B` baud-detect loop uses the main serial-port block
  at `0xFFFE20`, `0xFFFE24`, and `0xFFFE30`, plus a separate setup register at
  `0xFFFE28`.
- It does not use the low-memory monitor path at `0xFFFFC8/0xFFFFC9`.
- See `docs/BOOT-SERIAL-FINDINGS.md` for the detailed note and current native
  boot findings.

## Running the Self-Test

### Using the CLI flag

```bash
python3 -m alphasim --self-test --max-instructions 250000000
```

The `--self-test` flag sets DIP switch bit 5, which the boot ROM checks at $800030 (`BTST #5,D6`). When set, the ROM branches to the diagnostic code instead of the normal boot path.

### Using a custom DIP value

```bash
python3 -m alphasim --dip 0x2A --max-instructions 250000000
```

DIP value `0x2A` = `0x0A` (SCSI controller) | `0x20` (bit 5 = self-test mode).

## Manual Output vs Validated Behavior

The Alpha Micro self-test manuals show a larger user-visible output stream after
serial setup succeeds. AlphaSim does not yet emulate the full serial/peripheral
path needed to reproduce all of that output.

Historically expected manual output begins like this:

```
====================
| System Self-Test |
====================

System configuration:
 Processor - 68010
 Memory detected - 4096K bytes
 Number of serial ports - 3
 Floppy interface

Verify the proper configuration was detected ..

TESTING MEMORY ..
MEMORY TEST passed - 4096K bytes

TIMER TEST passed
```

What is currently validated in AlphaSim is narrower:

- self-test reaches `LED=5B`
- the ROM scans the three main serial-port bases
- a raw space (`0x20`) on any one of those ports drives the ROM to `LED=B5`
- the first validated output line is `300 baud detected\r\n`
- that line is written only to the matched port

The self-test loops continuously. After the timer test passes, it restarts from
the beginning. Tests beyond the timer test and first serial handshake still
require additional hardware work and may fail or hang.

## Self-Test Flow

### Boot into self-test mode

1. Normal reset: CPU fetches SSP=$00032400, PC=$800018 from ROM
2. Hardware init at $800018: LED=6, read DIP switch, clear vectors
3. `BTST #5,D6` — DIP bit 5 set → branch to self-test code (skip normal boot)
4. Self-test copies ROM to RAM at $032400 and runs from there

### LED codes

| LED Value | Meaning                    |
|-----------|----------------------------|
| 6         | Hardware init              |
| 128       | Self-test entered          |
| 130       | Configuration detection    |
| 91 (`5B`) | Serial baud detect wait    |
| 181 (`B5`)| Serial baud matched        |
| 131-136   | Memory test setup          |
| 140       | Memory test in progress    |
| 144-150   | Memory test phases         |
| 152       | Timer test                 |
| 160       | Serial port test           |
| 184-185   | Disk/peripheral tests      |

### Memory Test

The ROM walks all of RAM writing and reading test patterns to verify every byte. With 4MB RAM this takes approximately 125M emulated instructions. The test is thorough — it detects size, tests bit patterns, and reports the result.

### Timer Test

The timer test verifies the MC6840 PTM can generate interrupts. The ROM:

1. **Installs handler** at vector 30 ($078) — autovector for IPL level 6
2. **Configures Timer 1**: latch=$4000 (16384 ticks = ~16ms at 1 MHz), internal clock
3. **Configures Timer 3**: latch=$0009 (9 ticks), external clock (should NOT fire)
4. **Enables timers**: CR1 bit 7 = 0 (run mode)
5. **Polls flag** at workspace+$1A with a DBne loop (30000-iteration timeout)

The interrupt handler at $8013C4:
```
MOVE.W  #$2700,SR           ; Mask all interrupts
MOVE.B  #$01,2(A1)          ; Write CR register
MOVE.B  #$01,(A1)           ; Write CR register
MOVE.B  #$FF,$001A(A5)      ; Set polling flag = $FF
RTE                          ; Return from exception
```

The handler sets the polling flag to $FF but does **not** clear the timer IRQ flag (no two-step read). This is intentional — the edge-triggered interrupt fires once, the handler sets the flag, and the polling loop detects it.

- If the polling flag is set before timeout → **PASS**
- If the DBne loop expires (30000 iterations) → **FAIL**

### Key MC6840 behaviors exercised by the timer test

| Behavior | What the test verifies |
|----------|----------------------|
| IPL level 6 | Handler installed at vector 30 ($078 = autovector level 6) |
| Autovector mode | MC6840 uses VPA, not vectored interrupts |
| Internal clock gating | Timer 1 (internal) fires; Timer 3 (external) does not |
| Edge-triggered IRQ | Single interrupt fires; no re-entry after RTE |
| CR1 bit 7 = run/hold | Setting bit 7 to 0 enables counting |
| Counter loading | LSB write commits MSB+LSB to counter and starts counting |

## Validated Serial Behavior

Current integration tests establish the following:

1. During the first `LED=5B` window, the ROM touches:
   - `0xFFFE20`, `0xFFFE22`
   - `0xFFFE24`, `0xFFFE26`
   - `0xFFFE28`
   - `0xFFFE30`, `0xFFFE32`

2. During that same window, it does not touch:
   - `0xFFFFC8`
   - `0xFFFFC9`

3. Before polling the three serial bases, the ROM writes this setup sequence to
   `0xFFFE28`:
   - `0x15`
   - `0x25`
   - `0x45`
   - `0x85`

4. If one selected port shows:
   - status bit 0 set at `base`
   - data byte `0x20` at `base+2`

   then the ROM reaches `LED=B5`.

5. After that match, the first validated output line is:

   `300 baud detected\r\n`

6. That line is written only to the matched port's data register.

These results come from raw bus-level integration tests, not from trusting the
current `acia6850.py` behavior by itself.

## Emulator Bugs Found via Self-Test

The self-test uncovered four MC6840 emulation bugs (see `bugs.md` #16-#19):

1. **IPL level wrong** (#16): Was 3, should be 6. Vector mode was vectored (66), should be autovector (0).
2. **Clock source not gated** (#17): `tick()` decremented all timers regardless of clock source. Timer 3 (external clock, latch=9) fired in ~72 CPU cycles before Timer 1 could fire.
3. **Level-triggered interrupt** (#18): Once an IRQ flag was set, the interrupt fired continuously, trapping the CPU in an infinite handler-RTE-interrupt loop. Fixed to edge-triggered (pending on 0→1 transition, cleared by IACK).
4. **CR3 bit 7 misinterpreted** (#19): Was treated as timer reset; actually T3 prescale control (divide by 8).

## Reference

- Alpha Micro self-test manual: `docs/DIAGNOSTICS/DSM-00156-02-A00-System Self Test User's Guide-Ver2.0+.PDF`
- ROM disassembly spec: `docs/AMOS_DIS_BOOTROM_SPEC.md`
- Timer implementation: `alphasim/devices/timer6840.py`
- Detailed boot/serial note: `docs/BOOT-SERIAL-FINDINGS.md`
- Bug history: project memory `bugs.md`
