# AM-1200 System Emulator — Technical Specification

**Project:** AlphaSim  
**Version:** 0.3 DRAFT  
**Date:** 26-Feb-2026  
**Target Hardware:** Alpha Microsystems AM-1200 (DWL-00177-00 Rev B03)  
**Target OS:** AMOS (Alpha Micro Operating System)

---

## 1. Project Overview

### 1.1 Goal
Build a complete system emulator for the Alpha Microsystems AM-1200 computer, capable of booting AMOS from original disk images and running all user programs. The emulator will be written entirely in Python for portability, with a stretch goal of running standalone on a Raspberry Pi.

### 1.2 Success Criteria
1. Boot ROM executes successfully (LED sequence 6 → 11 → 0 → 14)
2. AMOS kernel loads from disk image and initializes
3. Console login prompt appears on emulated terminal
4. User programs (editors, compilers, utilities) run correctly
5. Multi-user terminal support (multiple serial ports)

### 1.3 Available Assets

**Hardware documentation:**
- AM-1200 schematics (15 sheets, DWL-00177-00 Rev B03, P. Elliot, Dec 86)
- AM-1000 schematics (13 sheets, DWL-00167-00 Rev D25) — cross-reference for Z80 multicomm and VCR subsystems

**Boot ROM:**
- AM-178-05 boot ROM binary pair:
  - `AM-178-00-B05.BIN` — 8192 bytes, odd (low) byte EPROM
  - `AM-178-01-B05.BIN` — 8192 bytes, even (high) byte EPROM
  - Combined: 16384 bytes (16KB), maps to $800000–$803FFF
- AM-178-05 fully reverse-engineered annotated assembly (AM17805.M68, 5077 lines)
- AM-100L boot ROM original source code (NEWPRM.m68, 1756 lines)

**Software:**
- AMOS 1.3 disk image: `A13 Boot OS.img` (503MB, raw SCSI sectors, 983040 × 512-byte sectors)
- AMOS disk images for versions 1.3, 1.4, and 2.3 (copied from SCSI drives)
- Custom Python AMOS disk utility: https://github.com/tniccum21/Alpha-Disk-Library
- Custom M68000 disassembler: https://github.com/tniccum21/amos_disassembler
- Assembly source code collection
- Software manuals

**Primary target:** AMOS 1.3 on AM-1200/AM-1400 hardware (config DIP = $02, SCSI boot)

### 1.4 AM-1000 vs AM-1200 Architecture Comparison

The AM-1000 schematics (DWL-00167-00) provide valuable cross-reference material. Key differences:

| Feature | AM-1000 | AM-1200 |
|---------|---------|---------|
| CPU | Motorola 68000 | Motorola 68010 |
| Clock | 8 MHz oscillator | 8 MHz oscillator (sheet 2) |
| Serial ports 0–2 | 3× MC6850 ACIA (BR1941-5, 4.9152 MHz) | 3× MC6850 ACIA (BR1941-8, 4.9152 MHz) |
| Expansion serial | 68681 DUARTs (SIO3–SIO6) | 68681 DUARTs (SIO3–SIO6) |
| SASI/SCSI | TTL-based, same register map | TTL-based, same register map |
| Timer | MC6840 PTM | MC6840 PTM |
| RTC | Present | Present (32.768 KHz crystal) |
| Z80 multicomm | Present (sheet 9: Z80, 2716 ROM, RAM) | Present (sheets 14–15) |
| VCR subsystem | Present (sheets 10–13) | Present (sheet 11) |
| Phantom ROM | Present (PHANTOM signal, sheet 5) | Present (PHANTOM signal, sheet 5) |
| DMA | Not used for disk | Not used for disk |
| MMU | PAL-based (16L8A) | PAL-based (16L8A at U105) |

Both systems share identical I/O register layouts and peripheral architectures. The boot ROM auto-detects 68000/68010/68020 and adjusts accordingly.

---

## 2. Hardware Architecture (from schematics + boot ROM)

### 2.1 CPU

**Processor:** Motorola 68010 (identified as 68010(C-O2) on schematic sheet 2, U107)

