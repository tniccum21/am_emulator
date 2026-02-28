# amos_dis.py Boot ROM Mode — Developer Specification

**Version:** 1.0
**Date:** 2026-02-26
**Author:** Tom Niccum / Claude (RE analysis)

---

## 1. Motivation

Boot ROM disassembly is a primary use case for `amos_dis.py`. Current behaviour produces poor results on boot PROM binaries because the disassembler was designed for AMOS program files (`.LIT`, `.RUN`, `.TSK`) which have headers, known entry points, and follow AMOS calling conventions. Boot PROMs differ in several critical ways:

- No AMOS program header (`PHDR`); the image starts with 68000 exception vectors.
- Code executes on bare hardware before the OS is loaded — no SVC macros, no JCB/TCB, no JOBCUR.
- Strings use AMOS word-swapped encoding (low byte = first char, high byte = second char per 16-bit word) but are embedded inline within code regions.
- ROM images are assembled from two interleaved 8-bit EPROM chips (high byte / low byte).
- Computed dispatch through indirect jumps (`JMP @A6`, `CALL @A6`) and boot-ID lookup tables prevents the disassembler from following all code paths, leaving large regions decoded as `WORD` data instead of instructions.

A manually reverse-engineered reference PROM is provided as `NEWPRM.m68` (AM100/L boot code). Our target is the AM-178 System Self-Test boot ROM pair (`AM-178-00-B05.BIN` + `AM-178-01-B05.BIN`), 16KB combined.

---

## 2. Scope

Add a `--bootrom` (or `-b`) command-line option to `amos_dis.py` that enables boot-ROM-specific decoding behaviour. This is a mode flag, not a separate tool. When active, the disassembler applies the rules below. When inactive, all existing behaviour is unchanged.

---

## 3. Feature Requirements

### 3.1 ROM Chip Interleaving (Input Handling)

**Problem:** Boot ROMs ship as two 8-bit-wide EPROM chips that must be combined into a single 16-bit-wide binary before disassembly. Users currently do this manually with external scripts, and getting it wrong produces garbled output.

**Requirement:** When `--bootrom` is active and TWO input files are provided, `amos_dis.py` should interleave them automatically.

**Interleave scheme:** Byte-interleave with the first file as HIGH byte (even addresses) and the second file as LOW byte (odd addresses). For each byte index `i` in the source ROMs:

```
output[2*i]     = file1[i]   (HIGH byte, even address)
output[2*i + 1] = file2[i]   (LOW byte, odd address)
```

**Verification:** After interleaving, validate the 68000 boot vectors:
- Longword at offset $0000 (SSP) must be a plausible RAM address (> $10000, < $1000000, longword-aligned).
- Longword at offset $0004 (PC) must point into the ROM address range.

If validation fails, try the reverse order (swap file1/file2) and re-validate. If neither order produces valid vectors, emit a warning and proceed with the first order.

**Single file input:** When only one file is provided with `--bootrom`, treat it as a pre-combined binary (no interleaving).

**CLI examples:**
```
amos_dis.py --bootrom AM-178-01-B05.BIN AM-178-00-B05.BIN -o BOOTROM.DIS
amos_dis.py --bootrom BOOTROM.BIN -o BOOTROM.DIS
```

### 3.2 Exception Vector Table Decoding ($0000–$00FF)

**Problem:** The disassembler currently tries to decode the vector table area as instructions, producing nonsensical output like `BTST D1,D0` for the SSP vector.

**Requirement:** In `--bootrom` mode, decode offsets $0000–$03FF as a 68000 exception vector table:

| Offset | Vector | Label |
|--------|--------|-------|
| $0000 | Initial SSP | `V.SSP` |
| $0004 | Initial PC (reset entry point) | `V.PC` |
| $0008 | Bus Error | `V.BUSERR` |
| $000C | Address Error | `V.ADRERR` |
| $0010 | Illegal Instruction | `V.ILLEG` |
| $0014 | Zero Divide | `V.ZDIV` |
| $0018 | CHK Instruction | `V.CHK` |
| $001C | TRAPV Instruction | `V.TRAPV` |
| $0020 | Privilege Violation | `V.PRIV` |
| $0024 | Trace | `V.TRACE` |
| $0028 | Line-A Emulator | `V.LINEA` |
| $002C | Line-F Emulator | `V.LINEF` |
| $0030–$003B | (Reserved) | |
| $003C | Uninitialized Interrupt | `V.UNINIT` |
| $0040–$005F | (Reserved) | |
| $0060 | Spurious Interrupt | `V.SPUR` |
| $0064–$007F | Autovectors 1–7 | `V.AUTO1`–`V.AUTO7` |
| $0080–$00BF | TRAP #0–#15 | `V.TRAP0`–`V.TRAP15` |
| $00C0–$00FF | (Reserved) | |

