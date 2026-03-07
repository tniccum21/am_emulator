# Native Boot and Serial Findings

Date: 2026-03-07

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

## Related Integration Tests

- `tests/integration/test_boot_native_disk_read.py`
- `tests/integration/test_boot_native_handoff.py`
- `tests/integration/test_boot_native_amosl_ini.py`
- `tests/integration/test_boot_native_monitor_register_stages.py`
- `tests/integration/test_selftest_serial_window.py`
- `tests/integration/test_selftest_space_match.py`
- `tests/integration/test_selftest_header_output.py`
- `tests/integration/test_selftest_fffe28_placeholder.py`

