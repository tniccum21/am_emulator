# Resume Note

Last updated: 2026-03-28
Branch: `feature/native-boot-milestones`

## Current State — AMOS BOOTS TO COMMAND PROMPT, DISK WRITES WORK

The emulator boots AMOS/L 1.3D(165)-1 from the real disk image to an
interactive command prompt. Terminal I/O, disk reads, and disk writes
all work natively through pure hardware emulation. No Python bypasses.

```bash
# Run it (use pypy3 for ~5x speed):
python3 main.py          # interactive terminal
printf 'VER\r' | python3 main.py   # piped commands
```

### What works
- Full boot: ROM → monitor → AMOSL.INI → command prompt
- Terminal output: AMOS banner, license, all INI command echo
- Interactive keyboard input (Ctrl-C passed to AMOS, double Ctrl-C exits)
- Disk reads: hundreds of SCSI reads via DMA
- Disk writes: MAKE command creates files on disk
- VER, MAKE, and other built-in commands

### What doesn't work yet
- **COPY command**: `?Illegal instruction at 175274`
- **1.4 disk image**: fails before disk mount (different monitor, needs investigation)

## Current Bug: COPY illegal instruction at $175274

### Symptoms
- `COPY TEST2.TXT=TEST.TXT` → `?Illegal instruction at 175274`
- $175274 contains $5252 (RAM fill pattern "RR") — not executable code
- COPY.LIT is never loaded to that address

### What we know
- MAKE works fine (creates files, writes to disk via SCSI PIO + $80 IRQ)
- COPY.LIT should be loaded from disk by COMINT's program loader
- SCSI DMA reads go to the disk cache buffer at $038xxx
- The OS copies data from cache to the program area via CPU instructions
- The program area at $175000+ still has the $5252 fill pattern
- This means the OS's copy-from-cache-to-program step fails

### Theory
The DMA byte ordering may be causing byte-swapped data in the cache.
When the OS copies cache data to the program area using word/long
moves, the byte-swapped data becomes garbled code. The boot works
because the ROM/monitor SCSI path (SASI at $FFFFE0) uses PIO and
handles byte ordering differently than the SCSI DMA path ($FFFFC8).

Compare memory vs disk after DMA read:
```
Disk (raw):     C8 00 FF FF 00 02 74 00
Memory (words): $00C8 $FFFF $0200 $0074
Expected:       $C800 $FFFF $0002 $7400
```
Every word has bytes swapped. BUT: fixing this would break the boot,
which already works correctly. The AMOSL.INI loading and all other
DMA reads work — so the byte order must be correct for THOSE reads.

The mystery: why do boot reads work with this byte order but COPY
doesn't? Possible answers:
1. Boot reads use a code path that compensates for the swap
2. COPY uses a different DMA mechanism
3. The $175274 address itself is wrong (JCB partition misconfiguration)

### Next steps to investigate
1. Trace how COMINT loads .LIT programs — where does it expect the
   code and what address does it JMP to?
2. Check if the cache→program copy uses word moves (which would
   "unswap" the PDP-11 byte order) or byte moves
3. Compare the SASI PIO path (ROM boot) vs SCSI DMA path (OS reads)
   to understand why both work despite apparent byte swap
4. Check JCB partition settings — is $175274 the correct program base?

## Key Architecture (1.3 image ONLY — don't mix with 1.4)

### TCB field layout (AMOS 1.3)
```
TCB+$00 = T.STS (status word, bit 7 = OIP)
TCB+$02 = IDV module pointer (AM1000.IDV at $85FC)
TCB+$06 = T.IHW ($FFFFFE20 = ACIA port 0)
TCB+$0E = TDV module pointer (WYSE.TDV at $89F4)
TCB+$40 = T.JLK (JCB link at $7038)
TCB+$62 = T.INC (0 = use built-in TRMICP)
TCB+$66 = T.OTC (0 = use built-in TRMOCP)
```

### AM1000.IDV dispatch (from am1000.m68 source)
```
+$00: BR CHROUT  → enables TX on ACIA (writes $B5 to control)
+$02: BR INIT    → sets baud, resets ACIA, installs ISR vector
ISR: RDRF→INPR, DCD→INFI, TDRE→OUTPR
OUTPR: TRMOCP → send char or clear OIP
```

### ACIA interrupt levels
- Port 0: level 2 (autovector 26, $068)
- Ports 1/2: level 3 (autovector 27, $06C)

### SCSI bus status register bit 0
- Set during DATA_IN, STATUS, MESSAGE_IN (ready polling)
- Clear during COMMAND, DATA_OUT (phase detection needs exact match)

### SCSI WRITE flow (PIO, not DMA)
- Driver sends WRITE CDB → phase = DATA_OUT
- Driver polls for DATA_OUT ($06), then writes 512 bytes via PIO
- PIO auto-completes → phase = STATUS
- Driver writes $80 → schedules completion IRQ
- ISR fires → IOWAIT wakes → job resumes

## ACIA fixes applied (all on 1.3 image)
1. DCD=0 (carrier present) — AM1000 ISR checks DCD before TDRE
2. TX IRQ latch preserved during RX data reads
3. RX cooldown TDRE suppression removed
4. Per-port interrupt levels (port 0=L2, ports 1/2=L3)
5. Echo disabled after OS takes over

## Files
- `main.py` — interactive native boot runner
- `am1000.m68` — AM1000.IDV source (for 1.3 image)
- `am130.m68` — AM130.IDV source (for 1.4 image, DON'T mix)
- `alphasim/devices/acia6850.py` — ACIA with all fixes
- `alphasim/devices/scsi_bus.py` — SCSI bus with write support

## Verification
```bash
# Tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/ -k 'not test_native_boot_reads_amosl_ini'
# Expected: 60 passed, 1 skipped, 1 deselected

# Native boot
python3 main.py
# Expected: boots to command prompt, VER works, MAKE works

# COPY test (currently fails)
# At AMOS prompt: COPY TEST2.TXT=TEST.TXT
# Expected: ?Illegal instruction at 175274
```