**Output format:** Each vector should be emitted as `LWORD` with its resolved value and a comment showing the symbolic address if it points into the ROM:

```
V.SSP:   LWORD  LODMEM                ; $00032400 — initial SSP (RAM)
V.PC:    LWORD  LODMEM+START          ; $00800018 — initial PC (ROM entry)
V.BUSERR: LWORD $00800xxx             ; → BUSERR handler
```

**Notes:**
- Vectors pointing to $00000000 or containing $FFFFFFFF should be marked as unused/unprogrammed.
- The PC vector ($0004) establishes the ROM base address AND the first code entry point. Extract both.
- Vectors pointing into the ROM should be added to the code entry point list (see §3.4).

### 3.3 ROM Base Address Detection

**Problem:** Boot ROMs are mapped at a hardware-determined base address (e.g., $800000 for AM-178, $170000 for AM-100L as `LODMEM`). The binary file starts at offset 0, but code references use the mapped address. The disassembler needs to know this mapping to resolve absolute addresses back to labels within the ROM.

**Requirement:** Auto-detect the ROM base address from the PC reset vector at offset $0004:

```
rom_base = longword_at($0004) & 0xFFFF0000
entry_offset = longword_at($0004) & 0x0000FFFF
```

For our target: `$00800018` → base = `$00800000`, entry = `$0018`.

Emit this as an equate at the top of the output:

```
LODMEM = $800000                      ; ROM base address (auto-detected from reset vector)
```

Allow override via `--rom-base <address>` for cases where auto-detection is wrong.

When resolving absolute addresses in code (e.g., `JMP $00800xxx`), subtract the ROM base to get the file offset and generate a local label reference instead of an absolute address.

### 3.4 Multi-Pass Code Discovery

**Problem:** This is the biggest issue. The current single-pass disassembler cannot follow computed dispatch paths like `JMP (A6)` or `CALL @A6` where A6 was loaded from a lookup table. This causes the entire self-test code region (~8KB in the AM-178 ROM) to be emitted as `WORD` data directives.

**Requirement:** Implement iterative multi-pass code discovery:

**Pass 1 — Seed entry points:**
- PC reset vector target (from $0004)
- All exception vector targets that point into the ROM
- User-supplied entry points via `--entry <addr>` or `--entry-file <file>`

**Pass 2 — Recursive descent disassembly:**
From each entry point, follow code flow:
- Sequential instruction flow
- Branch targets (Bcc, BRA, BSR — both .S and .W forms)
- JSR/JMP with absolute or PC-relative addressing
- Stop at: RTS, RTE, JMP (indirect), STOP, or illegal/undecodable opcodes

**Pass 3 — Static branch target extraction:**
Scan the ENTIRE binary for words that could be branch/jump instructions, regardless of whether they were reached by Pass 2. For each potential JSR/BSR/Bcc/BRA, compute the target address. If the target is within the ROM and word-aligned, add it as a tentative entry point. Re-run Pass 2 with the expanded entry list.

**Pass 4 — Table-driven discovery:**
Identify dispatch table patterns:
- Sequences of `LWORD` values pointing into the ROM (like `BIDTBL` in NEWPRM.m68)
- Sequences of `WORD` values that could be PC-relative offsets
- The boot-ID table pattern: consecutive longwords of the form `LODMEM + offset`

For each discovered table entry that resolves to a valid ROM offset, add it as an entry point and re-run descent.

**Convergence:** Repeat passes until no new entry points are discovered.

**Metrics:** At the end of disassembly, report:
```
; Code coverage: XXXX bytes as instructions (XX.X%)
; Data coverage: XXXX bytes as WORD/BYTE/LWORD data (XX.X%)
; Unclassified:  XXXX bytes (XX.X%)
; Entry points:  XXX discovered
```

### 3.5 AMOS Word-Swapped String Detection and Decoding

