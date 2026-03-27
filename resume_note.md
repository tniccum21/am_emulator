# Resume Note

Last updated: 2026-03-26
Branch: `feature/native-boot-milestones`

## Current State

The native boot on the real boot image (`AMOS_1-3_Boot_OS.img`) has made
major progress. Two SCSI bus fixes unblocked the entire OS I/O chain:

1. **SCSI selection**: single-stage handshake (the 68010 monitor sends
   $00/$01/$11 once, not twice)
2. **SCSI DMA interrupt**: level 5 (monitor's ISR at $006C18), not level 2
   (ROM stub at $B9C)

### What now works natively

- ROM boot: 78 SASI reads (LBA 0-3326)
- OS SCSI driver at $006BBE: 207 reads via SCSI bus interface ($FFFFC8)
- TEST UNIT READY → GOOD
- READ(10) with DMA to monitor buffers
- AMOSL.INI read at LBA 3335
- LINE-A $A0AA (disk mount) completes and returns
- USP setup: `MOVE A6,USP` at $003740 executes, JCB+$7C = $7AC2

### What doesn't work yet

- No terminal output (ACIA TX callback never fires)
- No user-mode dispatch confirmed (need to verify stacked SR has S=0)
- COMINT has not started processing AMOSL.INI commands

### What happens after USP is set

User mode IS entered. COMINT processes AMOSL.INI:
- `:T` — trace mode on
- `JOBS 5` → 8 JOBBLD calls (5 + JOBALC + system)
- `TRMDEF TRM1,AM1000=0:19200,WYSE,...` → ACIA configured $03/$95/$B5
  - TRMATT ($A038) called successfully (allocates channel $182E)
- `VER` → TTYOUT ($A0CA) called 66x with D1='M','O','N',...

But zero TX output because TTYOUT → $A00A → IOGET → DDB queued but
DDB dispatch at $14FE never runs. Characters go through the I/O chain
but are never flushed to the ACIA.

### TCB discovery (2026-03-27)

The TCB exists at `$856E` with correct data:
- T.IHW = `$FFFE20` (ACIA port 0)
- T.JLK = `$7038` (→ JCB, bidirectional link attempt)
- AM1000.IDV loaded from disk at i=5,224,350
- WYSE.TDV loaded from disk at i=5,243,329
- Buffer size = 100 (from TRMDEF `100,100,100`)

But T.STS = `$0080`:
- **T$ASN ($200) NOT set** → terminal not assigned → can't attach
- T$DIS ($400) not set → not disabled
- T$OIP ($80) set → "output in progress" stuck from init

JOBTRM is NEVER written to a non-zero value. The TCB→JCB link
(T.JLK=$7038) exists but the JCB→TCB link (JOBTRM) is never set
because T$ASN is false.

The 66 TTYOUT calls with D6=$503 are trace/command-file output
(`:T` trace mode), not terminal TTY. Real terminal attachment
happens when COMINT calls EXIT or KBD after INI processing.

### Exact failure point: IDV signature check

The AM1000 IDV code at `$008A5E` does:
```
CMPI.L #$66FD83EF,-4(A6)   ; A6 = TCB+$4C = $91BC
BEQ    $008A7E               ; if match → success (set T$ASN)
                              ; Z=0 → MISMATCH → error path
```

Value at `$91B8`: `$00000064` (= 100, a buffer size parameter)
Expected: `$66FD83EF` (driver module signature)

The signature `$66FD83EF` doesn't exist ANYWHERE in loaded data.
WYSE.TDV escape sequences ARE loaded at `$92EA`, but TCB+$4C
(`$91BC`) points to a parameter block, not the driver entry point.

Possible causes:
1. TCB field offsets differ between AMOS versions
2. The .LIT loader doesn't set up the module signature
3. WYSE.TDV format doesn't include this signature on this version

## Key Decoded Addresses

### Scheduler
```
$001250: TST.L ($041C).W       ; test JOBCUR
$00129C: MOVEA.L ($041C).W,A0  ; load JCB
$0012AE: MOVEA.L $80(A0),A7    ; switch to job's SSP
$0012B2: MOVEA.L $7C(A0),A6    ; load saved USP
$0012B6: MOVE A6,USP           ; set USP
$0012EE: MOVEM.L (A7)+,...     ; restore registers
$0012F2: RTE                   ; dispatch to job
$001524: SCHED handler          ; decrement timeslice, preempt
$0014EA: WAKE handler           ; increment timeslice, set bit 13
```

### Init flow
```
$008196: JOBCUR = A0 (create JCB)
$0081AC: clear JCB memory (5396 bytes)
$0082B0: ACIA master reset ($03)
$008314: BSR $0033A6 (RTC init wrapper)
$0033B2: LINE-A $A07A (RTC init + TIMCAN/IOWAIT)
$008334: JMP $0036CE (USP setup)
$0036CE: allocate $68 bytes ($A060)
$0036E6: LINE-A $A080 (SRCH for DSK)
$0036F2: LINE-A $A0AA (disk mount — 207 SCSI reads)
$0036F4: MOVEA.L ($041C).W,A6 (after mount)
$003720: skip path (JCB+$18=0)
$003740: MOVE A6,USP ← USP SET!
```

### SCSI driver
```
$006B74: SCSI bus reset ($00 to $FFFFC8)
$006B96: Selection: write target ID + ATN
$006BBE: Poll $FFFFC8 for REQ (bit 1)
$006BF8: Write CDB bytes to $FFFFC9
$006C0E: DMA trigger ($80 to $FFFFC8)
$006C10: LINE-A $A03E (IOWAIT for DMA completion)
$006C18: Level-5 ISR (SCSI DMA completion handler)
```

### LINE-A table (at $0712)
```
$A034 → $1756 (IOGET)    $A03C → $12F4 (IOINI)
$A03E → $11DE (IOWAIT)   $A044 → $1040 (TIMSET)
$A046 → $10B6 (TIMCAN)   $A04C → $14EA (WAKE)
$A04E → $1524 (SCHED)    $A060 → $1A8C (MEMGET)
$A080 → $4982 (SRCH)     $A086 → $4982 (FETCH)
$A0AA → $4982 (MOUNT)    $A0CA → $2ED0 (TTYOUT)
```

## Verification

```bash
# Full test suite
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/ -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'
# Expected: 60 passed, 1 skipped, 1 deselected

# Quick native boot probe
python3 trace_post_scsi_fix.py
# Expected: HIT $003740 (MOVE A6,USP), 207 SCSI ops, JCB+$7C=$7AC2
```
