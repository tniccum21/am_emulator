# AlphaSim AM-1200 Emulator — Project Status

Last updated: 2026-03-24

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

### Native Boot (main.py) — IN PROGRESS

The goal is for the OS to boot and run commands using its own code, with only hardware emulated in Python. **This is not working.** main.py is currently full of Python bypasses that fake OS behavior, masking the real problem.

#### What ACTUALLY works (real hardware emulation)
1. **CPU core**: All 68010 instructions execute correctly
2. **ROM boot**: Loads AMOS from disk via real SCSI emulation (WD1002 + SCSI bus)
3. **Hardware devices**: Timer (MC6840), LED, DIP switch, ACIA (6850), SASI/SCSI
4. **OS loads and runs**: Scheduler runs, COMINT runs, LINE-A dispatch works
5. **Serial driver injection**: At $6C72, installs TX driver at $00B800 (hardware-level — like plugging in a serial board)
6. **Terminal detect handshake**: Driver returns Z=1, CR sent and echoed via ACIA

#### What is FAKED by Python bypasses (NOT real progress)
1. **FIND $A06C bypass** (main.py ~line 598): Python reads disk instead of the OS — native FIND has NEVER worked
2. **$002854 output intercept** (main.py ~line 683): Python captures terminal chars because $043C=0 (no terminal driver)
3. **Input bridging** (main.py ~line 889): Python stuffs TCB instead of real ACIA ISR path
4. **TTYIN $A072 intercept** (main.py ~line 701): Python reads TCB buffer instead of native handler
5. **Hardcoded minimal INI** (main.py ~line 874): Only `JOBS 8` + `VER`, skipping entire real boot sequence
6. **TTYOUT $A0CA intercept** (main.py ~line 688): Python emits characters

With all bypasses active, running `printf 'VER\n' | python3 -m alphasim.main ...` shows VER banners. But this is fake — Python's FIND loads the file and Python's output intercept displays it. The OS itself did nothing.

## Current Native Frontier

There are currently **two different native frontiers**, depending on which
disk image is being exercised. They should not be conflated.

### Selector/PIT Frontier (`HD0-V1.4C-Bootable-on-1400.img`)

This is the image used by `tests/integration/test_boot_native_pit_irq.py`.
On this path, the recent PIT work is still valid:

- native `cpu_model=68020` boot reaches the PIT-backed level-6 handler at
  `$0018E0`
- the same path reaches later monitor code at `$001DBE`

Those regressions are useful, but they do **not** prove progress on the real
`AMOSL.INI` boot path.

### Real Boot Frontier (`AMOS_1-3_Boot_OS.img`)

This is the image that matters for `AMOSL.INI`, and it is still blocked
earlier.

What is now verified on the current tree:

- `AMOSL.INI` still starts at logical block `3334`, so the first native read
  would be `LBA 3335`
- `trace_native_amosl_ini_path.py` on the real boot image still tops out at
  `LBA 3326`
- therefore native boot is still blocked in an **earlier command-file/job
  path**, not at terminal bring-up and not yet at `AMOSL.INI`

The current real-boot stop after a long native run is:

- `pc=$001C72`
- LED history `06 0B 00 0E 0F 00`
- `JCB+$20=$000B`
- `JCB+$38=$003E8000`
- final low sysvars are invalid:
  - `JOBCUR=$00FFFE11`
  - `JOBQ=$00000000`
  - `DDBCHN=$00007038`
  - `ZSYDSK=$00006C3A`

The most concrete upstream lead is now the earlier, still-stable sysvar/job
regime captured by `trace_native_sysvar_corruption.py`:

- initial real-boot setup:
  - `ZSYDSK: $00000000 -> $00007030` at `pc=$033186`
  - `JOBCUR: $00000000 -> $00007038` at `pc=$00819A`
  - `ZSYDSK: $00007030 -> $00007AC2` at `pc=$0081B6`