**Problem:** AMOS stores ASCII strings with bytes swapped within each 16-bit word. In the ROM binary (correct 68000 byte order), the string "COPR" is stored as bytes `$4F $43 $52 $50` (word $4F43 = 'O','C' then word $5250 = 'R','P'). The AMOS `ASCII` and `ASCIZ` assembler macros produce this encoding automatically. The disassembler should recognise and decode these strings.

**Detection heuristic:** A sequence of words starting at address `A` is a candidate AMOS string if:
1. For each word `W` at offsets `A`, `A+2`, `A+4`, ...:
   - `W & 0x00FF` (low byte) is a printable ASCII character ($20–$7E), CR ($0D), or LF ($0A)
   - `(W >> 8) & 0xFF` (high byte) is printable ASCII, CR, LF, or $00 (null terminator)
2. The sequence is at least 4 characters long (2 words minimum)
3. The sequence ends with a null byte (low byte = $00, or high byte = $00 after a valid low byte)

**Output format:** Emit detected strings using the AMOS `ASCIZ` macro with the decoded (human-readable) text:

```
STR01:  ASCIZ  /COPR. 1985 ALMI/
STR02:  ASCIZ  / Memory detected - /
STR03:  ASCIZ  /<0D><0A>SERIAL PORT TEST<0D><0A>/
```

For strings containing CR/LF, use `<0D>` and `<0A>` escape notation within the ASCIZ delimiter.

**Priority:** String detection should run BEFORE code discovery passes. Regions identified as strings should be excluded from instruction decoding (they will decode as garbage instructions otherwise).

### 3.6 RADIX-50 Inline Data Detection

**Problem:** Boot ROMs embed RADIX-50 encoded filenames inline after `CALL` instructions (e.g., in `GETFIL` patterns from NEWPRM.m68):

```
CALL    GETFIL
WORD    [BAD]         ; R50 packed filename part 1
WORD    [BLK]         ; R50 packed filename part 2
WORD    [SYS]         ; R50 packed filename extension
```

The `CALL` pushes the return address, and `GETFIL` pops it to read the inline data, then adjusts the return address past the data. Without recognising this pattern, the disassembler tries to decode the R50 words as instructions.

**Requirement:** Detect the `CALL` + inline RADIX-50 data pattern. When a `CALL`/`BSR` target is identified as a routine that consumes inline data (heuristic: the routine immediately pops the return address with `POP A1` or `MOV (SP)+,A1`), mark the words following the call as inline data and emit them as:

```
        CALL   GETFIL
        WORD   [BAD]                  ; RADIX-50: "BAD"
        WORD   [BLK]                  ; RADIX-50: "BLK"
        WORD   [SYS]                  ; RADIX-50: "SYS"
```

Use the `[XXX]` RADIX-50 literal syntax that the AMOS assembler accepts.

**Decoding:** RADIX-50 word = `A*1600 + B*40 + C` where: space=0, A-Z=1-26, $=27, .=28, 0-9=30-39.

### 3.7 Drive Parameter Table Detection

**Problem:** Boot ROMs contain drive geometry tables (seen in both NEWPRM.m68 as `DVTBL1`/`DVTBL2` and in our AM-178 ROM at offset $04C8). These are structured records of WORDs — not instructions, not strings. The disassembler currently misidentifies them.

**Detection heuristic:** A sequence of words is a candidate drive table if:
- It contains recognizable drive geometry values: cylinder counts (100–1024), head counts (1–16), sector counts (17–64), block counts (10000–65000)
- The values appear in a repeating record structure (same stride between similar-magnitude values)

**Output format:** Emit as commented WORD directives with the field interpretation:

```
DVTBL1:                                 ; drive parameter table
        WORD   306.                     ; cylinders
        WORD   1.                       ; alternate tracks
        WORD   4.                       ; heads
        WORD   307.                     ; write precompensation cylinder
        WORD   307.                     ; reduce write current cylinder
        WORD   6                        ; control field
        WORD   19520.                   ; total blocks
        WORD   287.                     ; diagnostic cylinder
        WORD   4.                       ; (reserved)
```

**Note:** The record structure is 9 words (18 bytes) per entry, matching the `DVTBL` layout in NEWPRM.m68. A zero word terminates the table.

### 3.8 Boot-ID Dispatch Table Detection

**Problem:** Boot ROMs use a boot-ID lookup table (`BIDTBL` in NEWPRM.m68) containing longword pointers to device-specific boot routines. These are `LWORD LODMEM+label` entries — absolute ROM addresses.

