# Native Boot and Serial Findings

Date: 2026-03-07
Updated: 2026-03-10

This note records what has been established about the AM-178 boot ROM self-test,
the native `AMOSL.MON` boot path, and the serial-related hardware interfaces.
The goal is to separate measured behavior from assumptions. Earlier work often
treated all early serial-looking traffic as "ACIA output." That is not a safe
interpretation.

## Scope and Evidence

Primary evidence used here:

- Native boot integration tests under `tests/integration/`
- Raw bus traces captured from the emulator while running the normal ROM boot
- Raw bus traces captured from ROM self-test mode (`DIP = 0x2A`)
- `images/AMOS_1-3_Boot_OS.img`
- `docs/AMOS/DSM-00002-03-A00-AMOS_System_Operators_Guide_to_the_System_Initialization_Command_File.PDF`
- `docs/DIAGNOSTICS/DSM-00156-02-A00-System Self Test User's Guide-Ver2.0+.PDF`
- `AM1200Schematics.pdf`
- `AM1000Schematics.pdf`

Important rule: statements below are either marked as proven by current traces
or framed as interpretations. Do not collapse those into one category.

## Current Native Boot Status

What is proven on the native path today:

- The ROM executes natively through the normal `cpu.step()` / `bus.tick()`
  loop.
- The ROM performs real SASI-backed disk reads.
- `AMOSL.MON` is loaded by the native ROM/SASI path.
- Control is handed off into loaded monitor code.
- The observed LED sequence is `06, 0B, 00, 0E, 0F, 00`.
- The native handoff test reaches post-sizing `PC = 0x00811C`.

What is not yet true:

- Native boot does not yet reach `AMOSL.INI` before later low-memory
  serial/interface activity begins.
- Native boot does not yet prove a prompt or a valid `TRMDEF`-driven terminal
  session.

Relevant tests:

- `tests/integration/test_boot_native_disk_read.py`
- `tests/integration/test_boot_native_handoff.py`
- `tests/integration/test_boot_native_amosl_ini.py`
- `tests/integration/test_boot_native_monitor_register_stages.py`

## Boot Image and `AMOSL.INI`

The current native integration path uses:

- `images/AMOS_1-3_Boot_OS.img`

Key `AMOSL.INI` facts on that image:

- It starts with `:T`
- It defines `JOBS 5`
- It defines terminals with:
  - `TRMDEF TRM1,AM1000=0:19200,WYSE,100,100,100`
  - `TRMDEF TRM2,AM1000=2:9600,WYSE,100,100,100`
- It later executes `VER`, `PARITY`, `DEVTBL`, `BITMAP`, and other startup
  commands

Implication:

- Meaningful terminal-visible startup output should not be assumed before
  `AMOSL.INI` is read and the first `TRMDEF` has been processed.
- Any earlier byte stream is not, by itself, evidence of a valid AMOS console.

## Native `AMOSL.MON` Findings

### Proven: `AMOSL.MON` executes under emulation

The native handoff path is not Python loading `AMOSL.MON` by filename into RAM.
The ROM reads sectors through the emulated disk path, places monitor code in
RAM, and then the emulated 68010 executes it.

### Proven: there are at least two distinct monitor-stage register access paths

The native monitor path does not touch one serial-related address block only.
Two stages are visible:

1. High-memory stage:
   - first observed writes at `PC = 0x0082B6`, `0x0082BC`, `0x0082C2`
   - writes `0x03` to:
     - `0xFFFE20`
     - `0xFFFE24`
     - `0xFFFE30`

2. Later low-memory stage:
   - first observed accesses begin at `PC = 0x006B6A`
   - they touch only:
     - `0xFFFFC8`
     - `0xFFFFC9`

This is tracked directly by:

- `tests/integration/test_boot_native_monitor_register_stages.py`

### Proven: the low-memory `0xFFFFC8/0xFFFFC9` path is explicit

At `PC = 0x006B6A`, live CPU state includes:

- `A5 = 0xFFFF_FFC8`

So the low-memory monitor path is explicitly addressing that alias/interface
location. This is not a false decode from some unrelated device.

### Proven: the low-memory path is not emitting a text banner

The first low-memory write sequence is:

- read `0xFFFFC8`
- write `0x00` to `0xFFFFC8`
- write `0x00` to `0xFFFFC9`
- write `0x01` to `0xFFFFC8`
- write `0x11` to `0xFFFFC8`
- then poll `0xFFFFC8`

After that, writes to `0xFFFFC9` are sourced from low RAM through `A1`,
starting around `0x006FFA`.

Observed payload behavior:

- first pass: NUL bytes from a zero-filled buffer
- later pass: raw low-memory bytes such as `0x28`, `0x00`, `0x01`

Conclusion:

- this activity is not a validated banner or terminal trace
- it is some low-level interface path that writes raw memory bytes

Interpretation:

- this may include interface initialization and probing
- it is not safe to describe it as meaningful terminal output

## ROM Self-Test Findings

Relevant tests:

- `tests/integration/test_selftest_serial_window.py`
- `tests/integration/test_selftest_space_match.py`
- `tests/integration/test_selftest_header_output.py`
- `tests/integration/test_selftest_fffe28_placeholder.py`

### Proven: self-test uses the main serial block, not the `HW.SER` alias

During the first `LED = 5B` baud-detect window, the ROM touches:

- `0xFFFE20`
- `0xFFFE22`
- `0xFFFE24`
- `0xFFFE26`
- `0xFFFE28`
- `0xFFFE30`
- `0xFFFE32`

It does not touch:

- `0xFFFFC8`
- `0xFFFFC9`

This means the ROM self-test baud-detect path is not driven by the alias path
currently labeled `HW.SER` in the emulator spec.

### Proven: `0xFFFE28` participates in self-test setup

Before the ROM polls the three serial-port bases during `LED = 5B`, it writes
the sequence:

- `0x15`
- `0x25`
- `0x45`
- `0x85`

to `0xFFFE28`.

Current status:

- `0xFFFE28` is modeled as a placeholder device only
- its exact hardware identity is still unresolved

### Proven: self-test accepts a space on any one main port

If raw bus-level responses are injected during `LED = 5B` so that one selected
base shows:

- status bit 0 set at `base`
- data byte `0x20` at `base + 2`

then the ROM reaches `LED = B5`.

This has been verified independently for:

- `0xFFFE20`
- `0xFFFE24`
- `0xFFFE30`

The alias path alone (`0xFFFFC8/0xFFFFC9`) cannot drive the ROM to `B5`.

### Proven: first validated self-test output line

After a successful space/baud match, the ROM writes:

- `300 baud detected\r\n`

and it writes that line only to the matched port's data register.

This is the first serial output line that is currently validated from raw bus
behavior.

## Hardware Findings from Schematics

### AM-1200

Relevant sheets from `AM1200Schematics.pdf`:

- sheet 3: I/O decoding
- sheet 7: serial I/O
- sheets 8-9: additional serial I/O
- sheet 15: separate communications logic

Important observations:

1. Sheet 7 is real serial I/O with discrete `6850` devices.
   - Ports are selected with chip-select lines such as:
     - `S101CSR*`
     - `S102CSR*`
   - These ports sit on the `IOD8..IOD15` data bus.

2. Sheet 3 contains a different split interface decode path.
   - Signals include:
     - `SCMDWR*`
     - `SSTATRD*`
     - `SDATWR*`
     - `SDATRD*`
     - `ESIOCSR*`

3. Sheet 15 shows another communications subsystem using `Z80SIO/2`-style
   logic and separate control selects.

Interpretation:

- the AM-1200 has more than one serial-related interface path
- the ROM self-test path at `0xFFFE20/24/30` fits the direct `6850` hardware
- the low-memory monitor path at `0xFFFFC8/0xFFFFC9` fits the split
  command/status/data style more closely than a plain `6850`

### AM-1000

Relevant sheet from `AM1000Schematics.pdf`:

- sheet 9

Important observation:

- the AM-1000 host-side interface exposes:
  - `DATWRT`
  - `CMDWRT`
  - `DATRD`
  - `STATIN`

This is structurally very close to the AM-1200 sheet-3 split decode:

- `SDATWR*`
- `SCMDWR*`
- `SDATRD*`
- `SSTATRD*`

Interpretation:

- the AM-1200 low-memory monitor path likely preserves an older split
  interface concept that is already visible in the AM-1000 design
- this further weakens the old assumption that `0xFFFFC8/0xFFFFC9` is simply
  "the same 6850 as port 0"

## What This Means for the Emulator

The current single-device ACIA model should not be treated as authoritative for
all early boot behavior.

Current practical conclusions:

1. Do not use pre-`AMOSL.INI` bytes written through `0xFFFFC9` as a progress
   signal for terminal bring-up.

2. Do not assume the ROM self-test and low-memory monitor path are talking to
   the same serial hardware block.

3. Keep the ROM self-test path and the native monitor path separate in both
   debugging and emulation design.

4. Treat `0xFFFE28` as unresolved hardware until its function is established.

5. Treat `0xFFFFC8/0xFFFFC9` as a provisional low-memory serial/interface path,
   not as a proven alias of the self-test `6850` port block.

## Open Questions

1. What exact decode maps `0xFFFFC8/0xFFFFC9` onto the AM-1200 sheet-3 split
   interface signals or a downstream device?

2. What hardware function does `0xFFFE28` perform during self-test setup?

3. What caller enters the low-memory `0x006B68` path, and is it monitor init,
   diagnostics, or an error/report path?

4. What state should be reached after that low-memory interface activity so
   that `AMOSL.INI` is opened and the first `TRMDEF` is executed?

## 2026-03-09 Native Boot Update

The March 7 note above remains valid, but the current native investigation has
now advanced well past the original "low-memory serial activity starts first"
stopping point.

### Proven: masked PTM IRQ behavior was wrong and is fixed

The emulator PTM model had a real interrupt bug:

- the MC6840 model was asserting `_interrupt_pending` on timer underflow even
  when that timer's interrupt-enable bit was clear
- native boot was then taking a spurious PTM interrupt into `$0FA2`
  immediately after the `A060` dispatch path

That bug is now fixed in:

- [timer6840.py](../alphasim/devices/timer6840.py)
- [test_timer6840.py](../tests/devices/test_timer6840.py)

Current verified result:

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/devices/test_timer6840.py`
  passes with `3 passed`

### Proven: native boot now reaches real post-handoff monitor paths

With the PTM fix in place, native boot is no longer blocked in the old
post-handoff timer wait loop. The plain native path now reaches:

- `QUEUEIO`
- the real file-open area around `$3720/$3748`
- `TTYLIN`
- `SCNMOD`
- `SRCH` for `AMOSL.INI`

It also drains `JCB+$20` through the in-memory `AMOSL.INI` bytes on the native
path. That means the investigation is no longer about whether the monitor
survives handoff at all; it does.

What still does **not** happen:

- SASI reads still stop at LBA `3326`
- `AMOSL.INI` on this image starts at LBA `3335`
- native boot still never issues the expected reads into the `AMOSL.INI` area
- `tests/integration/test_boot_native_amosl_ini.py` remains the expected xfail

### Proven: the injected-command harness and native `AMOSL.INI` path are different

The `trace_scnmod.py` terminal-buffer harness can feed real `AMOSL.INI` lines
through COMINT/SCNMOD far enough to process:

- `:T`
- `JOBS 5`
- `JOBALC TOM,TOM2`
- the first `TRMDEF`
- the second `TRMDEF`
- `VER`

But that only proves line execution after `TTYLIN` is artificially supplied.
It does **not** prove that native `AMOSL.MON` autonomously opens and reads
`AMOSL.INI`.

Also important:

- even through the first two `TRMDEF` lines and `VER`, the harness produced
  only `.` prompt characters
- no banner/copyright output appeared in that injected path

### Proven: the low-memory ACIA path contains a real RX-IRQ race

The stale `RX=$00` seen later at `$3752` is not random garbage. The native
traces show:

- the low-memory poll/read loop at `$006C3E-$006C64` generates a second echoed
  `NUL`
- ACIA vector 64 interrupts that loop before the second data-register read at
  `$006C52` can consume it
- the pending `NUL` then survives until the later `$3752` path

Experimental gating of ACIA IRQ delivery only while `PC` is in
`$006C3E-$006C64` proves the race:

- the paired reads complete
- the stale `RDRF=1 RX=$00` condition disappears at `$3752`

No permanent emulator change for that ACIA race is committed yet. Earlier
speculative echo-model changes were reverted.

### Proven: early runnable-job loss is real, but it is not the final blocker

Tracing around the post-handoff scheduler showed:

- `$8196-$81AE` initializes the new JCB
- `$826C-$8278` loads `JCB+$20` with the `AMOSL.INI` bytes/count
- nothing repopulates `JCB+$78`
- `$1228-$1230` then loads the next-job link from `JCB+$78`, clears it, and
  stores that value into `JOBCUR`
- if `JCB+$78` is still zero there, `JOBCUR` becomes zero and execution falls
  into the idle loop

For tracing, forcing `JCB+$78` / `JOBQ` to self-link back to `$7038` keeps the
init job runnable long enough to reach the real `AMOSL.INI` miss path. That
self-link hack is still useful diagnostically, but by itself it is not the
fix; with it in place, native boot still fails before any new disk I/O.

### Proven: `A06C` is just the `SRCH` wrapper

On the native `AMOSL.INI` path:

- `SCNMOD` reaches `$3A70-$3A7C`
- it writes `#$0100` into the descriptor at `$7074`
- clears descriptor `+$24`
- sets `A6=$7074`
- sets `D6=1`
- calls `A06C`

The LINE-A dispatcher then maps `$A06C` directly to handler `$1C30`, which is
`SRCH`. There is no hidden request-building step inside `A06C`.

Inside `SRCH`:

- the raw descriptor in `A6=$7074` is used as-is
- `D6=1` is carried into the search
- `SRCH` loads `JOBCUR`
- calls `A052`
- receives the current job's module chain
- and misses in memory for `AMOSL.INI`