- then the repeating late loop before the `3326` ceiling:
  - `pc=$001230`: `JOBCUR $7038 -> 0`
  - `pc=$001338`: `JOBCUR 0 -> $7038`

So the next real problem is the earlier command-file/job scheduler path,
especially why the real boot image stays trapped in the `JOBCUR 0 <-> $7038`
regime and later drifts into the final bogus sysvars
instead of advancing to the first `AMOSL.INI` read.

The current tree also refines the older March-10 scheduler hypothesis:

- the natural real-boot path still reaches the same late scheduler sites with
  `USP = 0`
- first clean hits now confirm the loaded-monitor ops directly:
  - `$00122C`: `MOVE.L (A3),($041C).W`
  - `$001230`: `CLR.L (A3)+`
  - `$001338`: `CLR.L 120(A0)` which clears `JCB+$78`
- at those first natural hits:
  - `A0 = $7038`
  - `A3 = $70B0` at `$00122C/$001230`
  - `A3 = $70B4` at `$001338`
  - `USP = 0`
  - `JCB+$78/$70B0 = 0`
  - `JCB+$7C/$70B4 = 0`

That keeps the old “zero `USP` / broken runnable-chain producer” diagnosis
alive, but one important continuity assumption is now false:

- the older one-shot `USP=$00032400` seed at `$006B7A` no longer drives the
  current tree into the deeper repaired late `AMOSL.INI` path
- on today’s code it instead stabilizes in a later runnable-chain loop:
  - `$0013D2 = MOVE USP,A6`
  - `$0013F2 = MOVE.L 120(A6),D7`
  - `$0013F6 = BEQ ...`
  - `$0013F8 = MOVEA.L D7,A6`
  - `$0013FA = BRA $0013F2`
- even on that seeded path:
  - `last_lba` is still `3326`
  - `last_a086_pc` stays zero
  - `saw_55aa`, `saw_56BC`, and `saw_56D2` stay false

So the real current target is not “reuse the old USP workaround.” It is:

1. explain the natural producer-side state at `$00122C/$001230/$001338`
2. explain why the seeded path only advances to the `$0013D2/$0013FA`
   runnable-chain loop on the current tree
3. only then revisit whether an emulator change around `USP` provenance,
   reset modeling, or supervisor/user transitions is justified

The highest-value native change on 2026-03-24 is the new timer block at
`$FFFE60-$FFFE67`. The monitor is using that block on the clean
`cpu_model=68020` path, so the older “missing MC6840/PTM wake” framing was
wrong for this image.

What is now implemented:

- `alphasim/devices/timer8253.py` models the native PIT-style block the loaded
  monitor touches at `$FFFE60/$61/$62/$63/$64/$66`
- it is registered from `build_system()` in `alphasim/main.py`
- the native vector-30 path is now backed by real hardware instead of an
  unimplemented open bus

What the monitor is actually doing on this path:

- queue-arm path:
  - `$0019D8` multiplies the queued delay by `20`
  - writes the one-shot count to `$FFFE62`
  - starts it with `$FFFE63 <- 1`
- vector-30 ISR path:
  - `$0018E0` checks `($0564).W` / `($0403).W`
  - on this image it branches to `$001944`
  - that path acknowledges `$FFFE60/$FFFE61`, not `$FFFE13`
- native timer init path near `$00F182` writes:
  - `$FFFE66 <- $B6`
  - `$FFFE64 <- $14`, `$00`
  - `$FFFE66 <- $30`, `$FFFE61 <- $00`
  - `$FFFE66 <- $70`, `$FFFE63 <- $00`

This moves the frontier materially:

- clean native `cpu_model=68020` boot now reaches the native level-6 handler
  at `$0018E0`
- the old `$001C90/$001CAC` wait-loop frontier is cleared
- the run reaches later loaded-monitor code at:
  - `$001DBE` by about `4.043M` instructions
  - `$001EB2` by `8.0M` instructions