**Detection heuristic:** A sequence of consecutive longwords where each value is within the ROM address range (`rom_base` to `rom_base + rom_size`) is a candidate boot-ID table.

**Output format:**

```
BIDTBL: LWORD  LODMEM+FLOPY8           ; boot ID #0 — AM210 8"
        LWORD  LODMEM+AM410            ; boot ID #1 — AM410
        LWORD  LODMEM+AM500            ; boot ID #2 — AM500
        ...
```

Each resolved target should be added to the code entry point list.

### 3.9 Hardware I/O Register Annotation

**Problem:** Boot ROMs interact heavily with memory-mapped I/O registers. Absolute short addresses in the $FExx–$FFxx range (or board-specific ranges) appear as large decimal numbers that are meaningless without context.

**Requirement:** In `--bootrom` mode, annotate known hardware register addresses. Support a `--hw-map <file>` option for user-supplied mappings, with a built-in default table for common Alpha Micro hardware:

```
; Known Alpha Micro I/O registers
$FE00 = IOWST     ; Interface driver write/status register
$FE03 = IOSIZ     ; Serial interface size/baud select
```

**Output:** Append comments on instructions that reference these addresses:

```
        MOVB   #6.,65024.              ; → IOWST: interface init
```

### 3.10 Firmware Work Area Annotation

**Problem:** Boot ROMs establish a base register (typically A5) pointing to a firmware work area in RAM, then use offsets from it throughout the code. These offsets often coincidentally match AMOS JCB/TCB field names, causing false `AMB:` annotations.

**Requirement:** In `--bootrom` mode, suppress `AMB:` annotations for A5-relative offsets by default. Instead, if offset equates are available (like the `NEWPRM.m68` data area at the end), use those:

```
PRMEND:
L2070   = PRMEND
FLPSTP  = L2070 + 2
RESONB  = FLPSTP + 2
BOTADR  = RESONB + 2
...
```

Support a `--fw-equates <file>` option to supply these.

### 3.11 Decimal Suffix Handling for Data

**Requirement:** All numeric values emitted as `WORD`, `BYTE`, or `LWORD` data directives in `--bootrom` mode must include the decimal period suffix when the value contains digits 8 or 9, or when the value would be ambiguous in octal:

```
; CORRECT:
WORD   306.          ; cylinders (decimal, contains no 8/9 but > octal range)
WORD   9600.         ; baud rate (contains 9, needs suffix)
WORD   6             ; small value, valid in both radixes

; WRONG:
WORD   306           ; assembler reads as octal 306 = decimal 198
WORD   9600          ; N error: digits 8,9 illegal in octal
```

**Rule:** Emit the decimal suffix (trailing period) on ALL data values >= 8 to be safe, since octal interpretation of even valid-looking values can silently produce wrong results.

### 3.12 Self-Test Diagnostic String Table

**Problem:** System self-test ROMs (like our AM-178) contain a large block of diagnostic messages (~100 strings) in the AMOS word-swapped encoding, often as a contiguous string table. This region should be decoded as a string table, not as code or word data.

**Requirement:** When a contiguous region of AMOS-encoded strings is detected (§3.5), group them under a table label and number them sequentially:

```
MSGTBL:                                 ; diagnostic message string table
MSG001: ASCIZ  /<0D><0A><0D><0A>====================<0D><0A>| System Self-Test |<0D><0A>.../
MSG002: ASCIZ  / Memory detected - /
MSG003: ASCIZ  /68010<0D><0A>/
MSG004: ASCIZ  /68000<0D><0A>/
        ...
```

---

## 4. Command-Line Interface

```
amos_dis.py [existing options] --bootrom [options] <file1> [file2] -o <output>

Boot ROM options (only valid with --bootrom):
  --rom-base <addr>      Override auto-detected ROM base address (hex, e.g., $800000)
  --entry <addr>         Add a manual code entry point (hex); may be repeated
  --entry-file <file>    File of entry point addresses, one per line ($XXXX format)
  --hw-map <file>        Hardware I/O register name mapping file
  --fw-equates <file>    Firmware work area equate definitions
  --no-strings           Disable AMOS word-swap string detection
  --no-tables            Disable drive/dispatch table detection
  --coverage             Print code coverage statistics at end of output
```