The boot ROM auto-detects CPU generation at $800EA0 via TRAP #0 exception frame analysis:
1. Installs a trap handler at vector $80 (TRAP #0)
2. Saves SP to D2
3. Executes TRAP #0
4. Trap handler saves new SP to D0, returns via RTE
5. D2 - D0 = frame size: 6 bytes → 68000, 8 bytes → 68010, 10+ bytes → 68020+

**Confirmed from ROM binary analysis:** The boot ROM does NOT use MOVEC/VBR (zero MOVEC instructions found in the 16KB ROM). It relies on the default VBR=0. Exception vectors are always at $000000.

**Key CPU features for emulation:**
- 24-bit address bus (A1–A23), 16MB address space ($000000–$FFFFFF)
- 16-bit data bus (D0–D15)
- Vectored interrupts (7-level priority)
- Supervisor/User mode
- 68010 exception stack frame (8 bytes with format word, vs 68000's 6 bytes)

**Emulation decision:** Implement 68010 instruction-accurate emulation. Cycle-accurate timing is NOT required — AMOS is a timesharing OS, not a raster-racing video game. However, we must correctly implement the 68010 stack frame format (8 bytes), since the boot ROM explicitly tests this.

### 2.2 PDP-11 Byte Ordering (CRITICAL)

Alpha Microsystems was originally a PDP-11 clone manufacturer. When they moved to the Motorola 68000, they preserved PDP-11 little-endian word ordering in all on-disk and in-memory data structures for backward compatibility. This is the single most important architectural detail for correct emulation.

**The rule:** All AMOS data on disk is stored with bytes swapped within each 16-bit word. When the SASI/SCSI hardware transfers data from the disk to 68010 memory, adjacent bytes are swapped so the 68010 (big-endian) sees the values correctly.

**Example:** The MFD entry for PPN [1,2] (OPR:) is stored on disk as bytes `02 01`. After the hardware byte-swap, the 68010 sees word $0102 (= 258 decimal). The boot ROM compares this against `#258.` and finds a match.

**Byte-swap algorithm for disk reads:**
```python
# When reading a 512-byte sector from disk image into 68010 memory:
for i in range(0, 512, 2):
    memory[dest + i]     = disk_data[i + 1]   # swap adjacent bytes
    memory[dest + i + 1] = disk_data[i]
```

**Design choice for $AAAA5555:** The disk label magic number `$AAAA5555` was deliberately chosen because it is byte-order invariant — the bytes `AA AA 55 55` read the same before and after swapping. This allows the boot ROM to detect labeled disks without knowing the swap state.

**Where the swap happens in the emulator:** The byte-swap must be performed by the SASI controller emulation during PIO data transfer (the `MOVB WD.ERR(A4),(A2)+` loop). Each byte read from the SASI data register should come from the disk image with the appropriate byte-pair swap applied. Since the transfer is byte-by-byte (512 iterations), the controller must track the byte position and serve bytes in swapped order: byte 1, byte 0, byte 3, byte 2, etc.

### 2.3 Memory Map

Derived from boot ROM analysis and schematic address decoding (sheets 1–4):

```
$000000–$000007   Exception vectors (SSP + PC, fetched from ROM at reset)
$000008–$0007FF   Exception vector table (256 vectors × 4 bytes)
$000800–$01FFFF   Low RAM (OS kernel load area — boot loads OS starting at $000000)
$020000–$FE7FFF   Main RAM (probed in 128KB blocks, up to ~16MB)
$800000–$803FFF   Boot ROM (16KB, two 2764 EPROMs)
$FE0000–$FFFFFF   I/O space (memory-mapped peripherals, detailed below)
```

**Boot ROM mapping note:** At reset, the 68010 fetches SSP from $000000 and PC from $000004. The ROM is at $800000, but the hardware uses a "phantom" mechanism (PHANTOM signal, schematic sheet 5 on both AM-1000 and AM-1200) to temporarily map the ROM to $000000 during the first two bus cycles after reset. The initial SSP ($032400) and initial PC ($800018) are read from ROM, then the phantom mapping is disabled and RAM appears at $000000. The ROM copies itself to RAM at $032400 and jumps there.

**RAM configuration:** The boot ROM probes memory by writing/reading longwords at 128KB boundaries starting at $020000, up to a maximum of $FE8000. A typical AM-1200 has 1–4MB of RAM using 41256 (256Kx1) DRAM chips (schematic sheet 6). For emulation, 4MB is a safe default.

### 2.4 Boot ROM Binary Format

The boot ROM consists of two 8KB EPROM images that are byte-interleaved to form the 68010's 16-bit data bus:

```
AM-178-01-B05.BIN → Even (high) bytes: D8–D15 at even addresses
AM-178-00-B05.BIN → Odd (low) bytes:  D0–D7 at odd addresses
```

**Interleave algorithm:**
```python
for i in range(8192):
    combined[2*i]     = rom01[i]   # even byte (high)
    combined[2*i + 1] = rom00[i]   # odd byte (low)
```

**Verification (confirmed from binary analysis):**
- SSP at $800000: $00032400 ✓
- PC at $800004: $00800018 ✓
- First instruction at $800018: `11FC 0006 FE00` = `MOVE.B #6,$FE00` (write 6 to LED) ✓
- Copyright string at $800008: "COPR. 1985 ALMI..." (stored byte-swapped within words)
- TRAP #0 at $800EAA for 68010 detection ✓
- ROM utilization: ~15KB of 16KB used (last non-zero byte at offset $3AF5)
- 4 RTE instructions, 1 TRAP #0, 0 MOVEC instructions

**Text encoding note:** ASCII strings in the ROM appear byte-swapped within 16-bit words when reading the combined binary linearly. This is an artifact of the EPROM interleaving — the CPU reads words correctly through its 16-bit bus. For emulation, load the combined binary directly; the CPU core's word-oriented memory reads will produce correct text.

### 2.5 I/O Register Map

All I/O is memory-mapped. Addresses are byte-accessible (8-bit I/O data bus). Derived from boot ROM equates and schematic I/O decode logic (sheet 3):

#### 2.5.1 System Control

| Address     | Name    | R/W | Description                              |
|-------------|---------|-----|------------------------------------------|
| $00FE00     | HW.LED  | W   | Front panel 7-segment LED display        |
| $00FE03     | HW.CFG  | R/W | Configuration DIP switch / control latch |
| $00FFC8     | HW.SER  | R/W | Primary serial port (console, 6850 ACIA) |
| $00FF88     | HW.MCA  | R/W | Multi-comm controller base               |

#### 2.5.2 Serial Ports — Primary (6850 ACIAs, schematic sheet 7)

Three 6850 ACIA chips provide ports 0–2. Each ACIA has a 2-register interface: status/control at base, data at base+2. Baud rate generators are BR1941-8 (AM-1200) / BR1941-5 (AM-1000) with 4.9152MHz crystals.

| Address     | Port | Register                  |
|-------------|------|---------------------------|
| $FFFE20     | 0    | Status (R) / Control (W)  |
| $FFFE22     | 0    | Rx Data (R) / Tx Data (W) |
| $FFFE24     | 1    | Status / Control           |
| $FFFE26     | 1    | Rx/Tx Data                 |
| $FFFE30     | 2    | Status / Control           |
| $FFFE32     | 2    | Rx/Tx Data                 |

**6850 ACIA register bits:**
- Status register: bit 0 = Rx Data Ready, bit 1 = Tx Data Register Empty, bit 2 = DCD, bit 3 = CTS, bit 4 = Framing Error, bit 5 = Receiver Overrun, bit 6 = Parity Error, bit 7 = IRQ
- Control register: bits 0-1 = Counter Divide Select, bits 2-4 = Word Select, bits 5-6 = Tx Control, bit 7 = Rx IRQ Enable

#### 2.5.3 Serial Ports — Expansion (68681 DUARTs, schematic sheets 8–9)

Expansion serial ports use Signetics 68681 DUART chips (SIO3–SIO6), providing ports 3–11. Each DUART handles two channels with 16 registers per channel.

| Address Base | DUART | Ports  |
|-------------|-------|--------|
| $FFFF01     | SIO3  | 4–5    |
| $FFFF11     | SIO4  | 6–7    |
| $FFFF61     | SIO5  | 8–9    |
| $FFFF71     | SIO6  | 10–11  |

Port numbering: Each SIO DUART is spaced 16 ($10) bytes apart. The boot ROM's port detection writes $55 to register offset 24 (scratch register) and reads it back to detect whether a port exists (function L15F8).

#### 2.5.4 SASI/SCSI Disk Interface (schematic sheet 10)

The AM-1200 uses a SASI (Shugart Associates System Interface, predecessor to SCSI) implementation built from discrete TTL logic — NOT a dedicated SCSI controller chip. This is essentially bit-banged SCSI through latched data/status/control registers. Confirmed identical register layout on both AM-1000 (sheet 8) and AM-1200 (sheet 10).

| Address     | Register | Description                           |
|-------------|----------|---------------------------------------|
| $FFFFE0     | Base     | Data register (R/W) / Bus status (R)  |
| $FFFFE1     | +1       | Error register (R) / Data out (W)     |
| $FFFFE2     | +2       | Sector count                          |
| $FFFFE3     | +3       | Sector number                         |
| $FFFFE4     | +4       | Cylinder low                          |
| $FFFFE5     | +5       | Cylinder high                         |
| $FFFFE6     | +6       | SDH (drive/head select)               |
| $FFFFE7     | +7       | Status (R) / Command (W)              |

**Bus status register bit mapping (from boot ROM SCSI code):**
- Bit 0: BSY
- Bit 1: BSY (reconfirmed in some contexts)
- Bit 2: REQ
- Bit 3: (reserved)
- Bit 4: I/O
- Bit 5: (reserved/variant)

The boot ROM supports multiple controller types selected by config DIP bits [3:0]:
- 0,1: WD1002-05 (ST-506/MFM Winchester — classic)
- 2: SCSI (primary, address $FFFFE0)
- 3: SMD (Storage Module Drive — alternate SCSI at $FFFFD8)
- 4: Floppy
- 5: SCSI Tape
- 6: SCSI Floppy
- 7+: SMD variants

#### 2.5.5 Timer (6840 PTM, schematic sheet 10)

| Address     | Register | Description         |
|-------------|----------|---------------------|
| $FFFD00+    | Timer    | 6840 Programmable Timer Module |

The 6840 provides 3 independent 16-bit timer channels. The AM-1000 schematic (sheet 8) shows it clocked from a 1 MHz input (via address decode timing), with the output driving interrupt logic. The AM-1200 uses a similar arrangement.

#### 2.5.6 Calendar/RTC (schematic sheet 12)

Real-time clock using a 32.768KHz crystal (XY4) with a dedicated RTC chip. The AM-1000 schematic (sheet 8, "TIME+DAY" section) shows an INS8250-style or similar RTC with 1024 Hz output. Address is generated by the I/O decoder on sheet 3 (RTCSRN# chip select).

#### 2.5.7 VCR Interface (schematic sheet 11)

A Z80A-based subsystem for controlling a video cassette recorder (used for backup storage). This has its own ROM (2784), RAM, and video circuitry. The AM-1000 schematics provide the most detailed view of this subsystem (sheets 10–13), including sub-master clock logic, TV sync generators (PAL/NTSC), serial-to-parallel converters, CRC logic, and VCR/Link select. For initial emulation, this can be stubbed as "not present."

#### 2.5.8 Z80 Multicomm Controller (AM-1000 sheet 9 / AM-1200 sheets 14–15)

A Z80 sub-processor with its own 2716 EPROM (2KB), static RAM, I/O ports, and command decoder. The AM-1000 schematics show: Z80 CPU clocked at 4 MHz, microcode ROM, memory select logic (RAMSEL), interface I/O ports (8 output channels C0–C7), and a command decoder (LS138/8131). The Z80 communicates with the 68010 through shared I/O registers and interrupt signaling (CMODWRT signal). For initial emulation, AMOS may probe for this — the emulator should return "not present" status when probed.

#### 2.5.9 Front Panel LED ($00FE00)

A single byte write to $FE00 controls the front panel 7-segment display. The boot ROM uses specific codes to indicate boot progress:

| LED Value | Meaning                        |
|-----------|--------------------------------|
| 0         | Trying next drive / success    |
| 1         | OS partition not found         |
| 2         | OS file not found / exhausted  |
| 3         | Floppy/tape init               |
| 6         | Hardware init starting         |
| 11        | Controller init in progress    |
| 14        | OS handoff (boot complete)     |
| 128+      | Diagnostic test codes          |

### 2.6 Interrupt Architecture

The 68010 uses a 3-bit encoded interrupt priority level (IPL0–IPL2). From schematic sheet 2, the interrupt encoder (U108, priority encoder) maps device interrupts to IPL levels:

| IPL Level | Source(s)                               |
|-----------|-----------------------------------------|
| 1         | Serial port (MAININT# from sheet 7)     |
| 2         | SASI disk controller (from sheet 10)    |
| 3         | Timer (6840)                            |
| 4         | SIO expansion (INTR from sheets 8–9)    |
| 5         | Multi-comm / VCR                        |
| 7         | NMI (power fail — PWFAIL, sheet 5)      |

### 2.7 DMA

The AM-1200 does NOT use DMA for disk transfers. All SASI/SCSI data transfer is PIO (Programmed I/O) — the CPU reads/writes one byte at a time in tight loops (confirmed by boot ROM code at L0640: `MOVB WD.ERR(A4),(A2)+` in 512-byte loops). This simplifies emulation significantly.

---

## 3. Boot Sequence (from AM-178-05 analysis, confirmed against binary)

Understanding the exact boot sequence is critical — it defines what hardware must work and when.

### 3.1 Reset Vector Fetch (Hardware)
1. CPU asserts RESET
2. Phantom ROM maps $800000 ROM to $000000
3. CPU reads SSP from $000000 → gets $00032400 (confirmed in binary)
4. CPU reads PC from $000004 → gets $800018 (confirmed in binary)
5. Phantom mapping disables, RAM reappears at $000000
6. CPU begins executing at $800018

### 3.2 Hardware Init (HWINIT, $800018)

Confirmed instruction-by-instruction against binary:
1. `MOVE.B #6,$FE00` — write 6 to LED: "hardware init starting"
2. `MOVE.B $FE03,D6` — read config DIP switch into D6
3. `MOVE.B #$40,$FE03` — pulse config port high
4. `MOVE.B #0,$FE03` — pulse config port low
5. `MOVEQ #9,D1` / `CLR.W (A7)` / `DBF D1,*` — clear 10 words of interrupt vectors at top of stack
6. `BTST #5,D6` — check config bit 5: if set → jump to diagnostic self-test
7. Copy 16KB ROM image from $800000 to RAM at $032400 (using `MOVE.L (A0)+,(A1)+` loop, $FFF+1 = 4096 longwords = 16384 bytes)
8. Set A5 = $032400 (workspace base)
9. Add offset $66 to A5, jump via `JMP (A6)` — continues in RAM copy at BTDSEL

### 3.3 Boot Device Selection (BTDSEL)
1. Re-read config DIP switch
2. Mask config bits [3:0] → index into controller dispatch table
3. Call controller-specific init (WD1002, SCSI, SMD, or Floppy)
   - SCSI init (L0544): bus reset, target probe, geometry auto-detect
   - Geometry detect (L0666) sets **offset mode** flag in WK.DP2 bit 0
   - When offset mode ON: SCSI read adds +1 to LBA (sector N → LBA N+1)
   - This skips physical sector 0 (hidden geometry sector)
4. Read "sector 0" (with offset: LBA 1 = disk label sector)
5. Check for disk label magic $AAAA5555 (byte-order invariant)
6. If labeled: install partition-aware scanner functions
7. If unlabeled (our AMOS 1.3 case): use default MFD scanner

### 3.4 Default MFD Boot Path (unlabeled disk — AMOS 1.3)

The boot ROM's "partition scanner" treats the MFD as a partition table. This works because AMOS PPNs happen to match the "partition type" codes the ROM looks for:

| ROM searches for | Value | MFD entry found | Meaning |
|-------------------|-------|-----------------|---------|
| Type 258 ($0102) | PPN [1,2] | OPR: account | "Boot partition" |
| Type 260 ($0104) | PPN [1,4] | SYS: account | "OS partition" |

**Detailed trace (confirmed against ROM binary + disk image):**

1. L01DA reads "sector 1" → with offset: LBA 2 → physical sector 2 → MFD
2. SASI transfers 512 bytes with PDP-11 byte-swap applied
3. After swap: MFD entry 0 word = $0102 (was disk bytes `02 01`)
4. `CMPW D1,@A2` with D1=$0102 → **match** at entry 0 → OPR:[1,2]
5. L01FA file search: reads UFD block 76 of OPR:[1,2]
6. Scans for boot file name pattern (AMOS.DIR at block 206)
7. Chain-loads boot file into RAM at workspace+4016
8. If system flag set → mark boot successful

### 3.5 OS Loading
1. Scan MFD for type 260 ($0104) → **match** at entry 1 → SYS:[1,4]
2. If not found: LED=1, retry loop
3. Search SYS:[1,4] UFD for OS system file (AMOSL.MON at block 104)
4. If not found: LED=2, retry loop
5. Chain-load OS into RAM starting at $000000
6. Set WHYBOT (boot reason code), OS flags, HLDTIM (1200 ticks)
7. Write 14 to LED — "OS handoff"
8. Jump to address stored at $000030 (OS entry point, offset 48)

### 3.6 Implications for Emulation

**Minimum hardware for boot ROM to complete:**
- 68010 CPU (instruction-accurate, with correct 8-byte exception frames)
- RAM at $000000–$03FFFF (at minimum 256KB)
- ROM at $800000–$803FFF (16KB, from interleaved binary pair)
- Phantom ROM-to-RAM mapping on reset (first 2 bus cycles only)
- LED port at $FE00 (write-only, for debugging)
- Config DIP switch at $FE03 (read-only, return $02 for SCSI boot)
- SASI/SCSI registers at $FFFFE0–$FFFFE7 (for disk boot)
- SASI controller with PDP-11 byte-swap on data transfer
- SCSI sector read with offset mode (+1 LBA adjustment)
- Disk image backend returning raw 512-byte sectors

**Can be deferred:**
- Serial ports (needed for console but not boot — OS writes to them later)
- Timer (OS uses for scheduling)
- RTC (OS uses for timestamps)
- VCR, Floppy (unless booting from those)
- Z80 multicomm (stub as "not present")

---

## 4. Emulator Architecture

### 4.1 High-Level Design

```
┌─────────────────────────────────────────────────┐
│              Emulation Scheduler                 │
│   (run N instructions, service I/O, repeat)     │
├─────────────────────────────────────────────────┤
│              MC68010 CPU Core                    │
│   (registers, ALU, all addressing modes,        │
│    exception handling, 68010 stack frames)       │
├─────────────────────────────────────────────────┤
│               Memory Bus                        │
│   (address decode → RAM / ROM / I/O dispatch)   │
├──────┬──────┬────────┬──────┬──────┬────────────┤
│ RAM  │ ROM  │ Serial │ SASI │Timer │ LED/Config │
│      │      │ Ports  │ Disk │ RTC  │            │
├──────┴──────┴────────┴──────┴──────┴────────────┤
│           Host Platform Interface               │
│   (terminal emulation, disk image files,        │
│    PTY/TCP for terminals, GUI for LEDs)         │
└─────────────────────────────────────────────────┘
```

### 4.2 Module Breakdown

```
alphasim/
├── cpu/
│   ├── mc68010.py          # CPU core (registers, fetch/decode/execute)
│   ├── instructions.py     # Instruction implementations
│   ├── addressing.py       # Addressing mode calculations
│   ├── exceptions.py       # Exception/interrupt processing
│   └── disassemble.py      # Built-in disassembler for debugging
├── bus/
│   ├── memory_bus.py       # Address decode, read/write dispatch
│   └── phantom.py          # ROM phantom mapping logic
├── devices/
│   ├── ram.py              # RAM (configurable size)
│   ├── rom.py              # ROM (load from binary image)
│   ├── led.py              # Front panel LED display
│   ├── config_dip.py       # Configuration DIP switch
│   ├── acia6850.py         # Motorola 6850 ACIA (serial ports 0-2)
│   ├── duart68681.py       # Signetics 68681 DUART (serial ports 3+)
│   ├── sasi.py             # SASI disk controller (TTL state machine)
│   ├── timer6840.py        # Motorola 6840 PTM
│   └── rtc.py              # Real-time clock
├── storage/
│   ├── disk_image.py       # AMOS disk image reader (from existing Python utility)
│   └── scsi_disk.py        # SCSI target emulation (backed by disk image)
├── terminal/
│   ├── console.py          # Terminal emulation (stdin/stdout or PTY)
│   └── serial_mux.py       # Multi-port serial multiplexer
├── debug/
│   ├── monitor.py          # Interactive debug monitor
│   ├── breakpoints.py      # Breakpoint/watchpoint engine
│   └── trace.py            # Instruction/memory access trace log
├── main.py                 # Entry point, configuration, main loop
└── config.py               # System configuration (RAM size, boot device, etc.)
```

### 4.3 CPU Core Design

The 68010 has approximately 70 base instructions with 14 addressing modes, producing hundreds of combinations. We implement:

**Approach:** Decode-dispatch using opcode lookup tables. Each instruction is a Python function taking (cpu, opcode_word) → None, modifying CPU state directly.

**Registers:**
```python
class MC68010:
    d = [0] * 8           # D0-D7 (32-bit data registers)
    a = [0] * 8           # A0-A7 (32-bit address registers, A7 = SP)
    pc = 0                # Program counter (32-bit, only 24 used)
    sr = 0x2700           # Status register (16-bit: T|S|III|XNZVC)
    usp = 0               # User stack pointer (when in supervisor mode)
    ssp = 0               # Supervisor stack pointer
    vbr = 0               # Vector Base Register (68010 — not used by ROM, but implement anyway)
    stopped = False        # STOP instruction state
    cycles = 0            # Cycle counter (approximate)
```

**Critical 68010 differences from 68000:**
1. Exception stack frame is 8 bytes (not 6) — 4-word frame with format/vector word — the boot ROM explicitly tests this
2. VBR register — exception vectors can be relocated (not used by boot ROM, but AMOS may use it)
3. MOVEC instruction — move to/from control registers (implement for completeness)
4. Loop mode for DBcc (not critical for correct execution)

### 4.4 Memory Bus Design

```python
class MemoryBus:
    def __init__(self):
        self.regions = []   # List of (start, end, device) sorted by address
        self.phantom = True # ROM phantom mapping active after reset

    def read(self, address, size):  # size: 1=byte, 2=word, 4=long
        address &= 0xFFFFFF        # 24-bit mask
        if self.phantom and address < 8:
            return self.rom.read(address + 0x800000, size)
        device = self._decode(address)
        return device.read(address, size)

    def write(self, address, size, value):
        address &= 0xFFFFFF
        if self.phantom:
            self.phantom = False    # Any write disables phantom
        device = self._decode(address)
        device.write(address, size, value)
```

### 4.5 SASI Disk Controller

This is the most complex peripheral. The AM-1200's SASI is NOT a standard chip — it's a TTL state machine (schematic sheet 10) that implements a simplified SCSI initiator protocol. The boot ROM drives it through explicit bus phase management.

**Emulation approach:** Implement a finite state machine that tracks SCSI bus phases (FREE → ARBITRATION → SELECTION → COMMAND → DATA → STATUS → MESSAGE → FREE) and responds to the register reads/writes that the boot ROM performs.

Key operations from the boot ROM:
1. Bus reset (write $80 to status register)
2. Target selection (write ID to SDH, poll for BSY)
3. Command phase (send 6-byte CDB one byte at a time via error register)
4. Data-in phase (read 512 bytes one at a time from error register, **with PDP-11 byte-swap**)
5. Status phase (read completion status)
6. Message phase (read/discard message byte)

**SCSI sector read with offset mode (SCREAD, L05EE):**
- If WK.DP2 bit 0 is set (offset mode): LBA = requested_sector + 1
- Builds 6-byte CDB: opcode $08 (READ), 3-byte LBA, transfer length 1
- PIO transfer: 512 iterations of `MOVB WD.ERR(A4),(A2)+`
- **Byte-swap:** Controller hardware swaps adjacent bytes during transfer, so disk byte pair [B0,B1] arrives as [B1,B0] in 68010 memory

The SCSI target (disk) is backed by the existing AMOS disk image utility, which already understands the AMOS filesystem, partition tables, block sizes, and chaining. Note: the disk utility reads with its own little-endian unpack (`struct.unpack('<...')`) which is the on-disk byte order. The emulator's SASI controller must apply the byte-swap when presenting data to the 68010.

### 4.6 I/O Device Base Class

```python
class IODevice:
    def read(self, address, size):
        raise NotImplementedError

    def write(self, address, size, value):
        raise NotImplementedError

    def tick(self, cycles):
        """Called periodically for devices that need time-based updates"""
        pass

    def get_interrupt_level(self):
        """Return current interrupt request level (0=none)"""
        return 0
```

---

## 5. Implementation Plan

### Phase 1: CPU + Memory + ROM Boot (Milestone: LED=6)

**Goal:** Execute the first instruction of the boot ROM.

1. Implement MC68010 CPU core with essential instructions
2. Implement RAM and ROM devices
3. Implement memory bus with phantom ROM mapping
4. Implement LED port (write-only, print to console)
5. Implement config DIP switch (return configurable value)
6. Load combined boot ROM binary (interleave the two EPROM images)
7. Execute reset sequence

**Test:** LED port receives value 6 (hardware init starting).

**Required 68010 instructions for Phase 1 (from boot ROM analysis):**
MOVE (byte/word/long), MOVEQ, MOVEA, MOVEP, LEA, PEA,
ADD, ADDA, ADDI, ADDQ, SUB, SUBA, SUBI, SUBQ,
AND, ANDI, OR, ORI, EOR, EORI, NOT, NEG,
CMP, CMPA, CMPI, TST, CLR,
BTST, BSET, BCLR, BCHG,
ASL, ASR, LSL, LSR, ROL, ROR, SWAP, EXT,
Bcc (BEQ, BNE, BCC, BCS, BHI, BLS, BPL, BMI, BGE, BLT, BGT, BLE, BRA),
DBcc (DBRA, DBF, DBNE, DBEQ),
Scc (SNE),
JMP, JSR, RTS, BSR,
LINK, UNLK,
MOVEM, TRAP, RTE, RTS,
MULU, MULS, DIVU, DIVS,
NOP, STOP

### Phase 2: Disk Bootstrap (Milestone: LED=14)

**Goal:** Boot ROM reads disk image, loads OS into RAM, reaches handoff.

1. Implement SASI controller state machine
2. Implement SCSI target disk backed by AMOS disk image
3. Support READ(6) command ($08) — the primary command during boot
4. Support TEST UNIT READY ($00), REZERO UNIT ($01), REQUEST SENSE ($03)
5. Handle disk label check ($AAAA5555 magic, byte-order invariant)
6. Handle chain-loaded sector reading

**Test:** LED displays 14 (OS handoff), CPU jumps to $000030.

### Phase 3: AMOS Kernel Alive (Milestone: Console prompt)

**Goal:** AMOS initializes and produces output on serial port 0.

1. Implement 6850 ACIA for serial port 0 (console)
2. Connect to host terminal (stdin/stdout or PTY)
3. Implement timer 6840 (AMOS needs this for scheduling)
4. Fix any 68010 instruction gaps found during OS init
5. Implement remaining SCSI commands the OS uses

**Test:** AMOS login prompt "AMOS x.x - DEV0:" appears on console.

### Phase 4: Multi-User + Full I/O (Milestone: Multi-terminal)

**Goal:** Full system emulation with multiple terminals.

1. Implement 68681 DUART for expansion serial ports
2. Implement serial port multiplexer (PTY or TCP sockets per port)
3. Implement RTC (AMOS needs date/time)
4. Complete interrupt system (all device interrupts)
5. Test AMOS program execution (EDIT, BASIC, compilers)

### Phase 5: Raspberry Pi Target (Stretch Goal)

1. Performance optimization (Cython hot paths, instruction cache)
2. Physical front panel LED via Pi GPIO
3. Physical serial port(s) via Pi UART/USB-serial
4. Auto-boot configuration (Pi boots directly into emulator)
5. Optional: hardware front panel replica with 7-segment displays

---

## 6. Asset Integration Plan

### 6.1 Boot ROM Binary ✅ RESOLVED

The ROM binary pair has been received and verified:

| File | Size | Role | Verification |
|------|------|------|-------------|
| AM-178-01-B05.BIN | 8192 bytes | Even (high) bytes D8–D15 | SSP=$00032400, PC=$00800018 |
| AM-178-00-B05.BIN | 8192 bytes | Odd (low) bytes D0–D7 | First instr: MOVE.B #6,$FE00 |

**Loading procedure for emulator:**
```python
rom01 = open("AM-178-01-B05.BIN", "rb").read()  # even bytes
rom00 = open("AM-178-00-B05.BIN", "rb").read()  # odd bytes
combined = bytearray(16384)
for i in range(8192):
    combined[2*i]     = rom01[i]
    combined[2*i + 1] = rom00[i]
# Load combined[] at address $800000
```

The combined ROM has been saved as `am1200_rom.bin` for direct loading.

### 6.2 AMOS Disk Image Format

**Image type:** Raw SCSI sectors, flat file. LBA × 512 = file byte offset.

**Disk geometry (from hidden sector 0, little-endian):**

| Offset | Size | Field | AMOS 1.3 value |
|--------|------|-------|----------------|
| 0 | 8 | Drive identity bytes | `05 03 04 03 05 04 03 00` |
| 16 | 4 | Formatted size (×100) | 6347 → 634,700 sectors |
| 24 | 4 | Number of logical devices | 14 |

**Logical device layout:**
- Physical sector 0: hidden geometry (never accessed by logical devices)
- Physical sector 1+: logical devices start
- Each logical device gets `formatted_size / num_logicals` sectors (= 45,335)
- DSK0 starts at physical sector 1; DSK1 at sector 45,336; etc.

**AMOS filesystem structure (per logical device):**
- Logical sector 0: Disk label ($AAAA5555 header if labeled; zeros if unlabeled)
- Logical sector 1: MFD (Master File Directory) — 63 × 8-byte PPN entries + 8-byte link
- Logical sector 2+: Bitmap (free-space tracking, all $FF = free)

**MFD entry format (8 bytes, little-endian words):**
- Word 0: PPN (low byte = programmer, high byte = project)
- Word 1: UFD block number (first User File Directory block)
- Words 2-3: Password (RAD50 encoded)

**UFD entry format (12 bytes, little-endian words):**
- Words 0-1: Filename (RAD50, 6 chars)
- Word 2: Extension (RAD50, 3 chars)
- Word 3: Status/attributes
- Word 4: First block number
- Word 5: File size (blocks)

**Chain linking:** Files span multiple sectors via chain links. The first word of each data sector is a link to the next sector (0 = last). Actual file data follows the link word (510 bytes per sector).

**AMOS 1.3 disk contents confirmed:**
- OPR:[1,2] — AMOS.DIR (directory file)
- SYS:[1,4] — AMOSL.MON (OS monitor), AMOSL.INI, CMDLIN.SYS, 100+ system commands
- DVR:[1,6] — Device drivers (ELS, LPR, MEM, TRM, SCZ, etc.)
- HLP:[7,0] — Platform monitors (ALPHA.AMX/.VUX for various hardware)
- OPR:[7,1] — Help files
- SYS:[7,7] — System sources, SYSLIB.LIB, symbol tables

### 6.3 AMOS Disk Utility Integration

**Repositories:**
- Disk utility: https://github.com/tniccum21/Alpha-Disk-Library
- Disassembler: https://github.com/tniccum21/amos_disassembler

**Integration approach:** The SCSI target emulation in `storage/scsi_disk.py` translates SCSI READ(6) LBA addresses directly to file offsets (`LBA × 512`). It does NOT use the disk utility's logical device abstraction — the boot ROM addresses raw physical sectors. The byte-swap (PDP-11 word ordering) is applied by the SASI controller during the PIO transfer loop, not by the disk backend.

The disk utility's `Alpha_Disk_Lib.py` can be used as a reference and debugging tool during development, but the emulator's disk path is deliberately simple: raw sector reads with byte-swap.

### 6.4 Documentation

Any manuals covering the following would be helpful (in priority order):
1. AMOS System Programmer's Guide (system calls, interrupt usage)
2. Hardware Technical Reference (if one exists beyond schematics)
3. AMOS device driver documentation (disk driver, serial driver)

---

## 7. Key Technical Risks & Mitigations

### 7.1 Undocumented Hardware Behavior
**Risk:** The SASI controller is built from TTL gates, not a standard chip. Its exact timing and state transitions may have quirks not visible in the boot ROM code.
**Mitigation:** Start with the boot ROM's usage patterns as the specification. If AMOS drivers behave differently, we have the AMOS source code and can analyze their expectations. The AM-1000 schematics provide an additional cross-reference for the SASI implementation.

### 7.2 AMOS OS Dependencies
**Risk:** AMOS may depend on subtle hardware timing (e.g., interrupt latency for serial I/O, timer tick rate for scheduling).
**Mitigation:** Implement configurable timing parameters. The boot ROM's delay loops give us calibration data (e.g., L338C multiplies by 45000 for delays). The 8 MHz clock on both AM-1000 and AM-1200 provides the timing baseline.

### 7.3 Python Performance
**Risk:** A 68010 at 8MHz executes roughly 1M instructions/second. Python interpretation overhead could make this unachievable.
**Mitigation:** 
- Start with pure Python for correctness
- Profile and identify hot paths (instruction decode, memory access)
- Cython-ize the CPU core and memory bus if needed
- On Raspberry Pi 4/5 (1.5-2.4 GHz ARM), Cython should be more than sufficient
- AMOS is a timesharing OS — it spends significant time idle-waiting, which can be accelerated

### 7.4 Instruction Set Completeness
**Risk:** Missing or incorrectly implemented 68010 instructions could cause silent corruption.
**Mitigation:** 
- Use the existing disassembler to identify all instruction forms in the ROM and OS images
- Build comprehensive instruction tests
- Add trap-on-unimplemented-opcode for early detection
- Cross-reference with Motorola 68000 PRM (Programmer's Reference Manual)

### 7.5 Z80 Multicomm Probing
**Risk:** AMOS may probe for the Z80 multicomm controller during initialization and hang if it gets unexpected results.
**Mitigation:** Implement a minimal stub at the multicomm I/O addresses that returns "not present" status. The AM-1000 schematics (sheet 9) provide the command decoder and I/O port layout needed if full emulation becomes necessary.

---

## 8. Open Questions

### 8.1 Resolved ✅

| # | Question | Answer |
|---|----------|--------|
| 1 | Raw ROM binary? | Yes — AM-178-00-B05.BIN + AM-178-01-B05.BIN (interleaved pair, verified) |
| 2 | GitHub URLs? | Alpha-Disk-Library + amos_disassembler repos provided |
| 3 | Clock speed? | 8 MHz (confirmed on both AM-1000 and AM-1200 schematics) |
| 4 | MMU? | PAL-based (16L8A) present on both systems; likely used for memory protection but not address translation |
| 5 | Multicomm? | Z80-based subsystem present on both AM-1000 and AM-1200 |
| 6 | Disk image format? | Raw SCSI sectors, flat file, 512 bytes/sector, LBA × 512 = offset |
| 7 | Boot device config? | SCSI = config DIP bits [3:0] = $02 |
| 8 | OS version? | AMOS 1.3 (primary target); also have 1.4 and 2.3 |
| 9 | Byte ordering? | PDP-11 heritage: disk data is little-endian words, SASI hardware byte-swaps during transfer |
| 10 | Boot magic? | $AAAA5555 = disk label header (byte-order invariant), NOT $AAD5AAD5 (annotation error) |
| 11 | Boot path? | Default MFD scanner: type 258 = PPN [1,2] (OPR:), type 260 = PPN [1,4] (SYS:) |
| 12 | OS file? | AMOSL.MON in SYS:[1,4] at block 104 |

### 8.2 Still Pending

| # | Question | Impact |
|---|----------|--------|
| 1 | **Sector size:** 512 bytes confirmed for SCSI. Do any AMOS versions use different block sizes? | Likely always 512, but worth confirming |
| 2 | **SCSI target ID:** What ID does the drive respond on? Boot ROM probes with $E1 (225). | Need to decode target selection logic |

---

## 9. Naming & Conventions

- **Project name:** AlphaSim
- **Language:** Python 3.10+ (f-strings, match/case, type hints)
- **Number format:** Hex with $ prefix in documentation (matching AMOS convention), 0x prefix in Python code
- **Addressing:** All addresses are 24-bit ($000000–$FFFFFF)
- **Byte order:** Big-endian (68000 native)
- **Source organization:** One class per hardware device, one file per module
- **Testing:** pytest, with hardware register tests for each device

---

*End of specification — Version 0.2 DRAFT*