- by that later point:
  - `QHEAD=$00000000`
  - `EVBUSY=$0000`
  - `JOBCUR=$0000A86E`

Test/coverage updates:

- added:
  - `tests/devices/test_timer8253.py`
  - `tests/integration/test_boot_native_pit_irq.py`
  - `tests/cpu/test_rte.py`
- retired:
  - `tests/integration/test_boot_native_scsi_dma_irq.py`
  - `tests/integration/test_boot_native_scsi_alias_command.py`
- verification after the change:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/cpu/test_rte.py tests/devices/test_scsi_bus.py tests/devices/test_timer6840.py tests/devices/test_timer8253.py tests/integration/test_boot_native_cpu_probe.py tests/integration/test_boot_native_pit_irq.py`
    - `13 passed`
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'`
    - `22 passed, 1 skipped, 1 deselected`

The current blocker is no longer the old pre-selector low-memory loop, the
ACIA hypothesis, or the earlier post-SCSI fill-region jumps. Several upstream
issues are now fixed:

1. **Missing `$FFFE40-$FFFE5F` direct clock/date bank**
   - implemented in `alphasim/devices/rtc_direct_bank.py`
   - mapped in `build_system()` and covered by device/integration tests

2. **Broken `MOVEC CACR` behavior**
   - unsupported control-register `MOVEC` now traps on the default
     `cpu_model=68010`
   - higher models now expose minimal `CACR` semantics:
     - `68020`: bit 9 sticks
     - `68030/68040`: bits 31 and 9 stick
   - with `cpu_model=68020`, the native selector reaches `$00ED40` with
     `D1=$00000008` and sets low `SYSTEM bit $00008000` without any fake
     `SYSTEM` write

3. **Broken low-memory `$FFFFC8/$FFFFC9` alias handshake**
   - the SCSI-bus interface now models the monitor's observed two-stage
     selection handshake
   - after the first `00/00/01/11`, `$FFFFC8` reports `$14`
     (pending command handshake)
   - after the second `00/00/01/11`, the interface enters real `COMMAND`
     phase and accepts CDB bytes

That last fix moved native boot materially, but one more downstream hardware
bug was still masking the next frontier:

- on the `cpu_model=68020` path, the low-memory alias writer emits a real
  `READ(10)` CDB:
  - `28 00 00 00 00 02 00 00 01 00`
- the SCSI bus interface executes:
  - `READ lba=2 count=1`
- after DMA, the AM-1200 monitor expects the SCSI completion interrupt on
  autovectored **level 2**, not level 5
- `alphasim/devices/scsi_bus.py` now raises level 2, and the native trace
  shows:
  - `SCSI DMA complete status=$00 irq_delay=2048`
  - `SCSI IRQ pending`
  - `SCSI IRQ ack level=2 vector=26`

That correction removed the old apparent `LED = 12` frontier, but two more
downstream native bugs were still distorting the control flow:

- the low-memory vector-26 helper at `$004EF8/$004EFC` pushes a replacement
  SR byte and then executes `RTE`
- the emulator's generic synthetic 68000-style exception frame for normal
  exceptions left that helper returning into garbage instead of back to
  monitor code
- `alphasim/cpu/exceptions.py` now applies a narrow compatibility rule for
  vectors 8, 9, and 26 so the relevant monitor helpers return correctly
- privileged instructions in `alphasim/cpu/instructions.py` now raise vector 8
  using the faulting opcode PC instead of the already-advanced PC
- dedicated integration regressions now cover both return paths:
  - `PC=$001C98`
  - `SR=$0019`
- and later:
  - `PC=$001CAC`
  - `SR=$0019`

Those corrections remove the earlier bogus jumps to fill regions around
`PC=$190000`, `PC=$083A10`, and `PC=$1784B6`.

The next live frontier is now the low-memory delayed-event queue/timer wake
path, not a plain missing `JCB+$78` producer:

- the first scheduler dequeue at `$001C44/$001C48/$001C4C` still clears:
  - `JOBCUR=$0000A86E -> 0`
  - `JCB+$78=$00000000`
- but the run also has a valid queued work block:
  - `$001A2A` writes `($042A).L = $00007BC2`
  - `$001980` sets `($046E).W = $00FF`
- the live queued block at `$7BC2` is a timeout/work area seeded by the
  loaded monitor path at `$00A2F0`, not the older `$1B00` callback node:
  - `link=$00000000`
  - `delay=$0000C350`
  - `callback=$00000000`
  - `owner=$00000000`
- by `8,000,000` instructions, the delay never changes from `$0000C350`
- none of the known timeout-service PCs are reached in that window:
  - `$001902`, `$001910`, `$001920`, `$001924`, `$00227C`, `$002280`
- none of the old queue-consumer/callback sites are reached either:
  - `$00199C`, `$001B00`, `$001D10`, `$001D80`, `$001D86`
- after the first dequeue, native code sits in the idle/wait loop around
  `$001C90/$001CAC` with:
  - `QHEAD=$00007BC2`
  - `EVBUSY=$00FF`
  - `WAKE0=$0000`
  - no new interrupt taken during the open interrupt window at `$001C94`

The ACIA theory is therefore demoted from “current blocker” to “negative
finding”:

- by `8,000,000` instructions on the corrected path, there is still no ACIA
  transmit
- ACIA port 0 still sees only one observed access:
  - status read at `PC=$00F2B6`, returning `$16`
- there are still no writes to `$FFFE20-$FFFE25` or `$FFFE28`
- simply injecting the existing compat serial-driver stub does not move the
  run past the earlier frontier

So the next real problem is now the periodic wake source behind the delayed
event queue after the first successful low-memory alias `READ(10)` and the
corrected exception return helpers:

- what is supposed to service or decrement the queued block at `$7BC2`
- why the timeout-service path never runs before the later low-memory
  corruption around `pc=$002584`

## THE BLOCKER — Native FIND ($A06C)

The OS has **never successfully found a file on disk by itself**.

Every command in AMOSL.INI needs FIND to locate and load program files from disk. The boot sequence is:

```
:T                          ← label
JOBS 8                      ← FIRST executable command, needs FIND to load JOBS.LIT
JOBALC JOB1,MODEM,JOB2
TRMDEF TRM1,AM130=0:19200,WY50,250,250,250,EDITOR=100   ← CRITICAL: sets up terminal
VER
PARITY
LOAD TRMDEF
TRMDEF TERM2,AM130=1:19200,WY50,150,150,150
TRMDEF NULL,PSEUDO,NULL,100,100,100
DEL TRMDEF
DEVTBL DSK1,DSK2,...,DSK15
DEVTBL TRM,RES,MEM
BITMAP DSK,,0,1,...,15
ERSATZ ERSATZ.INI
QUEUE 2500
MSGINI 10K
LOAD SYSTEM
SYSTEM DCACHE.SYS/N 200K
SYSTEM TRM.DVR[1,6]
... (more SYSTEM lines)
SYSTEM
DEL SYSTEM
LOG SYSTEM SERVICE
MOUNT DSK0: through DSK15:
... (more setup)
VER
```

TRMDEF is the critical command — it creates the terminal device, sets $043C (terminal output driver pointer), and establishes the entire I/O chain. Without it, there's no terminal, no output, no input. Everything after TRMDEF depends on it.

But TRMDEF can't load if FIND doesn't work. And JOBS can't load either — it's the very first command.

**Until native FIND works, nothing works. All Python bypasses are masking this single failure.**

## What We Know About FIND

