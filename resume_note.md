# Resume Note

Last updated: 2026-03-29
Branch: `feature/native-boot-milestones`

## Current State — Interactive Commands Work, .LIT Loading Broken

The emulator boots AMOS/L 1.3D(165)-1 to an interactive prompt.
Keyboard input works, built-in commands work, but loading .LIT
programs (RENAME, COPY, etc.) fails with "?Illegal instruction at
175274" when typed AFTER boot completes.

```bash
python3 main.py          # interactive terminal
printf 'VER\r' | python3 main.py   # piped commands
```

### What works
- Boot to prompt, keyboard input, command echo
- VER, DIR, MAKE, DATE, TIME, LOG, SYSTAT (built-in or small .LIT)
- .LIT loading during INI (before DCACHE.SYS loads)

### What's broken: .LIT program loading after boot

**Symptom**: `RENAME QQQ.TXT=ZZZ.TXT` → `?Illegal instruction at 175274`

**Root cause analysis** (2026-03-29):
- COMINT finds the file in the directory (3 SCSI reads for dir lookup)
- The SCSI reads complete (data reaches DCACHE cache at $038xxx)
- But the copy from DCACHE cache → program area ($175xxx) NEVER happens
- Zero bytes written to $170000-$180000 range
- The program area retains $5252 fill pattern

**Key discovery**: During INI (before DCACHE.SYS loads), .LIT programs
load via direct DMA to low memory ($00Dxxx). After DCACHE loads, disk
reads are intercepted by the cache layer. DCACHE has the file data in
its buffer but the delivery mechanism to the program area fails.

**What we've ruled out**:
- NOT a timer issue — timer is alive (1245 ISRs during RENAME, interlock
  prevents both T1/T2 from being disabled simultaneously)
- NOT a SCSI issue — all 3 SCSI reads (directory + file data?) complete,
  level-5 completion ISRs fire (3 vector-29 interrupts)
- NOT a byte-swap issue — INI .LIT loading works with the same DMA path

**Next steps to investigate**:
1. Compare SCSI LBAs read during EARLY (5.5M, works) vs LATE (20M, fails)
   to confirm whether the 3 late reads are directory or file data
2. Trace what DCACHE does with cached file data — does it try to copy?
   Where does the copy go? (hook bus._write_byte_physical more broadly)
3. Check if VER.LIT and DIR.LIT also fail at 20M — partial results show
   VER and DIR work at 20M, suggesting only MULTI-BLOCK .LIT files fail
4. The program partition address changes: $00Dxxx during INI, $175xxx
   after. Check JCB partition settings at both times.
5. DCACHE might deliver data synchronously (cache hit) without SCSI
   reads. Trace DCACHE's internal copy mechanism.

## Timer Architecture (working, but complex)

Three fixes keep the timer alive:
1. **Per-underflow trigger**: Each timer underflow sets _interrupt_pending
   if that timer's IRQ is enabled. Prevents stuck-composite problem.
2. **CR2 interlock**: Hardware prevents both T1 and T2 IRQs from being
   disabled simultaneously (only after timer system is stable).
3. **Watchdog**: Detects all-IRQs-disabled state, clears flags, enables
   T1+T2, and sets OS flag $0488=$FF to restart TINC processing.

CPU_TIMER_RATIO = 32 (vs hardware 8) compensates for cycle undercount.

## Files modified (this session)
- `alphasim/devices/timer6840.py` — Timer with all three fixes
- `alphasim/main.py` — Wire `_recovery_bus` for watchdog $0488 access

## Verification
```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/ -k 'not test_native_boot_reads_amosl_ini'
# Expected: 60 passed, 1 skipped, 1 deselected (~3:35)

python3 main.py
# Expected: boots to prompt, VER works, .LIT programs fail
```

## Key Architecture (1.3 image ONLY)

### SCSI WRITE flow (PIO, not DMA)
- Driver sends WRITE CDB → DATA_OUT phase
- PIO writes 512 bytes → auto-complete → STATUS phase
- Driver writes $80 → schedules completion IRQ

### ACIA interrupt levels
- Port 0: level 2 (autovector 26)
- Ports 1/2: level 3 (autovector 27)

### DCACHE.SYS
- Loaded during INI: `.SYSTEM DCACHE.SYS/N/M/U 100k`
- Intercepts all disk reads after loading
- Uses internal buffer at $038xxx for cached sectors
- Supposed to copy cached data to requester's buffer