So `A06C` is not the missing promotion step. It is behaving as expected.

### Proven: normal native miss path returns early and polls the raw descriptor

After the `SRCH` miss, native boot goes through:

- `$1CFE -> A080 -> $4982 -> $4A8C...$4AB4 -> $503C`

On the **normal** path:

- `A080` takes the `$4A8C` branch
- descriptor byte `+$01` ends up with bit `0x40` set
- `A080` returns without building a live request block
- `$503C-$5062` polls the raw descriptor at `$7074`
- byte 0 of that descriptor stays zero
- the path returns error `D7=4`
- and no disk reads occur beyond LBA `3326`

This failure happens before the later timer interrupt. The IRQ is not the cause
of the initial miss-path failure.

### Proven: the forced `A060` path still loses the live block immediately

Forcing the descriptor gate open shows a deeper branch:

- `A060` does execute and returns a pointer
- descriptor `+$12` initially becomes `$3E817C`

But the later path then does this:

- `$1D12` calls `A056`
- `A056` walks the current job's module chain and returns its end:
  `$3E8380`
- `$1D14` immediately stores that result into descriptor `+$12`
- this overwrites the earlier `A060` pointer before the later helper family
  uses the field

That is a stronger result than the earlier "the block is ignored later"
interpretation. The original `A060` result is not merely neglected; it is
replaced at once by the module-chain-end pointer.

The forced branch then continues through:

- `$1D18 -> $1D28 -> $551C -> $5594 -> $55AA -> $5692/$56A2 -> $56BC -> $56D2`

Key details on that path:

- the `0104 -> $0498` runtime slot is still zero
- `$55AA` therefore falls through the fallback family
- `$56BC` reads descriptor `+$12`, but by then it contains the overwritten
  value (`$3E8380`), not the earlier `A060` pointer
- `$56D2` still launches `A086` with `A4=A6=$7074`, the raw descriptor

So the current native failure is now best described as:

- `AMOSL.INI` is submitted into the search/load machinery
- the memory miss path does run
- but the path never turns the raw descriptor into a live disk request
- and the one deeper branch that does allocate/prepare state has its result
  overwritten before later use

## 2026-03-10 Native COMINT / TTYIN Update

### Proven: the seeded native path can reach real `TTYIN`

With the now-established narrow seeds in place:

- `D.REC` is forced at `$0050E2` to `#$00000104`
- `JCB+$00` bit 7 is forced at `SCNMOD ($3932)`
- `TCB+$12`, `TCB+$1A`, and `TCB+$1E` are seeded so COMINT can see the staged
  `AMOSL.INI` line

native boot reaches the real `TTYIN ($A072)` path and consumes the staged line
through the normal emulator stepping path.

That means the active blocker has moved beyond:

- `SCNMOD`
- `SRCH`
- the initial `TTYLIN` gate
- and the first character fetch

### Proven: the first post-`TTYIN` timer trip is secondary, not primary

On the seeded native path, the original shape was:

- first `TTYIN`
- immediate trip into `$000FA2`
- then the `$0013D2/$0013F6` error regime

Adding targeted timer suppression at `$001E2C/$001E2E` changes that materially:

- native now re-enters `TTYIN` 11 times
- the path runs much farther through COMINT's line-consumption loop
- but disk reads still stop at LBA `3326`

So the timer trip at `$000FA2` is real noise on this path, but it is not the
primary blocker.

### Proven: the post-`A072` line-editor path is the current corruption point

The most useful stop is now the first post-`TTYIN` rewrite site:

- `$001E2E..$001E4E`
- then `$00295C/$00299C`

Stopping at `$00299C` occurrence `#12` shows:

- COMINT has already returned from `A072`
- the TCB buffer has already shifted once
- `D1` still holds the consumed character
- `$00299C` writes that character back into the TCB buffer

This is the first direct proof of the current corruption:

- `AMOSL.INI` becomes `AOSL.INI`
- then `OAL.INI`
- and later the second native `SCNMOD` sees an interleaved-zero version of the
  same damaged line

So the live failure is no longer "native never sees the line." Native sees it,
then rewrites it incorrectly before real command execution can proceed.

### Proven: `TCB+$1E` and transient `A6=$3F8086` corruption are both real

Two concrete state problems are now measured on the live native path:

- `TCB+$1E` does not naturally advance from `$3E8086`
- after the first `A072` return, transient `A6` is already wrong through the
  post-`A072` editor path as `$3F8086`

The second point matters because by the time `$00295C/$00299C` runs, the bad
`A6` state is already live and the TCB buffer is rewritten from that regime.

Both narrow experiments changed behavior but were not individually sufficient:

- forcing `A6` only at `TTYIN` entry is too late
- forcing `A6` at `$001E2E` changes the path shape but still does not reach new
  disk I/O
- manually advancing `TCB+$1E` at `$001E2E` changes the path shape again, but
  still does not reach new disk I/O

### New: `TCB+$12` is a long on the live native `TTYIN` path

Capstone disassembly of the live native code around `TTYIN` now confirms:

- `$0020D8` is `SUBQ.L #1,$12(A5)`
- `$0020D4` is `SUBQ.L #1,$1A(A5)`

So `TCB+$12` is being consumed as a long, not a word.

That matters because the earlier seeded editor-path repro had been using a
word-sized seed at `TCB+$12`. That produces an artificial native state after
the first consume:

- seeded as word: `TCB+$12 = $000B0000`
- after first `SUBQ.L`: `TCB+$12 = $000AFFFF`

Re-running the same seeded `SCNMOD -> TTYIN` path with a long-sized seed at
`TCB+$12 = $0000000B` changes behavior materially:

- the old `$00299C` occurrence `#12` corruption stop no longer reproduces in
  the same window
- the run instead diverts into a later `$001202 / $001718` regime
- disk reads still do not go beyond LBA `3326`

So the earlier word-sized `TCB+$12` seed is no longer a trustworthy model of
the natural failure. Future seeded `TTYIN` experiments should treat `TCB+$12`
as a long.

### New: the faithful long-seed path now diverges before the old `TTYIN` frontier

A narrower faithful long-seed repro stopped at the first `$001202`:

- `python3 trace_native_cmdfile_jobq.py --pc-seeds-once --max-after-fix=4000000 --stop-pc-occurrence=0x1202:1 --stop-window-before=24 --stop-window-count=48 --seed-desc-long-at-pc=0x50E2:0x0E:0x00000104 --seed-byte-at-pc=0x3932:0x7038:0x80 --seed-long-at-pc=0x3932:0x3E8012:0x0000000B --seed-long-at-pc=0x3932:0x3E801A:0x000000C8 --seed-long-at-pc=0x3932:0x3E801E:0x003E8086 --suppress-timer-at-pc=0x1E2C:5000 --suppress-timer-at-pc=0x1E2E:5000`

That stop is materially earlier than the old word-seeded editor-path repro:

- recent execution is `A03E` at `$006C10`, then `$0011DE -> $0011F6 -> $001718 -> $001202`
- `JCB+$00 = $0010` at the stop
- `desc+12 = $000000`
- `ttyin_emu_hits = 0`
- `timer_pc_suppression_hits = 0`
- `saw_55AA/saw_56BC/saw_56D2 = 0`
- `last_lba = 3326`

But a matching word-seed comparison to the same first `$001202` stop now shows
the same early state:

- the word-seed path also reaches `A03E @ $006C10 -> $0011DE -> $001718 ->
  $001202`
- it has the same `JCB+$00 = $0010`
- it has the same `desc+12 = $000000`
- it also has no `TTYIN` consume and no timer-suppression hit yet

So the corrected long-sized `TCB+$12` model does not change the first shared
frontier. The first word-seed versus long-seed divergence is later than the
initial `$001202` scheduler stop:

- the word-seeded path can still be used to study the old post-`A072` editor
  corruption
- the faithful long-seeded path and the old word-seeded path both pass through
  the same first `A03E` / scheduler state before they later separate

A follow-up faithful long-seed run to the second `$001202` occurrence still
shows the same regime:

- it reaches `$001202` occurrence `#2`
- `last_a086_pc = $006EC4`
- `last_a086_d6 = $FFFF0001`
- `desc+12 = $000000`
- `ttyin_emu_hits = 0`
- `timer_pc_suppression_hits = 0`
- `saw_55AA/saw_56BC/saw_56D2 = 0`
- `last_lba = 3326`

So at least through the second observed `$001202` stop, the faithful long-seed
path is still looping through the same scheduler/wait regime rather than
reaching the later `TTYIN` or request-helper frontier.

A matching word-seed run to `$001202` occurrence `#2` also matches exactly:

- same `last_a086_pc = $006EC4`
- same `last_a086_d6 = $FFFF0001`
- same `desc+12 = $000000`
- same `ttyin_emu_hits = 0`
- same `timer_pc_suppression_hits = 0`
- same `last_lba = 3326`

So the first word-seed versus long-seed divergence is now known to be later
than the second observed `$001202` scheduler stop.

An 8M-step faithful long-seed repro to `$00299C` occurrence `#1` sharpens this
again:

- the faithful long-seed path does eventually reach `TTYLIN @ $00392C`
- it then reaches `$0029D0/$0029F0` and `$00299C` occurrence `#1`
- it does this with `ttyin_emu_hits = 0`
- it does this with `timer_pc_suppression_hits = 0`
- it still has `last_lba = 3326`
- by `$0029F0`, `A1` already contains `OSL.INI...`, so the leading `A` has
  already been lost before the first `$00299C` stop

So the earlier conclusion that the faithful long-seed model diverts away from
the old editor path is no longer strong enough. With a larger window, the
faithful long-seed path reaches the same first editor frontier too.

A matching 8M-step word-seed repro to `$00299C` occurrence `#1` matches that
faithful long-seed state exactly:

- same `TTYLIN @ $00392C`
- same `$0029D0/$0029F0/$00299C` lead-in
- same `A1 = OSL.INI...` by `$0029F0`
- same `JCB+$00 = $0200`
- same `JCB+$20 = $000A`
- same `last_a086_pc = $0056D4`
- same `last_a086_d1 = $00000706`
- same `ttyin_emu_hits = 0`
- same `timer_pc_suppression_hits = 0`

So the first word-seed versus long-seed divergence is now known to be later
than the first shared `$00299C` editor stop.

A state-focused faithful long-seed probe at the first shared `TTYLIN` stop
(`$00392C`, with `--trace-tcb`) adds one more important constraint:

- at `$00390A/$00392C`, `TCB+$12 = $00000000`
- `TCB+$1A = $00000000`
- `TCB+$1E = $000000`
- the TCB buffer at `TCB+$44 = $3E8086` is empty
- but `A2` already contains `AMOSL.INI\r\n`

So by the first shared `TTYLIN` entry, the active staged line is no longer
living in the TCB bookkeeping fields that earlier experiments had focused on.
The line is already in the transient working registers / copy path even though
the TCB bookkeeping has been cleared.

A matching word-seed state probe at that same first shared `TTYLIN` stop shows
the same result:

- same `TCB+$12/+1A/+1E = 0`
- same empty TCB buffer
- same `A2 = AMOSL.INI\r\n`

So the seed-width difference is no longer visible at the first shared
`$00392C` state checkpoint either.

A final narrow tracer update makes the first shared `A` loss more concrete.
With `A0` offset dumps at `$0029D0/$0029F0`, the faithful long-seed path shows:

- at `$0029D0`:
  - `A0+0C = $003E8170`
  - `A0+10 = $0001`
  - `A0+20 = $000B`
- at `$0029F0`:
  - `A0+0C = $003E8170`
  - `A0+10 = $0001`
  - `A0+20 = $000A`
  - visible text is already `OSL.INI...`

So the first shared leading-`A` loss is now tied to the `$0029D0/$0029F0`
pointer/count state hanging off `A0`, especially the decrementing `A0+20`
count, not to the older TCB bookkeeping theory.

A matching word-seed run with the same `A0` dump confirms that this state is
shared too:

- same `A0+0C = $003E8170`
- same `A0+10 = $0001`
- same `A0+20 = $000B -> $000A`
- same `OSL.INI...` text by `$0029F0`

So the shared `A0`-managed pointer/count path is now the strongest measured
explanation for the first visible `A` loss.

A deeper faithful long-seed stress probe to `$00299C` occurrence `#12` is now
complete:

- target: `$00299C` occurrence `#12`
- budget: `--max-after-fix=25000000`
- result: it never reached `$00299C` occurrence `#12`
- shared `$0029D0/$0029F0` iterations ran `JCB+$20` from `$000B` down to
  `$0000`
- `[CMD drained]` then hit at `$0029DC`
- the run then went through `SCNMOD 1` at `$003932`
- then `TTYLIN 2` at `$00392C`
- then repeated `IOWAIT` at `$001E22`
- final stop was `pc=$0012B6`, `JCB+$00=$0202`, `JCB+$20=$0000`,
  `last_lba=3326`
- the later log still showed repeated `Restored JOBQ self-link at pc=$001232`

So the deeper faithful long-seed path is not “same path, just slower” to
`$00299C` occurrence `#12`. It drains the shared `A0`-managed count to zero,
leaves the first editor loop, and falls back into the later
`$001E22/$0012B6/$001718` regime instead.

### Current best diagnosis

The current highest-signal shared native frontier is now:

- `A03E` / `IOWAIT` around `$006C10`
- then scheduler/service flow `$0011DE -> $001718 -> $001202`
- then, with a larger window, the first `TTYLIN/$0029D0/$0029F0/$00299C`
  editor sequence

Both the faithful long-seeded and older word-seeded repros reach the same
first stop there with:

- `JCB+$00 = $0010`
- `desc+12 = $000000`
- disk reads still stop at LBA `3326`

The first faithful-vs-word divergence is now isolated later than the first
shared `$00392C` state checkpoint and the first shared `$00299C` editor stop.

With the same deep 25M probe:

- faithful long-seed:
  - drains `JCB+$20` to `$0000`
  - hits `[CMD drained]` at `$0029DC`
  - goes through `SCNMOD 1`, `TTYLIN 2`, repeated `IOWAIT @ $001E22`
  - finishes in the `$0012B6/$001718` regime
- word seed:
  - also drains `JCB+$20` to `$0000`
  - but then reaches `TTYIN 1 @ $001E2C`
  - with `timer_pc_suppression_hits = 2`
  - and reaches `$00299C` occurrence `#12`
  - carrying the old artificial state again:
    - `A6 = $3F8086`
    - `D6 = $000AFFFF`
    - `D1 = $00000041`

So the seed-width difference does matter, but only after the shared
`JCB+$20 -> 0` transition and the post-drain `TTYIN` re-entry. That is now the
highest-signal split between the two seeded models.

A focused state stop at the word-seeded post-drain `TTYIN 1 @ $001E2C`
explains why that side re-enters the old editor path:

- `TCB+$12 = $000B0000`
- `TCB+$1A = $000000C8`
- `TCB+$1E = $3E8086`
- `TCB+$44 = $3E8086`
- the TCB buffer still contains `AMOSL.INI\r\n`
- `D6` is already `$000B0000` at the `A072` entry

So on the word-seeded side, the artificial long-sized count state is already
live at the post-drain `TTYIN` re-entry. The later `D6=$000AFFFF` corruption is
not being invented from nothing at `$001E2E`; it is descending directly from
the word-seeded `TCB+$12` state that survived into the second `TTYIN` cycle.

A tighter stop at `$0020EA` now pins down the transform itself. On the
word-seeded side, the second `TTYIN` loop reaches:

- `$0020D8`: decrement `TCB+$12`
- `$0020E4/$0020E6`: repeated byte copy loop
- `$0020EA`: exit with `D6=$000AFFFF`, `A6=$3F8086`, `A2=$3F8085`

At that `$0020EA` stop:

- `TCB+$12 = $000AFFFF`
- `TCB+$1E = $3E8086`
- both current and buffer text are already `MOSL.INI\r\n`
- the recent execution window shows the low word walking
  `$000F -> $0000 -> $FFFF` before the loop exits

So the old `D6=$000AFFFF` / `A6=$3F8086` state is not created at `$00299C`.
It is already established inside the post-drain second `TTYIN` copy loop by
`$0020D8..$0020EA`.

The faithful long-seed side now has a matching stop at `$001E22`, and it makes
the split much more concrete. After `[CMD drained] -> SCNMOD 1 -> TTYLIN 2`,
the faithful run arrives at:

- `TCB+$12 = $0000000B`
- `TCB+$1A = $000000C8`
- `A2 = $3E8086`
- `D6 = $0000000B` before the branch sequence

The recent execution window then shows:

- `$001E0C`: load `D6` from `TCB+$12`
- `$001E14`: take the low-count path
- `$001E20`: set `D6 = 2`
- `$001E22`: call `IOWAIT ($A03E)`

So the faithful long-seed model does not miss `TTYIN` by accident. It is
deliberately diverted into the low-count `IOWAIT` path because the post-drain
`TCB+$12` value is the natural `11`, not the artificial `$000B0000`.

There is now a direct matching stop on the word-seeded side at `$001E14`:

- `TCB+$12 = $000B0000`
- `TCB+$1A = $000000C8`
- `D6 = $000B0000` at the compare
- the branch at `$001E14` takes the high-count path toward `$001E26/$001E2C`

So the first post-drain split is no longer speculative. It is the count gate at
`$001E0C..$001E14`:

- faithful long seed: natural `11` falls through to `$001E20/$001E22` (`IOWAIT`)
- word seed: artificial `$000B0000` branches to `$001E26/$001E2C` (second `TTYIN`)

A narrow intervention at the branch itself confirms the compare timing. In the
faithful long-seed run, forcing `D6=$000B0000` at `$001E14` is too late:

- `[Force reg @pc] pc=$001E14 ... D6 $0000000B->$000B0000`
- execution still falls through to `$001E20/$001E22`
- `IOWAIT 7` runs with `D6=$00000002`

So the branch outcome is already committed by the compare/CCR state produced at
`$001E10`, not by the register value patched at `$001E14` after that compare.

Moving the same override up to the compare site does redirect the flow. In the
faithful long-seed run:

- `[Force reg @pc] pc=$001E10 ... D6 $0000000B->$000B0000`
- `$001E14` now takes `BHI $001E26`
- the run reaches `TTYIN 1 @ $001E2C`
- `D6` is still `$000B0000` there

So the post-drain split is fully controlled by the compare/CCR state at
`$001E10`. That is now a proven control edge, not just a correlation.

Pushing that compare-forced faithful run forward to `$0020EA` shows something
more important: the second `TTYIN` corruption loop does not require the old
global word-seeded `TCB+$12=$000B0000` state.

With only the compare-site override at `$001E10`, the faithful run reaches
`$0020EA` with:

- `D6 = $0000FFFF`
- `A6 = $3E8091`
- `A2 = $3E8090`
- `TCB+$12 = $0000000A`
- current/buffer text already `MOSL.INI...`

So the minimal causal edge is entering the second `TTYIN` copy loop at all.
The old word-seeded `D6=$000AFFFF` / `A6=$3F8086` shape was only one way of
getting there, not the essential requirement.

A bounded follow-up shows that this still does not break through to new disk
I/O. With the compare-site override at `$001E10`, the run goes:

- `TTYIN 1 @ $001E2C`
- through the `$0020D8..$0020EA` copy/corruption loop
- through `$00299A/$00299C`
- back to `IOWAIT 7 @ $001E22`

At that return-to-`IOWAIT` stop:

- `D1 = $00000041`
- `A6 = $3E8091`
- `A2` points at `OSL.INI...`
- `last_lba` is still `3326`

So the second `TTYIN` loop can be recreated from the compare gate alone, but it
still does not reach any new disk read. That makes it another real control edge
without yet being the root disk-I/O blocker.

A direct stop at the first `$001232` scheduler write changes the ordering
again. On the same seeded baseline, the run hits `$001232` *before* any of the
post-drain `TTYIN` experiments matter:

- `reg_force_pc_hits = 0`
- `last_a086_pc = $006E8E`
- `saw_55aa = 0`, `saw_56BC = 0`, `saw_56D2 = 0`
- `JOBCUR = JOBQ = $7038`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`

The trace window shows:

- `$00122C`: `MOVE.L (A3),($041C).W` writes `JOBCUR`
- `$001230`: clears through `(A3)+`
- `$001232`: returns

and the tracer immediately has to restore the forced self-link at `$001232`.
So the early scheduler queue write is a separate upstream event, not fallout
from the later compare-forced second `TTYIN` loop.

With `A3` added to the stop dump, the first `$001232` stop now shows:

- `A3 = $70B4` at the stop
- `A3 bytes = 00 00 00 00 00 00 2C 77 ...`
- `JOBCUR = JOBQ = $7038` still visible in the final milestone

The important inference is that `$001230` has already advanced `A3` past the
consumed queue slot. So the slot actually used by `$00122C` is the previous
long at `$70B0`, not the zero long now visible at `$70B4`. The next direct
check should therefore stop at `$00122C` or `$001230` to confirm the pre-clear
contents of the head slot.

That direct check is now done. At the first `$00122C` stop:

- `A3 = $70B0`
- `A3 bytes = 00 00 38 70 00 00 00 00 ...`
- `JOBCUR = JOBQ = $7038`

So the scheduler is dequeuing the forced self-link from `$70B0`, not a zero.
Then `$001230` clears that head slot and advances to `$70B4`, where the next
long is zero. That explains the repeated tracer restoration at `$001232`: with
only the self-link present, the queue is a one-element list and native code is
legitimately popping it empty.

A watched write range over `$70B0-$70B7` confirms there is no hidden enqueue
behind that head:

- before the first `$001232` stop, there are no native writes to `$70B4-$70B7`
- the only watched native write is the clear of `$70B0-$70B3` at `$001232`
- the tracer then immediately restores `$70B0` to `$7038`

So the next missing state is not in the dequeue logic at `$00122C/$001230`.
It is earlier: native code never populates the next queue/link slot behind the
forced self-link.

This is now directly proven by the watched queue window:

- at the first `$00122C` stop, `A3=$70B0` and `A3 bytes` start with
  `$00007038`
- at the first `$001232` stop, the only watched native writes in `$70B0-$70B7`
  are the zeroing stores to `$70B0-$70B3`
- there are still no native writes to `$70B4-$70B7`

So native dequeue is behaving consistently: it pops the one forced head entry
and finds no successor. The actual missing producer state is whatever should
have populated the next queue link before the first dequeue.

Refinement from a later stop at `$001238`: the producer does run immediately
after `$001232`, but it still enqueues a null successor.

- `$001234` writes `A6` into `$70B4-$70B7`
- `$001236` writes `A7` into `$70B8-$70BB`
- at that stop, `A6 = $000000`
- the watched writes are:
  - `$70B4-$70B7 = 00 00 00 00`
  - `$70B8-$70BB = 00 00 1E 76`

So the earlier “no writes to `$70B4-$70B7`” result was only true up to the
first `$001232` stop. The sharper conclusion is that the queue producer is
executing, but the successor pointer source is already zero by `$001234`.

A narrow forced-register test now confirms that causal edge directly. With
`A6` forced to `$001718` at `$001234`:

- the watched writes become `$70B4-$70B7 = 00 00 18 17`
- `$70B8-$70BB` still receive the same stack value `00 00 1E 76`

So the null successor is not caused by the queue write mechanism itself. It is
caused by `A6` already being zero at `$001234`. That makes the next likely root
cause the preceding `$001232` `MOVE USP,A6` step, or whatever state should have
made `USP` nonzero there.

That intervention is also strong enough to change later scheduler behavior. With
`A6=$001718` forced at `$001234`, the run reaches a second `$00122C`:

- `A3 = $70B0`
- `A3 bytes = 00 00 38 70 00 00 18 17 00 00 1E 76 ...`
- the watch log shows later writes at `$001378/$00137C` restoring `$70B0` to
  `$7038`
- `last_a086_pc` advances to `$006EC4`

So a nonzero successor pointer at `$001234` is not cosmetic; it changes the
queue/scheduler evolution enough to reach a second dequeue. But it is still not
sufficient on its own: `last_lba` remains `3326`, and the run still does not
reach the later `55AA/56BC/56D2` native miss path.

An independent cross-check with [trace_native_jobq_loss.py](../trace_native_jobq_loss.py)
pushes that one step earlier. On the raw native handoff:

- at the handoff milestone (`pc=$00811C`), `JOBCUR = JOBQ = 0`
- at `$00819A`, native writes `JOBCUR = $7038`
- at `$0081AE`, native writes zero into `JCB+$78 / JOBQ`

So native init is not just "failing to enqueue later." It explicitly zeros the
queue-link field at `$0081AE`, and nothing in the observed window restores it.
That is now the highest-signal upstream producer-side edge.

One tighter stop now pins down the later null enqueue directly. A raw stop at
`$001232` with the updated tracer shows:

- `op = $4E6E`
- `A6 = $001718`
- `USP = $00000000`
- `A3 = $0070B4`
- `JOBQ = JOBCUR = $7038`

So right before the later `MOVE.L A6,(A3)+` at `$001234`, the native path is
about to execute `MOVE USP,A6` with a zero `USP`. That explains the observed
`$70B4-$70B7 = 0` write without needing any new queue theory: the producer is
running, but it is sourcing its successor pointer from an already-zero `USP`.

That hypothesis now has a direct positive check. Forcing `USP=$001718` only at
`$001232` with `--watch-write-range=0x70B0:12 --stop-pc=0x1238` yields:

- `[Force reg @pc] pc=$001232 ... USP $00000000->$00001718`
- at `$001236`, `$70B4-$70B7 = 00 00 18 17`
- at the stop, `A6 = $001718` and `USP = $00001718`

So the later non-null successor write can be reproduced by fixing `USP` at the
`MOVE USP,A6` edge itself. The earlier `A6@$001234` workaround was hitting the
same causal edge one instruction later.

Carrying that same `USP@$001232` repair forward to the second dequeue shows the
same scheduler evolution as the older `A6@$001234` workaround:

- second stop reaches `$00122C` occurrence `#2`
- `A3 bytes = 00 00 38 70 00 00 18 17 00 00 1E 76 ...`
- watch log shows the same later `$001378/$00137C` writes restoring `$70B0`
  to `$7038`
- `last_a086_pc` advances to `$006EC4`
- `last_lba` still remains `3326`
- `saw_55AA/saw_56BC/saw_56D2` still stay zero

So fixing `USP` at `$001232` is enough to reproduce the earlier non-null queue
successor path, but it is still not sufficient to break the native disk-read
ceiling. The next real question is no longer "does `USP` matter?" It does. The
next question is why native `USP` is zero there in the first place.

The new `--trace-usp-history` probe makes that provenance question sharper.
On the natural path to the first failing dequeue:

- there are no `USP` history changes at all before `$001232`
- there are no `SSP` history changes or supervisor-bit flips in that window
- the first observed `MOVE USP` instruction is exactly the failing
  `MOVE USP,A6` at `$001232`
- at that pre-step point, `A6=$001718`, `USP=$00000000`, `SSP=$00032400`

So the native path is not "initializing `USP` wrong and then corrupting it
later" anywhere in the observed pre-`$001232` window. In the current model,
`USP` simply remains zero all the way to the first queue-dequeue use site.

One reset-style experiment narrows that further. Seeding `USP=$00032400`
once at the first post-handoff PC (`$006B7A`) produces a natural non-null
enqueue without touching the later scheduler registers directly:

- `[Force reg @pc] pc=$006B7A ... USP $00000000->$00032400`
- at `$001236`, physical bytes written are `03 00 00 24`, which read back as
  logical long `$00032400` on the Alpha Micro swapped bus
- the run reaches the second `$00122C` dequeue
- `last_a086_pc` advances to `$006EC4`
- `last_lba` still remains `3326`