---

## 5. Output Structure

The `--bootrom` output should follow this structure:

```asm
; Disassembled by amos_dis.py (boot ROM mode)
; Source: AM-178-01-B05.BIN + AM-178-00-B05.BIN
; ROM size: 16384 bytes ($4000)
; ROM base: $800000 (auto-detected)
; Code coverage: 12288 bytes (75.0%), 846 entry points
;

LODMEM = $800000                        ; ROM base address

;===================================================================
; Exception Vector Table ($0000-$00FF)
;===================================================================

V.SSP:   LWORD  $32400                  ; initial SSP
V.PC:    LWORD  LODMEM+START            ; initial PC → $0018
V.BUSERR: LWORD ...
         ...

;===================================================================
; Boot Initialization ($0018-$00xx)
;===================================================================

START:   MOVB   #6.,IOWST              ; initialize interface
         ...

;===================================================================
; Boot-ID Dispatch Table
;===================================================================

BIDTBL:  LWORD  LODMEM+FLOPY8          ; boot ID #0
         ...

;===================================================================
; Drive Parameter Tables
;===================================================================

DVTBL1:  WORD   306.                    ; 10MB: cylinders
         ...

;===================================================================
; Device Boot Routines
;===================================================================

FLOPY8:  ...
AM410:   ...
AM500:   ...
         ...

;===================================================================
; Self-Test Diagnostic Code
;===================================================================

         ...

;===================================================================
; Diagnostic Message String Table
;===================================================================

MSGTBL:
MSG001:  ASCIZ /...System Self-Test.../
MSG002:  ASCIZ / Memory detected - /
         ...

;===================================================================
; Firmware Work Area Equates
;===================================================================

PRMEND:
         ; (computed from end of ROM code)
```

---

## 6. Reference: NEWPRM.m68 Patterns

The manually reverse-engineered `NEWPRM.m68` (AM-100L boot PROM) demonstrates every pattern the disassembler must handle:

| Pattern | NEWPRM Example | Section |
|---------|---------------|---------|
| Exception vectors as `LWORD` | `PROM: LWORD LODMEM` / `LWORD LODMEM+START` | §3.2 |
| ROM base equate | `LODMEM = 170000` | §3.3 |
| Boot-ID dispatch table | `BIDTBL: LWORD LODMEM+FLOPY8` (14 entries) | §3.8 |
| Indirect dispatch | `CALL @A6` after loading from `BIDTBL` | §3.4 |
| Inline RADIX-50 after CALL | `CALL GETFIL` / `WORD [BAD]` / `WORD [BLK]` / `WORD [SYS]` | §3.6 |
| Drive parameter tables | `DVTBL1:` (9 words per record × multiple drives) | §3.7 |
| Firmware work area | `BOTADR = RESONB + 2` chain of equates at end | §3.10 |
| Hardware register I/O | `MOVB #200,7(A4)` to floppy controller | §3.9 |
| AMOS string macro | `ASCII $DIAGNOSTICCYLINDER$` | §3.5 |
| Delay loops | `MOV BOTADR(A5),BOTADR(A5)` (self-read for timing) | — |
| Handshake polling | `HNDSHK: MOVB @A4,D7` / `BPL HNDSHK` | — |
| Sector interleave table | `SECTBL: BYTE 1.` / `BYTE 6.` / ... (26 entries) | — |
| `DIAG` macro (status LED) | `DIAG 6` / `DIAG B` / `DIAG 0` — writes to diagnostic display | §3.9 |

---

## 7. Test Cases

### 7.1 Interleave Validation
- Input: `AM-178-01-B05.BIN` (8192 bytes) + `AM-178-00-B05.BIN` (8192 bytes)
- Expected: Combined binary with SSP=$00032400, PC=$00800018
- The tool should auto-detect which file is HIGH and which is LOW

### 7.2 Vector Table
- Offset $0000: `LWORD $32400` (not `BTST D1,D0`)
- Offset $0004: `LWORD LODMEM+START` (not `ORB #0,-(A4)`)
- Offset $0008–$0017: Copyright string decoded as `ASCIZ /COPR. 1985 ALMI/`

### 7.3 String Detection
- 91 word-swap encoded strings detected in AM-178 ROM
- Includes: "System Self-Test", "Memory detected", "SERIAL PORT TEST", "WINCHESTER TEST", "FLOPPY DRIVE TEST", "VCR TEST", "STREAMER TEST", "MULTI-COMMUNICATIONS PORTS TEST", etc.
- No string bytes decoded as instructions