- **Calling convention**: D6=1 for find-by-name. A4 points to file spec: A4+$06=name1 (RAD50), A4+$08=name2 (RAD50), A4+$0A=ext (RAD50), A4+$0C=PPN (packed proj,prog)
- **D6=0/2 (FETCH)**: Reads spec from A6 stack frame (3 words: name1, name2, ext)
- **The Python bypass proves files ARE on disk**: JOBS.LIT at [1,4], VER.LIT at [1,4], etc. The disk directory structure is valid.
- **The native FIND code reads disk via SCSI**: SCSI reads happen at LBAs 351-1944 (directory area)
- **But FIND never returns success**: Every command produced "Message # " error before bypasses were added

## What We Know About Terminal Output

- Terminal output goes through the scheduler at $00284A, NOT through TTYOUT ($A0CA)
- Scheduler checks `TST.L ($043C).W` — when $043C=0, BEQ at $002854 skips output
- $043C is the terminal output driver pointer, set by TRMDEF/ATRS.DVR
- Full output path: JCB from $041C → TCB from JCB+$38 → terminal device block → buffer → $002928
- Without TRMDEF, $043C=0 and all output is silently dropped

## NEXT STEPS

1. **Keep native boot free of Python bypasses** in `main.py` (FIND, output intercept, input bridging, `TTYIN`)
2. **Treat the current frontier as the low-memory queue/timer loop, not FIND**
3. **Explain the repeating `$7BA2 -> $1B00 -> $7BC2/$7BE2` callback chain**
4. **Explain why the low-stage loop stays in IPL7 polled-timer service instead of graduating into real boot work**
5. **Only return to native FIND after that early queue/timer loop is understood**

The only legitimate Python intercept is the serial driver injection at $6C72 (simulating plugging in hardware). Everything else should be done by the OS.

## Latest Checkpoint

The most current native checkpoint is now:

- default `cpu_model=68010`:
  - clean selector hit at `$00ED40` arrives with `D1=$00000002`
  - `SYSTEM` updates only to `$00300424`
- native `cpu_model=68020`:
  - selector reaches `$00ED40` naturally with `D1=$00000008`
  - low `SYSTEM bit $00008000` is set natively
  - the low-memory alias handshake at `$FFFFC8/$FFFFC9` now progresses into
    a real `READ(10)` instead of polling `$16` forever
  - the next frontier is after that first successful low-memory read

The older low-memory queue/timer findings below are still relevant historical
context for how the frontier moved, but they are no longer the highest-yield
active blocker.

The immediate blocker is now earlier than native `FIND`, and earlier than a PTM
interrupt explanation.

What is currently proven on the clean native path:

- `AMOSL.MON` loads and runs.
- After `LED 0E -> 0F -> 00`, the first low-memory monitor stage is reached at
  roughly `3.49M` instructions with `PC=$0008CE`.
- The first live JCB/DDT is built at `$A86E`.
- Native code explicitly clears `$A86E..$A8F6` at `$00F05E`, then repopulates
  selected fields.
- The first scheduler dequeue at `$001C48` still reads `JOBCUR+$78`, gets
  zero, clears `JOBCUR`, and falls into the idle loop.
- But the low-stage run does **not** wait for a PTM interrupt. During this
  whole loop the CPU is running at `SR=$2700/$2704/$2714`, so the IPL mask is
  `7` and a level-6 PTM interrupt cannot be accepted there.
- Low-memory AMOSL is actively polling and reprogramming the PTM instead:
  - monitor PTM setup at `$00F1B4 .. $00F1D8`
  - delayed-event arm at `$0019D2/$0019D6`
  - PTM status/counter polling at `$001902`, `$001910` or `$001920`
  - PTM re-arm/reset at `$00227C/$002280` and `$001914/$001924`

The active callback/event node is now concrete:

- queue node base is `$7BA2`
- `+$00` is the link field
- `+$04` is a delay/state field (`1` when queued, later cleared to `0`)
- `+$08 = $00001B00` callback
- `+$0C = $0000A86E` owner/job pointer
- `+$08` is no longer a mystery: `$1B00` loads `A0 := *(node+$0C) = $A86E`,
  sets `D6 = 8`, and re-enters `LINE-A $A03C`, which lands at the native
  `IOINI` path at `$001D10`