So a reset-style nonzero `USP` is also sufficient to unblock the first null
enqueue. But it is still not sufficient to reach new disk I/O, and it does not
reproduce the older accidental `$001718` successor value. That makes the
earlier direct `A6@$001234` workaround look even more like a one-instruction
hack rather than the true semantic value the ROM expects.

One longer reset-style run is the first real upstream step forward. With only
`USP=$00032400` seeded once at `$006B7A`, stopping later at `$0013D2`
(`MOVE USP,A6`) shows:

- the run naturally reaches `SRCH` on `AMOSL.INI` again at `$001C30`
- it later reaches the deeper late path with `saw_56BC=1` and `saw_56D2=1`
- `last_a086_pc` advances to `$0056D4`
- by the later `$0013D2` stop, `USP` is no longer `$00032400`; it has become
  `$00007AC2`
- `last_lba` still remains `3326`

So an early nonzero `USP` is not the final boot fix, but it is enough to move
the natural native run back into the later `AMOSL.INI` miss/request family
without any of the old direct `$001232/$001234` hacks. That makes the
reset/stack model a much stronger upstream lever than the previous one-step
queue workaround.

The strongest combined late-path run is now also complete. Using:

- early `USP=$00032400` seed at `$006B7A`
- forced `A060` gate
- preserved `desc+12` across `$001D14`
- `A086` promoted and primed to `desc+12`

the run reaches the full deeper miss/request family:

- `saw_55AA=1`, `saw_56BC=1`, `saw_56D2=1`
- `desc+12 = a060_block = $3E817C`
- `last_a086_pc = $0056D4`
- `last_a086_a4 = last_a086_a6 = last_a086_target = $3E817C`
- `last_a086_d1 = $00000104`

And it still fails in the same broad way:

- no reads beyond LBA `3326`
- final loop is still `$0013F2/$0013F6/$0013F8/$0013FA`
- final `USP = $00007AC2`

So the remaining blocker is now even narrower than "late request promotion."
This combined run proves that getting back into the late `A060/$56D4` family
with the preserved live block is still not enough. The next likely divergence
is the later scheduler / runnable-state loop around `$0013D2/$0013FA`,
especially whatever changes `USP` from `$00032400` to `$00007AC2`.

That `USP` transition is now isolated exactly. A targeted
`--trace-usp-history` run on the simpler early-`USP` baseline shows:

- repeated early scheduler cycles keep `USP = $00032400`
- at `pc=$003740`, opcode `$4E66` executes `MOVE A6,USP`
- at that moment, `A6 = $00007AC2`
- immediately after, `USP` changes `$00032400 -> $00007AC2`
- later `$0013D2` consumes that new value via `MOVE USP,A6`

So the current highest-signal native question is no longer "where did `USP`
go?" It is: why does the path at `$003740` load `A6=$00007AC2` and commit that
into `USP`?

That direct stop now has one tighter negative result too. A watched run with:

```bash
python3 trace_native_cmdfile_jobq.py \
  --pc-seeds-once \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --watch-write-range=0x7038:8 \
  --stop-pc=0x3740 \
  --stop-window-before=32 \
  --stop-window-count=48
```

still reaches the same late point:

- `pc = $003740`
- `A6 = $007AC2`
- `USP = $00032400`
- `last_lba = 3326`

But within the watched `$7038:8` window, the only writes before that stop are
to `+$00` and `+$02/+03`. No writes to `JCB+$04` / `$703C` appear in that
captured window. So the next sharp question is no longer just "what value sits
at `4($7038)`?" It is "where was `$703C` populated before this late path, or is
that slot coming from preexisting scheduler state outside the watched window?"

The follow-up stop at the load itself answers the first half of that. Running:

```bash
python3 trace_native_cmdfile_jobq.py \
  --pc-seeds-once \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --stop-pc=0x373C \
  --stop-window-before=32 \
  --stop-window-count=48
```

reaches `pc=$00373C` with `A6=$007038`, and the stop dump shows:

- `A6 bytes = 10 00 01 00 00 00 C2 7A ...`
- so `4(A6)` already contains the long `$00007AC2`
- `last_lba` is still `3326`

So the late `MOVEA.L 4(A6),A6` is not synthesizing `$7AC2`; it is reading a
real pointer already sitting in `JCB+$04`. The next question is therefore
pure provenance: was `$703C` already `$7AC2` at reset/early handoff, or did it
arrive through some write path the current byte-write watch does not cover?

That provenance is now resolved too. An initial snapshot with:

```bash
python3 trace_native_cmdfile_jobq.py \
  --force-reg-at-pc=0x6B7A:a6:0x7038 \
  --stop-pc=0x6B7A
```

shows that at the first stopable handoff PC (`$006B7A`), the live JCB bytes are
already:

- `A6 bytes = 00 00 01 00 00 00 C2 7A ...`
- so `JCB+$04 / $703C = $00007AC2` immediately at handoff
- `USP = $00000000`
- `last_lba = 3326`

So `$703C = $7AC2` is not a late corruption. It is part of the baseline native
state already present at handoff. The bug has therefore shifted again: either
that baseline pointer is supposed to be consumed differently later, or the real
loss is whatever state makes the later `$003720..$003740` path pick up and
commit that baseline `$703C` value into `USP`.

The pointed object is now characterized too. A second initial snapshot with:

```bash
python3 trace_native_cmdfile_jobq.py \
  --force-reg-at-pc=0x6B7A:a6:0x7AC2 \
  --stop-pc=0x6B7A
```

shows:

- `A6 = $007AC2`
- `A6 bytes = 00 00 00 00 00 00 00 00 ...`
- `USP = $00000000`
- `last_lba = 3326`

So baseline native state has `JCB+$04 = $7AC2`, and `$7AC2` itself is a zeroed
block at handoff. That makes the later `$00373C/$003740` sequence much more
suspicious: it is not switching to a live runnable frame, it is switching to a
pointer whose current target is empty memory.

There is now a direct positive intervention on that edge. Running:

```bash
python3 trace_native_cmdfile_jobq.py \
  --pc-seeds-once \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --seed-long-at-pc=0x373C:0x703C:0x00032400 \
  --stop-pc=0x3740 \
  --stop-window-before=32 \
  --stop-window-count=48
```

produces:

- `[Seed long @pc] pc=$00373C ... addr=$00703C $00007AC2->$00032400`
- stop at `pc=$003740`
- `A6 = $00032400` instead of `$00007AC2`
- `USP = $00032400`
- `last_lba = 3326`

So `JCB+$04` is a real causal input to the later bad `USP` transition. The next
question is whether carrying that same fix forward changes the eventual
`$0013D2/$0013FA` loop or disk-I/O ceiling, or whether it only repairs the
local transition.

That longer carry test is now done, and it is a negative result. With:

```bash
python3 trace_native_cmdfile_jobq.py \
  --pc-seeds-once \
  --max-after-fix=8000000 \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --seed-long-at-pc=0x373C:0x703C:0x00032400
```

the run still:

- drains the command file
- reaches `SCNMOD 1`
- rebuilds `AMOSL INI` in the name descriptor
- reaches `$001C30`, `$0056D4`, and later `$000FA2`
- ends in the same `$0013F2/$0013F6/$0013F8/$0013FA` loop
- never reads past LBA `3326`

The key change is local but real: by the later `$0013D2` stop, `USP` is still
`$00032400`, not `$00007AC2`. So seeding `JCB+$04` repairs the bad `USP`
handoff, but that repair alone is not sufficient to change the final failure
regime or produce new disk I/O.

One more cheap discriminator is now ruled out too. Adding:

```bash
--suppress-timer-at-pc=0x0FA2:5000
```

to that same carried-forward `$703C` repair does fire exactly once at the late
interrupt (`timer_pc_suppression_hits=1`), but the run still:

- reaches `$000FA2`
- reaches the later `$0013D2`
- ends at the same `$0013FA` loop
- never reads past LBA `3326`

So the late PTM interrupt at `$000FA2` is not the remaining blocker, even after
the `JCB+$04 -> USP` handoff is repaired.

The strongest integrated negative result is now updated too. Combining:

```bash
python3 trace_native_cmdfile_jobq.py \
  --pc-seeds-once \
  --max-after-fix=8000000 \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --seed-long-at-pc=0x373C:0x703C:0x00032400 \
  --force-a060-gate \
  --suppress-timer-after-a060=5000 \
  --preserve-desc12-at-1d14 \
  --promote-a086-to-desc12 \
  --prime-a086-target-from-desc
```

still ends at the same disk ceiling:

- `last_lba = 3326`
- final `pc = $0013F2`
- `USP = $00032400`
- `desc12 = $3E817C`
- `a060_block = $3E817C`
- `preserve_hits = 1`
- `a086_promotions = 1`
- `a086_primes = 1`
- `last_a086_pc = $0056D4`
- `last_a086_a4 = last_a086_a6 = $3E817C`
- `last_a086_d1 = $00000104`
- `saw_55aa = saw_56bc = saw_56d2 = 1`

So the stack of all validated local repairs still is not sufficient. The native
run now reaches the repaired late request path with the expected promoted block
and still fails before any new disk read. The next frontier is therefore below
or after that late `A086` path, not in `USP`, `JCB+$04`, or descriptor
promotion alone.

That next frontier is now narrowed one layer further. A targeted trace of the
same strongest repaired variant with `--trace-a086-occurrence=7` and a stop at
`$000FA2` shows the repaired late `A086` call taking this deterministic post
path:

- `$0056D4: A086` with `A4=A6=$3E817C`, `D1=$00000104`, `D6=$00001C03`
- return through `$004982..$0049DE`
- branch to `$004A26`
- jump through `$004A2A -> $004D86`
- then `$004D8C -> $004D94 -> $004D98 -> $004D9C -> $004DA0 -> $004DA2 -> $004DA4 -> $004DAA -> $004DB2`
- and only after that does execution land at the late `$000FA2` path

Within that traced window there is no `A0D0` service at all. So the next best
native discriminator is no longer "what happens at late `A086` entry?" It is
"which branch condition in the `$0049DE/$004DAA/$004DB2` ladder sends the
repaired path into `$000FA2` instead of a real loader/request continuation?"

That branch condition is now decoded. A direct disassembly stop at `$004DAA`
shows the relevant ladder:

- `$004D9C: MOVE.L 4(A1),D7`
- `$004DA2: BMI $004DAC`
- `$004DA4: ANDI.L #$00000200,D7`
- `$004DAA: BNE $004DB2`

In the traced repaired late path, `D7` arrives at `$004DAA` as `$00000200`, so
the `BNE $004DB2` branch is taken. That makes the next clean experiment
explicit: force this masked `D7` value to zero on the repaired late path and
see whether falling through past `$004DAA` changes the final control regime or
disk-I/O ceiling.

That branch override is now tested and is negative. A coarse variant that keeps
the repaired `USP` / `$703C` / promoted-`A086` stack but forces:

```bash
--force-reg-at-pc=0x4DAA:d7:0x00000000
```

at every `$004DAA` occurrence still ends at:

- `last_lba = 3326`
- final `pc = $0013F2`
- `USP = $00032400`
- `desc12 = a060_block = $3E817C`
- `saw_55aa = saw_56bc = saw_56d2 = 1`

So the `$004DAA` `BNE $004DB2` branch is real, but clearing its masked `D7`
input is not sufficient to change the final native failure regime. The next
branch frontier is therefore lower still, most likely inside the `$004DB2..`
continuation or the later path that still reaches `$000FA2`.

One correction and one stronger result tighten that further:

- forcing `D7` at `$004DAA` was not a true branch override, because `BNE` uses
  CCR flags already set by the prior `ANDI`
- forcing `D7=0` at `$004DA4` is the correct control test

With the repaired stack plus:

```bash
--force-reg-at-pc=0x4DA4:d7:0x00000000
--trace-a086-occurrence=7
--stop-pc=0x0FA2
```

the late repaired path changes exactly as expected:

- post step 46: `$004DA4` with forced `D7=$00000000`
- post step 47: `$004DAA` with `D7=$00000000`
- post step 48: `$004DAC` instead of `$004DB2`

So the `$004DAA` branch is definitely a real causal edge. But it still is not
the final one: even with that fallthrough forced, the run still reaches
`$000FA2` and still has `last_lba=3326`. The next frontier is therefore below
the branch itself, in the `$004DAC..` continuation that still converges on the
late exception/interrupt path.

The next stop attempt narrows that continuation too. A post-drain stop request
at `$004E46` never triggers on the forced-fallthrough variant. Instead, the log
shows the path reappearing at:

- `$0054D0` with `A4=$3E817C`, `A6=$004D86`
- then `$005140` still with `A6=$004D86`
- then the same late `$000FA2 -> $0013D2/$0013FA` regime

So the `$004DAC..` continuation does not simply return through the obvious
`$004E46` epilogue on this path. It dispatches back into the later
`$0054D0/$005140` service chain, and that alternate entry still is not enough
to produce new disk I/O.

A direct post-drain stop at `$005140` on that same forced-fallthrough variant
now shows the dispatch more concretely. The recent execution window is:

- `$0054E0 -> $0054EA -> $0054EE -> $0054F2 -> $0054F4 -> $0054F8`
- `$005508 -> $00550C -> $005510 -> $005518 -> $00551A`
- then back into `$0050E2 .. $0050FC`
- and `BCS $00510A` is taken again at `$0050FC`
- then the path continues through `$00510A .. $005140`

The key state at that stop is:

- `A6 = $004D86`
- `D1 = $00000200`
- `D6 = $00001C03`
- `desc12 = $3E817C`
- `last_lba = 3326`

So the forced `$004DA4` fallthrough does not create a wholly new late path. It
re-enters the already-known `$0054D0/$0050E2/$0050FC` bridge logic with
different `A6`, and that bridge still takes the old `BCS $00510A` route. That
makes the next integrated experiment clear: re-test the old `$0050FC` bridge
override on top of the repaired late path, not in isolation.

That integrated bridge test is now complete, and it is another strong negative.
Combining the repaired late path with:

