# Resume Note

Last updated: 2026-03-27
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

- **TDV not loaded**: TRMDEF completes but "WYSE" parsed as buffer
  param, not TDV filename → T.TDV=0, TCB+$76=0
- **T.IHW = 0**: ACIA hardware address not set in TCB
- No terminal output (ACIA TX callback never fires)
- JOBTRM never set → Tw wait forever

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

### IDV signature check (informational only)

The AM1000 IDV code at `$008A5E` checks `$66FD83EF` = RAD50 "PSEUDO".
This is informational only (outputs "System not available" message)
and does NOT block terminal assignment. The IDV `$0400` flag is a
capability bit (not T$DIS). Console ID test at `$3E878C` passes.

### T.INC / T.OTC = 0 is NORMAL (2026-03-27)

The AM130.IDV source code reveals T.INC=0 and T.OTC=0 is the
**normal case** — when zero, the ISR uses the built-in TRMICP/TRMOCP
macros from TRMSER. The IDV installs its ISR directly into the CPU
vector table, not through COMINT. COMINT ($A0F8) is for the AM-350
intelligent I/O controller, not standard serial ports.

### Actual blocker: TDV never linked, T.IHW=0 (2026-03-27)

**Correction**: TRMDEF does NOT hang — it completes. The scheduler
loop seen earlier was normal async I/O processing. Extended trace to
8M instructions confirms:

The TRMDEF handler at `$3E8xxx` processes:
1. TCB clear at `$3E83B2` (i=5221384)
2. IDV load at `$3E84B0` → AM1000.IDV loaded → T.IDV = `$89F4`
3. Buffer params parsed: 3x DSCAN ($A02A) at `$3E851E/8550/857E`
4. $A01E (SCNR) at `$3E85A0` hits CR → Z=1 → skips TDV load path
5. T.JLK set to `$7038` at `$3E874C`
6. IDV INIT call at `$3E8766` → IDV CHROUT via TCRT handler
7. Many TCRT ($A048) calls from `$3E8F5C` for terminal setup
8. TRMSER output processing at `$002AFA` writes to output buffer

But the TDV is never loaded because the TRMDEF command text has no
separate TDV name parameter — "WYSE" was consumed as part of the
buffer allocation, not as a TDV filename to FETCH.

### JOBTRM gate mechanism (2026-03-27)

The LINE-A handler at `$001FCA` (called 85 times) decides JOBTRM:
```
$001FCA: MOVEM.L ...           ; save regs
$001FCE: MOVEA.L ($041C).W,A0  ; A0 = JCB
$001FD2: MOVEA.L $38(A0),A5    ; A5 = TCB (from JCB+$38)
$001FD6: MOVE.L A5,D7          ; test if TCB exists
$001FD8: BNE $001FE4           ; yes → continue
$001FE4: MOVEA.L $0E(A5),A4    ; A4 = T.IDV
$001FE8: TST.W D1              ; test function code
$001FF0: BCC $00201C            ; D1 >= $FF00 → check dispatch
$00201C: MOVE.L $76(A5),D7     ; D7 = TCB+$76 (dispatch table)
$002020: BEQ $00200E            ; if zero → EXIT (skip JOBTRM!)
$002022: MOVEA.L D7,A6          ; A6 = dispatch table
$002024: CLR.L D7
$002026: MOVE.B D1,D7           ; function index
$002028: LSL.L #1,D7            ; word offset
$00202A: MOVE.W (A6,D7.W),D7   ; read dispatch entry
$002030: MOVE.W D7,D1           ; new function code
```

TCB+$76 = `$00000000` because TRMDEF never completed TDV loading.
Every call to this handler skips at `$002020` → JOBTRM never set.

### Root cause chain (updated 2026-03-27)

1. TRMDEF completes but T.TDV = 0 (TDV never loaded)
2. TCB+$76 = 0 (no TCRT dispatch table)
3. Terminal handler at `$001FCA` always skips at `$002020`
4. JOBTRM never set
5. TTYOUT finds JOBTRM=0 → job enters Tw wait forever

The async disk I/O chain DOES work (24 IOINI, 25 IOWAIT, 5 WAKE
calls during TRMDEF). The first 207 SCSI reads are synchronous but
the FETCH calls during TRMDEF do complete asynchronously.

### Corrected TCB field layout (AMOS 1.3)

The TCB field offsets on AMOS 1.3 differ from the manual:
```
TCB+$00 = $0080  T.STS (status word)
TCB+$02 = $85FC  AM1000.IDV module pointer (long)
TCB+$06 = $FFFFFE20  T.IHW (ACIA port 0 hardware address)
TCB+$0E = $89F4  WYSE.TDV module pointer (long)
TCB+$40 = $7038  T.JLK (JCB link)
TCB+$76 = $0000  TCRT dispatch table (never set)
```

T.IHW IS correctly set to $FFFFFE20. Both IDV and TDV ARE loaded.
AM1000.IDV is at $85FC, WYSE.TDV is at $89F4. The BSR at $3E84B0
loads WYSE.TDV (not IDV as first assumed).

### Actual blocker: IOWAIT in terminal output never completes

The TRMSER output handler at $001EC2 calls IOWAIT ($A03E) with
D6=4, waiting for the ACIA TX to complete. The IOWAIT handler puts
the job to sleep (removes from run queue, JOBCUR=0). The scheduler
idles forever because the ACIA TX interrupt never fires to WAKE
the job.

The output flow:
1. TCRT ($A048) → $001FCA handler → IDV dispatcher
2. IDV → TRMSER output at $002AFA → writes char to output buffer
3. $001EC2: IOWAIT ($A03E) — **job goes to sleep**
4. Expected: ACIA TX interrupt fires, dequeues char, calls WAKE
5. Actual: TX interrupt never fires → job never wakes

### Next steps

1. **Fix ACIA TX interrupt generation**: The ACIA should generate
   a level-2 interrupt when TDRE=1 and TX IRQ is enabled ($B5
   control = TX+RX IRQ). The TX IRQ latch may need adjustment.
2. **Verify the IDV CHROUT path**: The AM1000.IDV CHROUT at $85FC
   +$02 ($600A → $8608) should enable TX interrupts on the ACIA.
3. **Check if TINIT is called**: TINIT ($A0FA) should kick off
   output by triggering the first TX interrupt.

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