The measured repeating loop is:

- `$0022FE .. $002304` clears the node
- `$001ACE/$001AD8` calls `LINE-A $A034`
- that path enters `$0022E2`, pulls a free node from `($0490).W`, and returns
  `D6 = $7BA2`
- `$001ADE` stores the saved delay value (`1`) into `node+$04`
- `$001AE2/$001AEA/$001AF0` rebuilds it (`+$04=1`, `+$08=$1B00`,
  `+$0C=$A86E`)
- `$001AF0` calls `LINE-A $A044`
- `$001A14` queues `$7BA2`
- `$00197C` sets `($046E).W = $FF`
- `$0019D2/$0019D6` arms the PTM delay path
- `$001C48` dequeues and drops `JOBCUR` to zero
- later `$001994/$00199C` pops the node and calls the callback at `$1B00`
- that callback clears the DDT status word at `$A86E` from `0008` to `0000`,
  restores `JOBCUR` through the `IOINI` queue/store path, and then returns the
  queue node to the free list
- `$001D6E` restores `JOBCUR = $A86E`
- `$002332` pushes `$7BA2` back onto the free list headed at `$0490`
  (so `node+$00` becomes the previous head, `$7BC2`)
- then the same node is rebuilt and the cycle repeats

Persistent state on that loop:

- `($04C0).W` stays zero
- `($04C1).W` stays zero
- `($042A).W` oscillates between `0` and `$7BA2`
- `($046E).W` oscillates between `$FFFF` and `$FFFFFF`
- the run repeatedly re-enters `$001A14/$0019D2/$001C48/$001994/$001D6E`

So the best current diagnosis is:

- the native low-memory path is in a **polled timer/callback loop**
- the earlier “missing PTM interrupt into `$000FA2`” theory was wrong for this
  stage
- the `$1B00` callback is an `IOINI` re-entry/completion path, not driver init
- the producer side uses `A034`/`A044` to allocate and queue a delayed callback
  node from the free list, so the next fault is whatever keeps requesting that
  same delayed callback again
- the immediate service loop around `$004374/$004398` is polling the RTC block
  at `$FFFE04/$FFFE05`, not the disk controller; observed command/data pairs
  include RTC-style reads such as command `$5C` returning BCD digit `$02`
- no writes to the `$B440` state block were observed during a large slice of
  this loop, so the repeated delayed callback currently looks more like a
  background RTC/service cycle than a direct “disk hardware not ready” wait
- software-side tracing of `A04C/A04E/A046` supports that read: the visible
  side effects are only toggling `($0450).W` between `0` and `-1` and toggling
  the DDT word at `$A86E` between `0` and `$2000`, after which `A046` is
  called with `D6=1` to request another one-tick delayed callback
- so this loop increasingly looks like a normal periodic RTC/task service path,
  not the primary boot blocker
- filtered post-init write tracing over `$A866-$A97F` shows no second JCB/DDT
  being created at all; after init the only live writes are to the existing
  `$A86E` block and its context fields:
  - `$A86E/$A870` status fields
  - `$A8E6-$A8F5` queue/save slots
  - `$A8FC/$A900-$A905` small counters/state words
- higher-level payload tracing also stays empty: the name descriptor at
  `$007074` never becomes live on this pure native path, even after the low
  stage begins; `JCB+$20` only becomes nonzero when the initial service block is
  established
- direct tracing explains why: the pure native path never reaches the
  `SCNMOD/TTYLIN` family at `$00390A/$00392C/$003932` or the later descriptor
  service path at `$005118/$005140`
- instead it enters a different branch at `$003EBE`, and on the first live hit
  the gating state is:
  - `($0564).W = $00000001`
  - `($0400).W = $00302404`
  - the `($0400) & $00008000` test therefore fails, so execution branches to
    `$003F7A` (an RTC/date-service path) rather than falling through toward the
    descriptor-building family