```bash
--force-reg-at-pc=0x4DA4:d7:0x00000000
--seed-desc-long-at-pc=0x50E2:0x0E:0x00000104
```

still ends at:

- `last_lba = 3326`
- final `pc = $0013FA`
- `USP = $00032400`
- `desc12 = a060_block = $3E817C`
- `desc+0E = $00000104` by the late path
- `saw_55aa = saw_56bc = saw_56d2 = 1`

So the old `$0050FC` bridge edge is not sufficient even when re-tested on top
of the repaired late `A086`/`$004DA4` path. The next frontier is again lower,
inside the re-entered `$005140..` service chain that still converges on
`$000FA2`.

A direct stop at `$005140` on that full integrated variant shows why the bridge
seed was ineffective in practice. Even with:

```bash
--seed-desc-long-at-pc=0x50E2:0x0E:0x00000104
```

the recent execution is still effectively the same:

- `$0054E0 .. $00551A`
- `$0050E2 .. $0050FC`
- `BCS $00510A` still taken at `$0050FC`
- then `$00510A .. $005140`

At that stop:

- `desc+0E` has been seeded to the late image visible as `... 04 01 3E 00 ...`
- but `D7` in the recent-exec ladder still reaches `$0050F8/$0050FC` as `1`
- and `A6` is still `$004D86`

So on this repaired late path, seeding the descriptor field alone does not
actually change the compare/branch state consumed at `$0050FC`. The next clean
test is therefore to override the live `D7` value at the compare site itself,
not to seed the descriptor earlier.

That direct compare override is now complete, and it is the strongest new
control edge in this slice. On top of the repaired late path, forcing:

```bash
--force-reg-at-pc=0x4DA4:d7:0x00000000
--force-reg-at-pc=0x50F8:d7:0x00000104
```

changes the native run materially:

- it no longer returns to the old `$0013F2/$0013FA` regime
- it finishes at `pc = $0012EE`
- LED history now includes `12`
- the hottest late PCs become `$001DFC .. $001E10`
- `USP` remains `$00032400`
- `desc12` remains the preserved/promoted `A060` block at `$3E817C`

But even with that live compare override, disk reads still stop at
`last_lba = 3326`.

So the current lowest verified control edge is the live compare input at
`$0050F8/$0050FC`, not just the earlier descriptor field that sometimes feeds
it. The next frontier is the new late loop around `$001DFC .. $001E10`.

That next stop is now complete. On the repaired late path plus:

```bash
--force-reg-at-pc=0x4DA4:d7:0x00000000
--force-reg-at-pc=0x50F8:d7:0x00000104
--stop-pc=0x1DFC
```

the run reaches a real late loop at `$001DFC`, but it is not a new disk-loader
path. It is a post-drain `TTYLIN`/`IOWAIT` loop:

- final stop: `pc = $001DFC`
- `JCB+$00 = $0200`
- `JCB+$20 = $0000`
- `USP = $00032400`
- `last_lba = 3326`
- `A2` still contains `AMOSL.INI`

The recent-execution ladder is:

- `SCNMOD/TTYLIN` return at `$00390E/$00392C`
- then `$0028CC .. $002958`
- then `$001DF8 .. $001DFC`

The disassembly at that stop makes the loop concrete:

- `$001DFC`: load `A0` from `($041C).W`
- `$001E04`: load `A5` from `56(A0)`
- `$001E0C .. $001E1E`: compare/count gate
- `$001E20`: `MOVEQ #2,D6`
- `$001E22`: `A03E`
- `$001E24`: branch back to `$001DFC`
- `$001E2C`: `A072` is still the alternate path if the compare gate passes

So the `$0050F8` override does change the late regime, but it only lands in a
natural post-command-drain `TTYLIN` retry loop. The next clean question is the
live compare at `$001E10`: what exact values in the `A5`-anchored structure
keep this path in `A03E`/`IOWAIT` instead of re-entering `A072`.

That compare stop is now measured too. On the same repaired path, stopping at
`$001E10` shows:

- final stop: `pc = $001E10`
- `A0 = $007038`
- `A5 = $3E8000`
- `A2 = AMOSL.INI`
- `JCB+$20 = $0000`
- `last_lba = 3326`

The recent execution is the key:

- `$001E04`: `MOVEA.L 56(A0),A5`
- `$001E08`: `MOVE.L A5,D7` with `D7 = $3E8000`
- `$001E0C`: `MOVE.L 18(A5),D6`
- `$001E10`: compare gate with `D6` now equal to `0`

So the late retry decision is not reading `JCB+$20` directly. By the time the
loop reaches `$001E10`, it has already reloaded `D6` from the `A5`-anchored
structure at `$3E8000`, and that live value is `0` on the repaired path. That
explains why the loop keeps selecting the low-count `A03E`/`IOWAIT` side even
after the earlier `$0050F8` override succeeds.

The next clean experiment is therefore direct: override `D6` at `$001E10` on
this repaired path and see whether that takes the alternate `$001E2C: A072`
branch or still converges back to the same no-I/O regime.

That direct override is now proven. On the same repaired path plus:

```bash
--force-reg-at-pc=0x1E10:d6:0x0000043C
--stop-pc=0x1E2C
```

the late loop does take the alternate branch:

- `[Force reg @pc] pc=$001E10 ... D6 $00000000->$0000043C`
- `$001E14: BHI $001E26` is then taken
- `$001E2A: BLS $001E26` is not taken
- the run reaches `$001E2C: A072`

At that stop:

- `pc = $001E2C`
- `A0 = $007038`
- `A5 = $3E8000`
- `A2 = AMOSL.INI`
- `D6 = $0000043C`
- `JCB+$20 = $0000`
- `last_lba = 3326`

So the live `D6` value at `$001E10` is a real causal edge on the repaired
path too. The next question is not whether the branch can be flipped; it can.
The next question is whether carrying that same override forward changes the
overall disk-I/O ceiling, or only swaps one local retry loop for another.

That carry-forward test is now complete too, and it is another strong negative.
Running the repaired path with the same live override:

```bash
--force-reg-at-pc=0x1E10:d6:0x0000043C
```

does materially change late control flow:

- it repeatedly re-enters `TTYLIN`
- then repeatedly reaches `A072` at `$001E2C`
- then repeatedly returns through `SCNMOD`
- LED history becomes `06, 0B, 00, 0E, 0F, 00, 12`

But it still does not change disk I/O:

- `last_lba = 3326`
- final `pc = $0013F2`
- hot late loop is again `$0013F2/$0013F6/$0013F8/$0013FA`

The strongest new clue is below that re-entry. On the repeated post-`A072`
path, `A0DC` at `$003748` now sees:

- `A4 DDB = <invalid $000000>`
- while `A2` still contains `AMOSL.INI`
- and `JCB+$20` is already `0`

So the repaired late path plus forced `D6@$001E10` re-entry is still not
sufficient. The next concrete frontier is the first post-reentry `A0DC`
occurrence where the DDB context has become null.

That `A0DC` stop is now measured directly. Stopping at the first post-reentry
`A0DC` (`$003748` occurrence `#3`) shows that the DDB context is not being
clobbered immediately before the call. It is simply never restored on that
re-entry path.

The recent execution at that stop is:

- `$002C2C .. $002C32`
- `$003988 -> $003A08`
- `$0036F4 -> $0036FC`
- `$003720 -> $003732`
- `$00373C -> $003740`
- `$003742 -> $003748`

At the stop:

- `pc = $003748`
- `A4 = $000000`
- `A6 = $00032400`
- `A2 = OSL.INI`
- `JCB+$00 = $0200`
- `JCB+$20 = $0000`
- `last_lba = 3326`

The relevant disassembly is enough to frame the bug:

- `$0036F4`: load `A6` from `($041C).W`
- `$003720`: alternate branch zeroes/updates JCB-local state
- `$00373C`: load `A6` from `4(A6)`
- `$003740`: move that result into `USP`
- `$003742`: set `D6 = #$FE`
- `$003748`: call `A0DC`

Nothing in that re-entry path reloads `A4` before `A0DC`. So the immediate
next repair to test is narrower than “fix A0DC”: force the missing DDB context
back in at `$003748` and see whether that changes the late outcome.

That repair test is now complete too, and it is another clean negative.
Carrying the repaired path with:

```bash
--force-reg-at-pc=0x1E10:d6:0x0000043C
--force-reg-at-pc=0x3748:a4:0x00007074
```

does restore the DDB context locally:

- every post-reentry `A0DC` now sees `A4 DDB at $007074`
- the run still repeats `TTYLIN -> A072 -> SCNMOD`
- LED history still includes `12`

But it still does not change the global outcome:

- `last_lba = 3326`
- final `pc = $0013F2`
- the hot loop is still `$0013F2/$0013F6/$0013F8/$0013FA`

The strongest local detail is what happens immediately after the forced repair:

- `A4` is forced to `$007074` at `$003748`
- `A0DC` runs with that restored DDB
- then `$003752` clears `A4` back to `0`
- the path still falls through `$00375E`, later reaches `$000FA2`, and then
  returns to the old `$0013F2` loop

So the null-DDB-at-`A0DC` edge is real, but restoring `A4` there is still not
sufficient. The next frontier is below that repair, most likely the
post-`$003752` / `$00375E` handling on the re-entry path.

A direct stop at `$003752` adds one useful constraint and one warning. Using a
raw stop occurrence at `$003752` does not isolate the post-reentry wrapper by
itself. The measured `$003752` occurrence `#3` lands in a different service
context:

- recent execution goes through `$002B28 .. $002B5E`, then `$001B88 .. $001BA8`,
  then `$0037BA/$0037BC`, then `$003752`
- at that stop, `A4 = $3E8170` and `A6 = $3E81E8`
- later mixed-path logs also show another `$003752` with `A4 = $3E81E4`

So `$003752` is definitely part of the post-`A0DC` handling, but simple
occurrence counting there mixes at least two contexts. The next clean repair
test should therefore work at a later unambiguous edge instead of trying to
decode `$003752` in isolation: force `A4` at `$00375E` on top of the existing
re-entry repairs and see whether that changes the late outcome.

That `$00375E` repair test is now complete, and it is a clean negative result.
On top of the existing repaired path (`USP`, `$703C`, `$004DA4`, `$0050F8`,
`$001E10`, `$003748`, forced `A060`, preserved/promoted/primed late `A086`),

```bash
python3 /Volumes/RAID0/repos/am_emulator/trace_native_cmdfile_jobq.py \
  --max-after-fix=8000000 \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --seed-long-at-pc=0x373C:0x703C:0x00032400 \
  --force-reg-at-pc=0x4DA4:d7:0x00000000 \
  --force-reg-at-pc=0x50F8:d7:0x00000104 \
  --force-reg-at-pc=0x1E10:d6:0x0000043C \
  --force-reg-at-pc=0x3748:a4:0x00007074 \
  --force-reg-at-pc=0x375E:a4:0x00007074 \
  --force-a060-gate \
  --suppress-timer-after-a060=5000 \
  --preserve-desc12-at-1d14 \
  --promote-a086-to-desc12 \
  --prime-a086-target-from-desc
```

still ends at the old late loop:

- final `pc = $0013FA`
- `last_lba = 3326`
- hot loop still `$0013F2/$0013F6/$0013F8/$0013FA`
- `last_a086_pc = $0056D4`
- `last_a086_a4 = last_a086_a6 = $3E817C`

The stronger conclusion is that `$00375E` is too broad to use as a clean
re-entry repair site. The same force also fires in unrelated `A052` contexts,
then later mixed `A064` service paths at `$0037B8`, and the run eventually
zeros the JCB name descriptor at `$007074` through the `$001B92/$001B94` copy
path:

- one mixed `$00375E` stop sees `A6 = $004C92`
- another sees `A6 = $3E83F4`
- another sees `A6 = $3E81E8`
- later `A064` arguments include both `$3E81F0` and `$007080`
- the final JCB name descriptor bytes at `$007074` are all zero

So the next frontier is no longer “force `A4` later.” It is to find the first
post-reentry edge that is specific to the intended wrapper path and does not
also perturb the unrelated `$00375E/$0037B8` service contexts.

The next clean stop confirms the shape of that first `$0037B8` site. On the
same repaired path but without the over-broad `$00375E` force, a stop at the
first `$0037B8` occurrence shows:

```bash
python3 /Volumes/RAID0/repos/am_emulator/trace_native_cmdfile_jobq.py \
  --force-reg-at-pc=0x6B7A:usp:0x00032400 \
  --seed-long-at-pc=0x373C:0x703C:0x00032400 \
  --force-reg-at-pc=0x4DA4:d7:0x00000000 \
  --force-reg-at-pc=0x50F8:d7:0x00000104 \
  --force-reg-at-pc=0x1E10:d6:0x0000043C \
  --force-reg-at-pc=0x3748:a4:0x00007074 \
  --force-a060-gate \
  --suppress-timer-after-a060=5000 \
  --preserve-desc12-at-1d14 \
  --promote-a086-to-desc12 \
  --prime-a086-target-from-desc \
  --stop-pc-occurrence=0x37B8:1 \
  --stop-window-before=160 \
  --stop-window-count=224
```

At that stop:

- `pc = $0037B8`, `op = A064`
- `A1 = $3E83F0`
- `A3 = $3E81E4`
- `A6 = $00779E`
- `A064 arg ptr = $3E81F0`
- the first `A064` argument block is all zeroes
- `last_lba = 3326`

The recent execution window is the useful part:

- it is not the raw `$003748` wrapper anymore
- it comes through the internal walk at `$003772 .. $0037B8`
- in that walk, `A4` and `A3` settle on `$3E81E4`
- then `$0037B2/$0037B6` stage the `A064` call

So the first `$0037B8` stop is more specific than the raw `$003752` or forced
`$00375E` probes, but it is still already downstream of the mixed service
path. The immediate next target should therefore be the return branch at
`$0037BC`, to determine whether the zero `A064` argument is the reason the
path falls back toward `$003752`.