### 7.4 Code Coverage
- AM-178 ROM: 846 code entry points discoverable via static analysis
- EXTCODE region ($1B72–$3AD1): 497 branch targets, currently 0% coverage without multi-pass
- Target: >80% of ROM decoded as instructions or identified data structures

### 7.5 Drive Parameter Table
- AM-178 ROM at offset $04C8: matches NEWPRM `DVTBL1` format exactly
- First record: 306 cyls, 1 alt, 4 heads, 307 rwc, 307 wpc, 6 ctrl, 19520 blocks
- Should be emitted as labelled `WORD` data with field comments

---

## 8. Implementation Priority

Recommended development order (each step independently improves output quality):

1. **§3.2 Vector table decoding** — eliminates the garbled first 256 bytes; gives us the entry point and ROM base. Quick win.
2. **§3.3 ROM base address** — needed by everything else.
3. **§3.1 ROM interleaving** — eliminates the manual combination step and the biggest source of user error.
4. **§3.5 String detection** — decode the ~2KB of diagnostic strings; prevents them from polluting code analysis. Major readability improvement.
5. **§3.4 Multi-pass code discovery** — the big one. This is what turns 15% code coverage into 80%+. Start with Pass 1+2 (seeded recursive descent), then add Pass 3 (static scan), then Pass 4 (table-driven).
6. **§3.8 Boot-ID table detection** — feeds more entry points into §3.4.
7. **§3.7 Drive parameter tables** — structured data annotation.
8. **§3.6 RADIX-50 inline data** — prevents GETFIL-pattern inline data from being decoded as instructions.
9. **§3.11 Decimal suffixes** — correctness fix for the assembler round-trip.
10. **§3.9 Hardware I/O annotation** — nice to have, improves readability.
11. **§3.10 Firmware work area** — nice to have, reduces false AMB: hits.

---

## 9. Non-Goals (Out of Scope)

- **Full AMOS program disassembly changes:** This spec only affects `--bootrom` mode. Existing behaviour for `.LIT`/`.RUN`/`.TSK` files is unchanged.
- **Interactive disassembly (IDA-style):** We are not building a GUI or interactive tool.
- **Automatic labelling of all subroutines:** The tool generates positional labels (`L1B72:`, `L1C52:`) for branch targets. Meaningful names (`FLOPY8`, `AM410`, `HNDSHK`) require human analysis.
- **Cross-reference database:** No xref tracking beyond what's needed for code discovery.

---

## 10. Appendix A: AM-178 Boot ROM Analysis Summary

| Property | Value |
|----------|-------|
| ROM chips | AM-178-00-B05.BIN (LOW), AM-178-01-B05.BIN (HIGH) |
| Combined size | 16,384 bytes ($4000) |
| ROM base | $800000 |
| SSP | $00032400 (RAM) |
| Entry point | $00800018 → offset $0018 |
| Copyright | "COPR. 1985 ALMI" (AMOS word-swap encoded) |
| Function | AM-1000 System Self-Test diagnostic |
| Tests | Memory, serial ports, Winchester, floppy, VCR, streamer, multi-comms Z80, CTC, SIO, parallel, IPL |
| String count | 91 diagnostic messages |
| Code entry points | 846 (static analysis) |
| Drive tables | At $04C8, matching NEWPRM DVTBL format |
| Unprogrammed area | $3F00–$3FFF (all zeros) |

## Appendix B: AMOS Word-Swap Encoding Quick Reference

The AMOS M68 assembler's `ASCII` and `ASCIZ` macros store two characters per 16-bit word with the byte order swapped relative to 68000 big-endian convention:

```
Word bit layout:  [high byte] [low byte]
Character order:  [char 2]    [char 1]

Example: "AB" → word $4241 (high=$42='B', low=$41='A')
         "COPR" → $4F43 $5250 (word 1: O,C  word 2: R,P)
```

The 68000 CPU reads the word as $4F43, and the AMOS runtime extracts low byte first ($43='C'), then high byte ($4F='O'). This is why strings appear as character-pair anagrams in a hex dump but read correctly at runtime.

**Null termination:** A null byte ($00) in either the low or high byte position terminates the string. Padding with $00 in the high byte of the last word is standard.