- that gate is now decoded exactly:
  - `$003EBE`: `MOVE.L ($0564).W,D7`
  - `$003EC2`: `ANDI.L #$00000008,D7`
  - `$003EC8`: `BNE $003F7A`
  - `$003ECC`: `MOVE.L ($0400).W,D7`
  - `$003ED0`: `ANDI.L #$0000A000,D7`
  - `$003ED6`: `BEQ $003F7A`
  - `$003EDA`: `ANDI.L #$00008000,D7`
  - `$003EE0`: `BEQ $003F7A`
  - only after that does the code fall through to `A04C` and the later service
    logic
- the provenance of `SYSTEM=$00302404` is also concrete now:
  - ROM/monitor copy loops seed `$0400` from the file image as `$00300400`
    (`$03286E`, `$033184`)
  - `$032560` sets bit `$00000004`
  - `$00ED5C` sets bit `$00002000`
  - `$00F13C` sets bit `$00001000`
  - `$00F160` immediately clears that same `$00001000` bit again
  - **no native write before the gate ever sets `$00008000`**
- comparison against `AMS4.MON` and `TEST4.MON` shows the `$0400 & $A000` and
  `$0400 & $8000` tests are common across monitor families; those generated
  monitors differ around `$003EBE/$003EE4`, but they still require the `$8000`
  `SYSTEM` bit before they will avoid the RTC/date side path
- forcing `SYSTEM |= $00008000` once at the first `$003EBE` hit is enough to
  change control flow materially: execution leaves the low-memory `$003EBE ->
  $003F7A` loop and moves into the `$0043BC-$0043CC` family instead
- but that forced-bit experiment does **not** yet reach `SCNMOD/TTYLIN`,
  `A06C`, or a live name descriptor at `$007074` by `5.5M` instructions, so
  the missing `$8000` transition is real but not the whole boot failure
- the upstream selector for that `$8000` transition is now identified:
  - at `$00ED40`, live `D1 = $00000004`
  - the code there is a `BTST` ladder on `D1`
  - with `D1` bit 2 set, the run takes the `$00ED5C` path and sets only
    `SYSTEM |= $00002000`
  - the `SYSTEM |= $00008000` writes at `$00ED74/$00ED8C` exist in the monitor
    image but are only taken from the `D1` bit 3 / bit 4 branches
  - so the current problem is **not** “the monitor lacks an `$8000` writer”;
    it is “the emulator reaches the wrong `D1` case before that writer”
- once `$8000` is forced, the next missing dependency is also concrete:
  - the new path enters a subroutine at `$0043BC-$0043D4`
  - there `A5 = $FFFFFE40`, and the code polls byte `26(A5) = $FFFE5A`
  - the emulator currently has **no device mapped at `$FFFE40-$FFFE5F`**
  - reads therefore return open-bus `$FF`
  - that makes the `$0043BC` loop spin forever by construction
- a one-off experimental stub for `$FFFE40-$FFFE5F` was enough to prove the
  dependency is real:
  - with `SYSTEM |= $00008000` forced and a minimal BCD/handshake stub at
    `$FFFE40-$FFFE5F`, the machine gets past `$0043BC`
  - it reaches a real native `A06C` at `PC=$0078E4` almost immediately
  - by `7M` instructions it still had not reached a live `$007074`, but this
    experiment proves the missing `$FFFE40` block is a real blocker on the
    higher path
- so the stronger current diagnosis is that **no additional runnable work ever
  appears**, rather than “extra work exists but the scheduler never runs it”
- the current frontier is to explain why the `$7BA2/$1B00/$7BC2/$7BE2` loop
  is the only visible work instead of background activity alongside real boot
  progress

## Key Files