That `$0037BC` branch stop is now measured too. On the same repaired path,
stopping at the first `$0037BC` occurrence shows:

- `pc = $0037BC`
- `op = $6794` (`BEQ $003752`)
- `A1 = $3E83F0`
- `A3 = $3E81E4`
- `A6 = $3E83F4`
- `D7 = $FFFFFFFF`
- `last_lba = 3326`

The recent execution window is now even tighter:

- `$002B28 .. $002B5E`
- `$001B88 .. $001BA8`
- `$0037BA`
- `$0037BC`

So the branch site is not returning directly from the raw `$003748` wrapper.
It first comes back through the `$002B` walk and the `$001B88 .. $001BA8`
copy/helper path, then lands on `BEQ $003752` with the same staged zero-arg
context behind it. That makes the next experiment straightforward: seed the
first long at `A064 arg ptr = $3E81F0` to a nonzero value at the call site and
see whether the post-`A064` branch shape changes.

That seed test is now measured too. Seeding the first long of the `A064`
argument block at the call site,

```bash
--pc-seeds-once --seed-long-at-pc=0x37B8:0x3E81F0:0x400E1C03
```

does change the local state materially:

- at `$0037B8`, `A064 arg words` become `400E 1C03 0000 0000 ...`
- at the following `$0037BC` stop:
  - `D0 = $00000000` instead of `$00000016`
  - `JCB+$00 = $0010`
  - `A2 = AMOSL.INI...`
  - `A5 = $3E8000`

But it still reaches the same branch site:

- `pc = $0037BC`
- `op = $6794` (`BEQ $003752`)
- `A1 = $3E83F0`
- `A3 = $3E81E4`
- `A6 = $3E83F4`
- `D7 = $FFFFFFFF`
- `last_lba = 3326`

And the recent execution is still the same structural path:

- `$002B28 .. $002B5E`
- `$001B88 .. $001BA8`
- `$0037BA`
- `$0037BC`

So the zero arg block is a real local contributor, but seeding its first long
is still not sufficient to bypass the `$0037BC` return branch. The next useful
step is to carry that seeded variant forward and see whether the global result
changes even though the branch site is still reached.

That carry-forward run is now complete, and it is the strongest late-path
repair so far. With the same seeded `A064` block carried through to completion,
native now restores a real named `AMOSL INI` descriptor path:

- `JCB name query = 'AMOSL INI'`
- `pc=$001C30` sees `Query RAD50(le)='AMOSL INI'`
- the JCB name descriptor at `$007074` becomes live:
  - `00 41 00 00 FF FF 57 08 A0 78 79 3A ...`
  - `desc+12 = $3E817C`
- `$004A96` naturally clears the gate byte (`$41 -> $01`)
- the repaired late path reaches:
  - `$0055AA`
  - `$0056A6`
  - `$0056D2`
  - `$0056D4: A086`

and that late `A086` now runs on the promoted block itself:

- `last_a086_pre_a4 = last_a086_pre_a6 = $007074`
- `last_a086_a4 = last_a086_a6 = $3E817C`
- `last_a086_a1 = $3E8374`
- `last_a086_target = $3E817C`
- `saw_55aa = saw_56bc = saw_56d2 = 1`

But the global result is still negative:

- final `pc = $0013F8`
- final hot loop still `$0013F2/$0013F6/$0013F8/$0013FA`
- `last_lba = 3326`
- `reached_amosl_ini = 0`

So the remaining native blocker is now below the named-descriptor repair and
below the promoted late `A086` entry. In other words: it is now possible to
rebuild the `AMOSL INI` name path and still fail before any new disk read. The
next useful stop should therefore move below that restored flow, likely at the
first post-`A086` service/return point that leads into the eventual `$000FA2`
interrupt path, rather than back up in the earlier descriptor assembly logic.

That next stop is now measured too. On the same seeded late-path repair,
stopping at the first `$000FA2` occurrence shows:

- `D0 = $00000200`
- `D1 = $00000002`
- `D6 = $00001C03`
- `A2 = $3E817C`
- `JCB name query = 'AMOSL INI'`
- `desc+12 = a060_block = $3E817C`
- `last_lba = 3326`

The recent execution window is the important part:

- it does not jump straight from `$0056D4` to `$000FA2`
- instead it spins first in a tight service loop:
  - `$006B68`
  - `$006B6A`
  - `$006B6E`
- only after repeated iterations of that loop does it land on `$000FA2`

So the remaining late native blocker is now even tighter:

- the restored named `AMOSL INI` path survives all the way through the late
  promoted `A086`
- the failure is now in or immediately after the `$006B68/$006B6A/$006B6E`
  service loop on that promoted block
- `$000FA2` is downstream fallout, not the first late divergence

That makes the next target concrete: stop inside the `$006B68 .. $006B6E` loop
itself and determine what condition never resolves there, because that loop is
the last measured stage before the run falls into the old interrupt/error path.

A direct stop at raw `$006B68` occurrence `#1` adds an important warning. That
stop does not land in the repaired late `AMOSL INI` flow. Instead it lands in
an earlier service context with:

- `JCB name query = ''`
- `desc+12 = $000000`
- `preserve_hits = 0`
- `a086_promotions = 0`
- `a086_primes = 0`

and the recent execution there is an earlier path through `$006A30 .. $006AB8`
before entering `$006B68`.

So raw `$006B68` occurrence counts mix contexts too, just like raw `$003752`
did. The next stop inside that loop needs either a higher occurrence count or
another path-specific anchor that confirms the repaired named `AMOSL INI` state
is already live before interpreting the loop body.

A later path-specific stop at raw `$006B68` occurrence `#1300` now lands in
that repaired context. At that stop:

- `JCB name query = 'AMOSL INI'`
- `desc+12 = a060_block = $3E817C`
- `preserve_hits = 1`
- `a086_promotions = 1`
- `a086_primes = 1`
- `last_lba = 3326`

The loop body also decodes cleanly there:

- `$006B68: MOVE.B (A5),D7`
- `$006B6A: ANDI.B #$02,D7`
- `$006B6E: BNE $006B68`
- `A5 = $FFFFC8`

So the repaired late `AMOSL INI` path is now concretely blocked waiting for
bit 1 of the device/status byte at `$FFFFC8` to clear. The next direct
experiment is narrow: force or seed that polled byte so bit 1 is clear at
`$006B68`, then check whether the run escapes the loop or produces the first
disk read beyond LBA `3326`.

That next direct test is now run too. A one-shot byte seed at
`$006B68: $FFFFC8 <- $00` does break the loop and reaches `$006B70`:

- seed fires at `pc=$006B68` with `$FFFFC8: $14 -> $00`
- the stop lands at `$006B70: MOVE.B #$00,(A5)` with `A5=$FFFFC8`
- the recent execution is still the late service path through
  `$006A3E .. $006AB8 -> $006B68`
- local late-path state is still live there, including `A4=$3E817C`
- `last_lba` is still `3326` at that stop

So bit 1 at `$FFFFC8` is now proven causal: clearing it is sufficient to
escape the tight `$006B68/$006B6A/$006B6E` poll loop. The next direct question
is what the `$006B70 .. $006C18` continuation does in a full run, and whether
that changes the global disk-read ceiling.

That carry-forward run is now measured too, using the same repaired late-path
setup plus the same one-shot `$FFFFC8 <- $00` seed at `$006B68`. It changes the
native path materially:

- the run enters `$006B70 .. $006C10` and hits `IOWAIT @ $006C10`
- `JCB+$00` flips to `$0010` on that path
- the late miss/retry state walks the older alternate sequence again:
  `D1 = $0106`, then `$0202`, then `$0706`
- after that, native returns through `TTYLIN`, re-establishes
  `JCB name query = 'AMOSL INI'`, reaches `$0055AA/$0056A6/$0056D2/$0056D4`,
  and runs late `A086` again on promoted `desc+12 = $3E817C`

But it is still not sufficient globally:

- final state is still `pc=$0013F8`
- `last_lba` is still `3326`
- the late `$000FA2 -> $0013D2/$0013FA` failure regime still returns
- hot PCs still show repeated `$006B68/$006B6A/$006B6E` and repeated
  `$006C42/$006C44/$006C48`

So the next late blocker is no longer just the first `$006B68` bit-1 wait.
The one-shot clear opens the continuation, but the native path still re-enters
later status/poll waits and eventually converges back to the old terminal
failure loop without issuing any new disk reads.

A narrower stop inside that later continuation is now measured too. Forcing
`D7=1` at `$006C48` is sufficient to break the next local wait and reach
`$006C4A`:

- live recent execution there is the tight `$006C42/$006C44/$006C48` loop
- `A4` is still `$3E817C`
- `A5` is still `$FFFFC8`
- the raw status byte seen at `$006C44` is usually `$16`
- after `ANDI.B #$01,D7`, that means the branch at `$006C48` still sees
  `D7=0` and loops

So the continuation after the first `$006B68` escape has a second concrete poll
condition: it is waiting for bit 0 of the same `$FFFFC8` status byte to become
set. Forcing that branch through reaches `$006C4A`, which is immediately
followed by the next wait site at `$006C50` before `$006C52`.

The stronger end-to-end version of that experiment is now measured too:
forcing the whole observed `$006B68 .. $006C5C` status chain through every
time is still not sufficient. With these local repairs in place:

- `$006B6E` forced so the first bit-1 wait always falls through
- `$006C48` forced so the bit-0 wait always falls through
- `$006C50` forced so the bit-2 wait always falls through
- `$006C5C` forced so the final bit-1-clear wait always falls through

the full run still:

- enters `IOWAIT @ $006C10`
- flips `JCB+$00` to `$0010`
- walks the alternate `D1 = $0106 / $0202 / $0706` sequence
- returns through `TTYLIN`
- restores `JCB name query = 'AMOSL INI'`
- reaches `$0055AA/$0056A6/$0056D2/$0056D4`
- runs late promoted `A086` on `desc+12 = $3E817C`
- then still lands on `$000FA2 -> $0013D2/$0013FA`
- and still finishes at `pc=$0013F8`, `last_lba=3326`

So the entire measured `$FFFFC8` status-handshake chain is now proven real but
not globally sufficient. The remaining blocker is lower than that whole
service-loop handshake.

A direct stop at raw `$006E26` on that forced-handshake setup adds the same
warning pattern seen at earlier raw stops: occurrence `#1` is not the repaired
late `AMOSL INI` path. It lands in the earlier blank-query service context
instead, with:

- `JCB name query = ''`
- `D0 = 0`, `D1 = 0`, `D6 = $0000184A`
- `A4 = $3E817C`, `A6 = $00184A`
- branch point `$006E26: BEQ $006E82`

So raw `$006E26` occurrence counts are not a reliable anchor for the repaired
late path either.

The first path-specific stop below the forced handshake is instead the first
`$000FA2` on that same run. That stop shows the strongest current result:

- the repaired late path is live:
  - `JCB name query = 'AMOSL INI'`
  - `desc+12 = a060_block = $3E817C`
  - `preserve_hits = 1`
  - `a086_promotions = 1`
  - `a086_primes = 1`
- but the immediate predecessor path is still the same tight
  `$006B68/$006B6A/$006B6E` loop
- the recent execution window ends:
  - repeated `$006B68/$006B6A/$006B6E`
  - then direct interrupt entry at `$000FA2`
- stop state there is `D0=$00000200`, `D1=$00000002`, `D6=$00001C03`,
  `A2=$3E817C`, `A6=$006C18`

That also corrects the interpretation of the earlier “full forced handshake”
run: because it used `--pc-seeds-once`, the `$006B6E/$006C48/$006C50/$006C5C`
register forces were one-shot too. They changed the first service pass, but the
repaired late `AMOSL INI` path can still re-enter the same `$006B68` loop later
with no force active and then fall into `$000FA2`.

That persistent version is now measured too. A narrow tracer update adds
`--force-reg-at-pc-always=...`, so the known setup hooks can stay one-shot
while the `$FFFFC8` handshake overrides persist across every recurrence. With
those persistent repairs:

- `$006B6E` is forced every time so the bit-1 loop falls through
- `$006C48` is forced every time so the bit-0 loop falls through
- `$006C50` is forced every time so the bit-2 loop falls through
- `$006C5C` is forced every time so the final bit-1-clear loop falls through

and the result is still negative:

- final state is still `pc=$0013F8`
- `last_lba` is still `3326`
- repaired late state is still live at the end:
  - `JCB name query = 'AMOSL INI'`
  - `desc+12 = a060_block = $3E817C`
  - `preserve_hits = 1`
  - `a086_promotions = 1`
  - `a086_primes = 1`
  - `saw_55aa = saw_56bc = saw_56d2 = 1`

So even a persistent repair of the measured `$006B68 .. $006C5C` handshake is
not sufficient. The remaining blocker is now definitively below that whole
status-handshake family.

A path-specific stop at the first persistent-handshake `$000FA2` confirms that
lower framing: even with `--force-reg-at-pc-always` active on the full
observed `$006B68 .. $006C5C` handshake, the repaired late `AMOSL INI` state
is still live at interrupt entry, and recent execution is still dominated by
the same `$006B68/$006B6A/$006B6E` poll family before `$000FA2`.

A direct stop at `$006B70` on that same persistent setup proves the new tracer
hook can drive the branch through to the continuation. The next
highest-yield target is therefore inside the `$006B70 .. $006C18` /
`$006BBE .. $006BFC` continuation itself, not the top-level handshake gate.

The first raw `$006BBE` occurrence on that same persistent setup is not a
reliable repaired-path anchor. It lands in the earlier blank-query service
context with:

- `JCB name query = ''`
- `D1 = $00002000`
- `D6 = $0000184A`
- `A4 = $3E817C`
- `A6 = $00184A`
- `JCB+$20 = $000B`

and its recent execution runs through the earlier `$006AFE .. $00103E`
service path, not the repaired late `AMOSL INI` path. So the next useful stop
inside that continuation has to be a later occurrence, not raw occurrence `#1`.

A later-occurrence probe is now complete too. On the full persistent repaired
run, `$006BBE` never reaches occurrence `#1300`; the run finishes normally
first. The useful measured counts from that full run are:

- `$006B68/$006B6A/$006B6E`: `1374` hits each
- `$006C42/$006C44/$006C48`: `2252` hits each
- `$006BBE/$006BC0/$006BC4`: `611` hits each
- `$006BC6/$006BCA`: `606` hits each

That same run still keeps the repaired late state intact at the end:

- `JCB name query = 'AMOSL INI'`
- `desc+12 = a060_block = $3E817C`
- `preserve_hits = 1`
- `a086_promotions = 1`
- `a086_primes = 1`
- `saw_55aa = saw_56bc = saw_56d2 = 1`
- final `pc = $0013F8`
- `last_lba = 3326`

So the next useful stop inside the `$006BBE .. $006BFC` continuation is now
tighter: the last live `$006BBE` family occurrence, not a guessed much-later
occurrence number.

That last live `$006BBE` occurrence is now measured too. Stopping at
occurrence `#611` lands here:

- `pc = $006BBE`
- `D0 = $00000200`
- `D1 = $00000002`
- `D6 = $0000186A`
- `A4 = $3E817C`
- `A6 = $00186A`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`
- `JCB name query = ''`

and the recent execution is a tight zero-result loop:

- `$006BBE -> $006BC0 -> $006BC4 -> $006BC6 -> $006BCA -> $006BBE`

So the last live `$006BBE` occurrence is still not the repaired late
`AMOSL INI` path. It is the common zero-result service loop. Combined with the
earlier hot-PC counts (`611` hits at `$006BBE` versus `606` at
`$006BC6/$006BCA`), the next useful target is now the rarer taken branch at
`$006BC4 -> $006BD4`, not another stop on the zero-result `$006BBE` loop.

That rare taken branch is now measured too. Stopping at `$006BD4` occurrence
`#5` lands with:

- `D0 = $00000200`
- `D1 = $00000002`
- `D6 = $0000186A`
- `A4 = $3E817C`
- `A6 = $00186A`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`
- `JCB name query = ''`

and the immediate branch condition is now explicit in the recent execution:

- repeated common loop passes still reach `$006BC4` with masked `D7 = 0`
- on the taken pass, `$006BC0` sees raw `D7 = $16`
- then `$006BC4` sees masked `D7 = $02`
- and control branches to `$006BD4`

So the rarer taken path is real, but it is still the same blank-query service
context, not the repaired late `AMOSL INI` path. The next useful target is the
continuation below it, especially `$006D66/$006D6E`, not the branch predicate
itself.

The first direct continuation stop below that rare branch is now resolved too:
`$006D66` occurrence `#1` never occurs on the full persistent repaired run.
The run finishes first with the same repaired late end state:

- `JCB name query = 'AMOSL INI'`
- `desc+12 = a060_block = $3E817C`
- `preserve_hits = 1`
- `a086_promotions = 1`
- `a086_primes = 1`
- `saw_55aa = saw_56bc = saw_56d2 = 1`
- final `pc = $0013F8`
- `last_lba = 3326`

So the measured continuation is not using the `$006D66` exit. The next useful
stop is now directly at `$006D6E`.

That direct `$006D6E` stop is now resolved too: `$006D6E` occurrence `#1`
never occurs on the full persistent repaired run either. The run again
finishes first with the same repaired late end state:

- `JCB name query = 'AMOSL INI'`
- `desc+12 = a060_block = $3E817C`
- `preserve_hits = 1`
- `a086_promotions = 1`
- `a086_primes = 1`
- `saw_55aa = saw_56bc = saw_56d2 = 1`
- final `pc = $0013F8`
- `last_lba = 3326`

So the visible `$006D66/$006D6E` exits are not the continuation that carries
the repaired run to the final `$000FA2` convergence. The next useful target is
an earlier exit in that same branch family, most likely `$006B60` or `$006A6A`.

The first such earlier exit is now measured directly. Stopping at
`$006B60` occurrence `#1` lands with:

- `D0 = $00000200`
- `D1 = $00000000`
- `D6 = $00000010`
- `A4 = $3E817C`
- `A6 = $00186A`
- `A1 = $006C3A`
- `A2 = $3E81F0`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`
- `JCB name query = ''`

and the local continuation is now concrete:

- repeated `$006C42/$006C44/$006C48` polling continues until masked `D7 = 1`
- then `$006C4A/$006C50/$006C5C` complete the forced handshake
- `$006C5E .. $006C64` return with `D1 = 0`
- `$006B5C` sees zero and falls through to `$006B60`

So `$006B60` is a real earlier exit below the repaired handshake family, and
it is still in the blank-query service context. The next sibling target is now
`$006A6A`, to determine whether that alternate earlier exit is used too.

The first raw `$006A6A` occurrence is now measured, and it is not the same
local state as the `$006B60` fall-through. Stopping at `$006A6A` occurrence
`#1` lands with:

- `D0 = $00000000`
- `D1 = $00000002`
- `D6 = $00001C03`
- `A4 = $3E817C`
- `A6 = $005194`
- `JCB+$00 = $0000`
- `JCB+$20 = $000B`
- `JCB name query = ''`

and the recent execution reaches it through:

- `$005118 .. $005150`
- `$0069D8`
- `$006A2C .. $006A66`
- then `$006A6A`

So raw `$006A6A` occurrence `#1` is not the sibling local exit from the
`$006B60` service continuation. The next useful question is whether a later
`$006A6A` occurrence exists on the repaired cycle at all, or whether the
measured repaired path is effectively using the `$006B60` side only.

That later occurrence now exists and is measured. Stopping at `$006A6A`
occurrence `#2` lands with:

- `D0 = $00000000`
- `D1 = $00000002`
- `D6 = $00001C03`
- `A4 = $3E817C`
- `A6 = $005194`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`
- `JCB name query = ''`

Its lead-in is:

- `$005118 .. $005150`
- `$0069D8`
- `$006A2C .. $006A66`
- then `$006A6A`

and on this repaired-cycle pass the local state differs from raw occurrence
`#1` mainly by `JCB+$00 = $0010` and the earlier `D1` history (`$00000106`
feeding into the same `$00000200` local service shape).

So the measured repaired cycle does use both blank-query service branches:
`$006A6A` and `$006B60`. The next useful target is now the dispatcher above
them, especially the late `$005118 .. $005150` / `$005140` family that feeds
these blank-query service branches after repaired `A086`.

That dispatcher is now measured directly at a repaired-cycle stop. Stopping at
`$005118` occurrence `#2` lands with:

- `D0 = $00000000`
- `D1 = $00000106`
- `D6 = $00001C03`
- `A4 = $3E817C`
- `A6 = $00184A`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`
- `JCB name query = ''`

Its immediate lead-in is now concrete:

- `$004DFE .. $004E06`
- `$0050DE`
- `$0054D0 .. $00551A`
- `$0050E2 .. $0050FC`
- then `$00510A .. $005118`

At this stop, the blank-query dispatcher is taking the same late compare
family with `D1 = $00000106`, and `$0050FC` is again taking the carry path to
`$00510A`. So the next useful refinement is no longer “does this dispatcher
exist,” but how it advances across the later blank-query sequence
(`$00000106`, `$00000202`, `$00000706`) before final convergence.

That later-sequence refinement is now measured too. Stopping at `$005118`
occurrence `#4` lands with:

- `D0 = $00000000`
- `D1 = $00000706`
- `D6 = $00001C03`
- `A4 = $3E817C`
- `A6 = $00184A`
- `JCB+$00 = $0010`
- `JCB+$20 = $000B`
- `JCB name query = ''`

and the path is the same dispatcher family again:

- recent `A086` is the repaired blank-query call with `D1 = $00000706`
- the local state still comes through the same `$0050DE` / `$0054D0 .. $00551A`
  / `$0050E2 .. $00510A` family
- then `$00510A .. $005118`

So the later blank-query cycle really does advance through the expected
dispatcher sequence, not just a single `$00000106` case. The next frontier is
therefore no longer “which D1 case fires,” but why this whole repaired
dispatcher family stays on blank-query service work instead of ever turning
into a real `AMOSL INI` disk request beyond LBA `3326`.

There is now a narrow tracer hook for testing that directly:

- `--seed-long-at-pc-always=PC:ADDR:VALUE`

It is the persistent-memory analogue of `--force-reg-at-pc-always`, so it can
coexist with `--pc-seeds-once` while repeatedly repairing a specific memory
slot on every recurrence.

The first direct JCB-name preservation test with that hook is complete, and it
is informative but not yet a valid semantic preservation:

- target: persist the full JCB name descriptor at `$007074` on every
  `$005118` dispatcher pass
- result: the hook does fire on every recurrence
- but the seeded long values were byte-swapped relative to the desired on-wire
  byte image

Measured outcome of that malformed first pass:

- the blank-query name becomes garbage text, not `AMOSL INI`
- sampled query becomes `M7 Y$ SO4`
- final `desc+12` is corrupted to `$007C81`
- final `pc` is still `$0013F8`
- `last_lba` is still `3326`

So the hook works, but the first preservation attempt used the wrong endian
long values. The next useful run is the same experiment with byte-swapped
descriptor longs so the seeded bytes actually spell the intended `AMOSL INI`
descriptor image.

That first byte-swapped rerun is now complete too, and it exposed a tracer-side
issue instead of a clean emulator result:

- the persistent seeding hook does fire with the corrected long values
- but the resulting descriptor bytes at `$005140` are still not the intended
  `AMOSL INI` image:
  - observed prefix: `00 00 00 41 57 08 FF FF 79 3A A0 78 ...`
- when the tracer tries to decode that malformed RAD50 image, `dump_le_rad50_name`
  raises `IndexError`

So the next immediate task is not another emulator-side conclusion. It is:

1. make the tracer tolerate malformed RAD50 descriptor bytes without crashing
2. inspect the helper write packing or switch to exact-byte/word seeding for
   the JCB descriptor image
3. rerun the preservation experiment with the exact intended descriptor bytes

That exact-byte preservation rerun is now complete too. Using correctly packed
long values for the byte image at `$007074`:

- `$007074 = $41000000`
- `$007078 = $FFFF0857`
- `$00707C = $78A03A79`
- `$007080 = $00000000`
- `$007084 = $0001003E`
- `$007088 = $817C0000`
- `$00708C = $02000000`
- `$007090 = $00000000`

the persistent seeding hook now preserves a valid `AMOSL INI` descriptor across
every later `$005118` dispatcher pass.

Measured outcome of that corrected preservation run:

- `$005140` shows `JCB name query='AMOSL INI'`
- later `$00571C`, `$0056D2`, and `$0056D4` also keep
  `JCB name query='AMOSL INI'`
- the repaired late path still reaches promoted `A086` on `desc+12 = $3E817C`
- final `pc` is still `$0013F8`
- `last_lba` is still `3326`

So preserving a valid `AMOSL INI` JCB name descriptor across `$005118` is a
real local repair, but it is still not sufficient to break the global
`last_lba=3326` ceiling. The remaining blocker is therefore below, or
independent of, the JCB name descriptor text seen in the late dispatcher.

The next lower repaired-path anchor is now measured directly too. On the same
corrected preserved-name setup, stopping at `$00571C` occurrence `#1` lands in
the repaired late `AMOSL INI` continuation, not the old blank-query service
loop:

- `pc = $00571C`
- `JCB name query = 'AMOSL INI'`
- `JCB+$00 = $0010`
- `D1 = $00000104`
- `A1 = $3E83E8`
- `A2 = $3E81F0`
- `A6 = $00182A`
- `desc+12 = $3E817C`

The measured lead-in is:

- `$00503C .. $005068`
- `$0056D6: BNE $00571C`
- `$0056D8/$0056DC/$0056E0/$0056E6/$0056E8`
- `$00571C`

So the corrected JCB-name preservation does not just restore the visible name
text. It carries the run into the real post-`A086` late continuation at
`$00571C` with the repaired promoted block still live. The remaining blocker is
therefore below that branch family, inside the `$00571C .. $005750`
continuation or the state it is consulting there.

The first direct sub-branch test under that continuation is now complete too.
On the same repaired setup, stopping at `$005730` occurrence `#1` never hits
before the run finishes:

- no observed `$005730` / `$A06E` entry
- final `pc = $0013F8`
- `last_lba = 3326`
- final late repaired state is still intact:
  - `JCB name query = 'AMOSL INI'`
  - `desc+12 = $3E817C`
  - `preserve_hits = 1`
  - `a086_promotions = 1`
  - `a086_primes = 1`

So the repaired `$00571C` continuation is exiting before the `$005730` / `A06E`
call. The next local edge is therefore at or before the first branch targets in
that family, especially `$00572A` and `$005750`.

The sibling early-exit target is now ruled out too. On the same repaired
setup, stopping at `$005750` occurrence `#1` also never hits before the run
finishes:

- no observed `$005750`
- final `pc = $0013F8`
- `last_lba = 3326`
- final repaired late state is still intact:
  - `JCB name query = 'AMOSL INI'`
  - `desc+12 = $3E817C`
  - `preserve_hits = 1`
  - `a086_promotions = 1`
  - `a086_primes = 1`

So the repaired `$00571C` path is exiting even earlier than the first visible
branch targets at `$005730` and `$005750`. The tight next anchor is now the
immediate return site around `$005724`, or the state consumed between
`$00571C` and that return.

That immediate-return stop is now measured directly. On the same repaired
setup, stopping at `$005724` occurrence `#1` lands exactly on the early `RTS`:

- `pc = $005724`
- `JCB name query = 'AMOSL INI'`
- `JCB+$00 = $0010`
- `D1 = $00000104`
- `A1 = $005B90`
- `A2 = $000000`
- `A6 = $00182A`
- `desc+12 = $3E817C`

The measured local path is only:

- `$00571C`
- `$00571E`
- `$005720`
- `$005724`