| File | Purpose |
|------|---------|
| `alphasim/main.py` | Main emulator entry point, contains all the bypasses that need removing |
| `alphasim/cpu/mc68010.py` | MC68010 CPU core |
| `alphasim/cpu/opcodes.py` | Opcode dispatch table |
| `alphasim/cpu/accelerators.py` | Loop acceleration (division, etc.) |
| `alphasim/bus/memory_bus.py` | Memory bus with device mapping |
| `alphasim/devices/acia6850.py` | ACIA 6850 serial (terminal I/O) |
| `alphasim/devices/timer6840.py` | MC6840 timer |
| `alphasim/devices/sasi.py` | WD1002 SASI controller |
| `alphasim/devices/scsi_bus.py` | SCSI bus interface |
| `alphasim/storage/disk_image.py` | Raw disk image access |
| `alphasim/storage/scsi_target.py` | SCSI target (disk) |
| `alphasim/storage/amos_fs.py` | AMOS filesystem reader (used by Python bypass) |
| `alphasim/devices/serial_driver.py` | Injected serial TX driver code |
| `patch_driver_v7.py` | Python bypass track (working but fake) |
| `roms/AM-178-0[01]-B05.BIN` | ROM images (even/odd) |
| `images/HD0-V1.4C-Bootable-on-1400.img` | SCSI disk image |

## Bugs Found — 42 Total

See `memory/bugs.md` for full list. Key categories:
- **#1-7**: Phase 1-2 (CPU, addressing, CHS→LBA mapping)
- **#8-19**: Phase 3 (MC6840 timer — 12 bugs, root cause was CR2 bit check)
- **#20-31**: Phase 4 early (terminal init, TCB, I/O bridging in Python bypass)
- **#32-42**: Phase 4 native boot (TTYOUT register, piped stdin, stale echo, CR/LF, output path, double output, INI line endings)

## Branch

`feature/native-boot-milestones` — many uncommitted changes in main.py and other files.

## 2026-03-25 Native Update

Two more native exception fixes are now in place:

- [alphasim/cpu/exceptions.py](/Volumes/RAID0/repos/am_emulator/alphasim/cpu/exceptions.py)
  now gives vector `8` the normal `68000` short frame `[SR][PC]` again; the
  native privilege helper at `$000AFC` depends on that layout when it reads the
  fault PC with `MOVEA.L 6(A7),A1`
- the same file now preserves supervisor mode on `RTE` from the low-byte SR
  patch helpers at `$003DDA/$003DE2`
- [tests/cpu/test_rte.py](/Volumes/RAID0/repos/am_emulator/tests/cpu/test_rte.py)
  covers both quirks

This removes the old bogus jump into the RAM memtest fill pattern at
`$010000` and eliminates the previous late low-memory corruption path for the
real `AMOS_1-3_Boot_OS.img` run. The corrected native privilege helper now sees
`A1=$00001290` at `$000B04` instead of the broken `$12900000/$12900009`.

Current real-boot frontier on `cpu_model=68020`:

- after `12,000,000` instructions, the machine is stably at:
  - `PC=$001278`
  - `A7=$000006F2`
  - `SR=$2704`
  - `JOBCUR=$00000000`
  - `SVSTK=$000006F4`
- disk reads still stop at `LBA 3326`
- `AMOSL.INI` would still begin at `LBA 3335`, and no read reaches it yet
- there is still no native ACIA TX

The hot-PC profile from `5M..12M` instructions is now a stable scheduler /
interrupt-window loop:

- `$001250`
- `$001254`
- `$001256`
- `$001258`
- `$001274`
- `$001278`
- `$00127C`
- `$00128C`
- `$001294`
- `$00129A`

So the next blocker is no longer “late stack/sysvar corruption.” It is the
natural native job/scheduler handoff during pre-`AMOSL.INI` filesystem /
command-file work: why `JOBCUR` stays zero in that loop instead of
transitioning to the next command-file/file-load stage and the first
`AMOSL.INI` read.