So the repaired late path is not reaching any of the later `$00572A/$005730`
or `$005750` logic at all. The first concrete state change inside this helper
is that `A1` pivots from the earlier `$3E83E8` context to `$005B90` while
`A2` is cleared to zero, and then the helper returns immediately. The next
highest-yield target is therefore the caller immediately after that `RTS`, or
the lookup/state that turns `A2` into zero before the return.

One direct intervention on that local state is now measured too. Seeding a
nonzero long at `$3E81F0` exactly at `$0056E6`:

- `--seed-long-at-pc=0x56E6:0x3E81F0:0x00000001`

is not sufficient to reach `$005730`. The run still enters the same immediate
`$00571C .. $005724` return helper first. But the post-return path is now more
explicitly visible:

- after `$005724`, execution returns to `$005B78 .. $005B74`
- that path then re-enters `$0056BC .. $0056D4`
- `D1` shifts from `$00000104` to `$00000106`
- `A6` becomes `$000498`
- `A2` is still zero by the later `$0056D2/$0056D4` pass

So a nonzero long at `$3E81F0` does not break the first immediate-return path.
The next precise retry should use a word-sized seed at `$0056E6`, since the
observed opcode is consistent with a word test on `(A2)`. Independently of
that width issue, the caller-after-return path at `$005B78 .. $005B74` is now
measured and is part of the live fallback sequence.

That width-corrected retry is now complete too. Seeding a nonzero word at
`$3E81F0` exactly at `$0056E6`:

- `--seed-word-at-pc=0x56E6:0x3E81F0:0x0001`

still does not reach `$005730`, but it proves a narrower local point:

- the path no longer falls directly from `$0056E8` to `$00571C`
- instead it executes:
  - `$0056EA`
  - `$0056EC`
  - `$0056EE`
  - `$0056F0`
  - `$0056F2`
  - `$0056F4`
  - then returns to `$0056E6`
- only on that second pass does it branch to `$00571C`

The stop at `$00571C` on that run also shows the scan moved:

- earlier unseeded stop: `A2 = $3E81F0`
- word-seeded stop: `A2 = $3E81F8`

So the `$0056E6/$0056E8` word test is a real local causal edge. But seeding a
single nonzero word only advances the scan by one entry; it still falls back
through `$00571C .. $005724`, then `$005B78 .. $005B74`, and still never
produces reads past LBA `3326`. The next likely intervention is therefore not a
single word but the minimal scanned structure that keeps the `$0056EA .. $0056F4`
walk alive past the next entry.

That next-entry extension is now measured too. Seeding two words instead of
one:

- `--seed-word-at-pc=0x56E6:0x3E81F0:0x0001`
- `--seed-word-at-pc=0x56E6:0x3E81F8:0x0001`

still does not reach `$005730`, but it confirms the walk shape:

- the first `$00571C` stop now lands with `A2 = $3E8200`
- compared with the earlier runs:
  - no word seed: `A2 = $3E81F0`
  - one word seed: `A2 = $3E81F8`
  - two word seeds: `A2 = $3E8200`

So the live path is scanning 8-byte entries, and each seeded first word keeps
the `$0056EA .. $0056F4` walk alive for exactly one more entry. Even with two
entries preserved, the path still falls into the same immediate-return helper
and still stops at `last_lba=3326`. The next minimal test should therefore seed
the next entry in the same 8-byte series, or dump the surrounding `$3E81F0`
table to identify what full entry shape the scan is actually expecting.

### Current validated test baseline

Most recently re-run on 2026-03-09:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/devices/test_timer6840.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration
```

Results:

- device tests: `3 passed`
- integration tests: `17 passed, 1 skipped, 1 xfailed`

Expected xfail:

- `tests/integration/test_boot_native_amosl_ini.py`

## Current Highest-Signal Open Questions

1. Why does the faithful long-seed path fall into `$001E22/$0012B6/$001718`
   after `JCB+$20` drains to zero, while the word-seed path re-enters
   `TTYIN @ $001E2C`? The current best answer is the post-drain count gate at
   `$001E0C..$001E22`: faithful `TCB+$12=$0000000B` takes the low-count
   `IOWAIT` path, while the word-seeded side carries an artificial high value.
   Forcing `D6` only at `$001E14` is too late, so the remaining immediate test
   is to intervene at the compare itself. That intervention now works at
   `$001E10`, so the next question is whether that alone recreates the later
   `$0020D8..$0020EA` corruption loop. It does, with natural `TCB+$12`
   bookkeeping, so the next question is whether the second `TTYIN` loop itself
   matters to the real disk-read blocker or is still just seeded-path noise.
   The current best answer is: it is real, but still not sufficient, because
   the compare-forced path falls back to `IOWAIT @ $001E22` with
   `last_lba=3326`. Also, the first `$001232` queue-write stop occurs earlier
   than that whole branch, so the scheduler path must be tracked separately.

2. On the word-seeded side, which exact post-drain `TTYIN` instruction turns
   the already-live `D6=$000B0000` into the old artificial `D6=$000AFFFF`, and
   why does that restore `A6=$3F8086`? The current best answer is
   `$0020D8..$0020EA`; the remaining task is to explain the exact register
   setup entering that loop.

3. How exactly does the shared `$0029D0/$0029F0` pointer/count state
   (`A0+0C=$003E8170`, `A0+10=$0001`, `A0+20=$000B->$000A`) produce the first
   visible `A` loss?

4. What is the smallest native state repair that finally produces disk reads
   beyond LBA `3326`?
   The current answer is narrower than before: early `USP` repair, repaired
   late `A086` state, the `$004DA4` branch override, and now the live
   `$0050F8/$0050FC` compare override are all individually real and still not
   sufficient. That override now resolves to a concrete post-drain
   `TTYLIN`/`IOWAIT` loop at `$001DFC .. $001E24`, and the `$001E10` stop now
   shows that the live compare input is reloaded as `D6=0` from the
   `A5=$3E8000` structure before the branch. The next highest-yield experiment
   is to explain the first post-reentry `A0DC` occurrence where
   `A4 DDB = <invalid $000000>`, because carrying the `$001E10` `D6` override
   forward is now proven to change the local retry path without changing the
   global `last_lba=3326` ceiling. That stop now shows the tighter issue:
   `A4` is never reloaded on the `$003A08 -> $0036F4 -> $003720 -> $00373C`
   re-entry path. Forcing `A4=$007074` at `$003748` is now also proven real
   and still not sufficient, because `$003752` clears it again and the run
   still returns to `$0013F2`. The attempted later repair at `$00375E` is now
   also proven too broad: it fires in unrelated `A052/A064` contexts, can
   zero the JCB name descriptor at `$007074`, and still ends at
   `$0013FA` / `last_lba=3326`. A stop at the first `$0037B8` occurrence is
   more specific: it arrives through the `$003772 .. $0037B8` internal walk
   with `A4/A3=$3E81E4`, stages `A064 arg ptr = $3E81F0`, and that first
   argument block is all zeroes. The first `$0037BC` stop now confirms the
   return path too: it goes through `$002B28 .. $002B5E`, then
   `$001B88 .. $001BA8`, then lands on `BEQ $003752` with the same staged
   zero-arg context. Seeding the first long at `$3E81F0` to `0x400E1C03` is a
   real local edge, and carrying that seeded variant forward is now complete:
   it restores a live `AMOSL INI` name descriptor, naturally clears the
   `$004A96` gate, reaches `$0055AA/$0056A6/$0056D2/$0056D4`, and runs the
   late `A086` on the promoted target `$3E817C`, but it still ends at
   `$0013F8` with `last_lba=3326`. A direct stop below that flow is now also
   measured: before the run lands at `$000FA2`, it spins in a tight
   `$006B68/$006B6A/$006B6E` service loop on the promoted block. A later stop
   at `$006B68` occurrence `#1300` now lands in the repaired named context and
   decodes that loop concretely as `MOVE.B (A5),D7; ANDI.B #$02,D7;
   BNE $006B68` with `A5=$FFFFC8`. Clearing that polled bit at `$006B68` is
   now also proven causal: a one-shot seed `$FFFFC8 <- $00` breaks the loop
   and reaches `$006B70: MOVE.B #$00,(A5)` on the same late path with
   `A4=$3E817C`. Carrying that intervention forward is now also measured: it
   enters `$006B70 .. $006C10`, hits `IOWAIT`, flips `JCB+$00` to `$0010`,
   walks the alternate `D1=$0106/$0202/$0706` sequence, then returns to the
   repaired `AMOSL INI` path and late promoted `A086` again, but still ends at
   `pc=$0013F8` with `last_lba=3326`. The next highest-yield experiment is
   therefore lower in that continuation. The `$006C42/$006C44/$006C48` poll is
   now directly measured too: it loops on `ANDI.B #$01,D7` against the same
   `$FFFFC8` status byte, and forcing `D7=1` at `$006C48` reaches `$006C4A`.
   The next highest-yield experiment is one step lower again. The whole
   observed `$006B68 .. $006C5C` status-handshake chain is now also proven
   insufficient when forced through end-to-end: native still returns to
   repaired `AMOSL INI`, late promoted `A086`, then `$000FA2 -> $0013F8` with
   `last_lba=3326`. A path-specific stop at that forced-handshake `$000FA2`
   clarified why: the repaired late path re-entered the same
   `$006B68/$006B6A/$006B6E` loop immediately before the interrupt, and the
   earlier “full forced handshake” was one-shot because it used
   `--pc-seeds-once`. That persistent follow-up is now measured too, with a
   narrow `--force-reg-at-pc-always` tracer hook: even persistent repair of the
   whole observed `$006B68 .. $006C5C` handshake still ends at `pc=$0013F8`
   with `last_lba=3326` and repaired late `AMOSL INI` state intact. The next
   highest-yield experiment is therefore below that handshake family, not
   another variant of the same `$FFFFC8` poll repair.

## March 11, 2026 Tooling Checkpoint

- `trace_native_cmdfile_jobq.py` now accepts
  `--seed-word-series-at-pc=PC:START:STRIDE:COUNT:VALUE`.
- The new flag expands into the existing one-shot word-seed path, so it uses
  the same runtime behavior as the already-validated `--seed-word-at-pc`
  mechanism.
- `python3 -m py_compile trace_native_cmdfile_jobq.py` passed immediately after
  that change.
- The next live test is a dense `$0056E6/$0056E8` table fill so the 8-byte scan
  can be kept alive across a broader range than the manual one-entry/two-entry
  probes.

## March 11, 2026 Dense Scan Fill

- First dense-fill probe:
  `--seed-word-series-at-pc=0x56E6:0x3E81F0:0x8:64:0x0001`
  with `--stop-pc-occurrence=0x5730:1`.
- Result: `$005730` still never hit.
- The repaired run carried the live `AMOSL INI` name and promoted
  `desc+12=$3E817C` state forward, but it still converged on the old
  `$000FA2 -> $0013D2/$0013FA` regime with `last_lba=3326`.
- So a broad nonzero fill of the first word across 64 consecutive 8-byte
  entries is still not sufficient to push the `$0056E6` scan past the
  immediate-return helper into the visible `$005730` continuation.
- Structural follow-up on the same 64-entry fill with
  `--stop-pc-occurrence=0x571C:1`:
  the first stop lands with `A1 = A2 = $3E83E8`, `D1 = $00000104`,
  `desc+12 = $3E817C`, and repaired `AMOSL INI` state intact.
- Recent execution at that stop is now concrete:
  `$0056F6 -> $0056F8 -> $0056FC -> $00571C`.
- The disassembly at that point resolves the next gate:
  `$0056F8` is `TST.W 4(A2)` and `$0056FC` is `BEQ $00571C`.
- `A1/A2` bytes at the stop are
  `01 00 00 00 00 00 00 00 ...`, so the dense first-word fill is sufficient to
  carry the scan all the way to `$3E83E8`, but it still falls into the
  immediate-return helper because the second word at `4(A2)` is still zero.
- The next minimal experiment is therefore to seed the `+4` word across the
  same 8-byte series and test whether that is enough to reach `$005730`.
- Dual-word dense-fill follow-up:
  added a second series seed at `0x3E81F4` so both the first word and `4(A2)`
  were forced nonzero across 64 consecutive 8-byte entries, then reran with
  `--stop-pc-occurrence=0x5730:1`.
- Result: `$005730` still never hit.
- The run kept the repaired late state intact all the way through the promoted
  late `A086` path:
  `desc+12 = $3E817C`, `preserve_hits = 1`, `a086_promotions = 1`,
  `a086_primes = 1`, `saw_55aa = saw_56bc = saw_56d2 = 1`.
- It still finished at `pc = $0013FA` with `last_lba = 3326`.
- So the `$0056F8/$0056FC` `TST.W 4(A2)` gate is real, but satisfying it across
  the same 64-entry series is still not sufficient to reach the visible
  `$005730` continuation or produce new disk I/O.
- The next structural test is to stop earlier at `$005726` or `$00572A` under
  the same dual-word fill and determine whether the path is now escaping the
  immediate RTS but branching away before `$005730`.
- Structural stop follow-up with the same dual-word fill and
  `--stop-pc-occurrence=0x5726:1` still never hit `$005726`.
- That run again carried the full repaired late state through promoted `A086`
  and still finished at `pc = $0013FA`, `last_lba = 3326`.
- So even after satisfying both the first-word scan and the `4(A2)` gate
  across the whole 64-entry range, the path still does not escape into the
  post-RTS `$005726 ..` continuation.
- The next useful stop is back at `$00571C` under the same dual-word fill so
  the live bytes around `A1/A2` can show what further field inside the entry is
  still causing the helper to return immediately.

## Related Integration Tests

- `tests/integration/test_boot_native_disk_read.py`
- `tests/integration/test_boot_native_handoff.py`
- `tests/integration/test_boot_native_amosl_ini.py`
- `tests/integration/test_boot_native_monitor_register_stages.py`
- `tests/integration/test_selftest_serial_window.py`
- `tests/integration/test_selftest_space_match.py`
- `tests/integration/test_selftest_header_output.py`
- `tests/integration/test_selftest_fffe28_placeholder.py`
