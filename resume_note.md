# Resume Note

Last updated: 2026-03-24
Branch: `feature/native-boot-milestones`

## Latest Checkpoint

The current native frontier depends on **which disk image is under test**.
That distinction was blurred in the last checkpoint and needs to be explicit.

For the selector/feature image used by the PIT regression
(`HD0-V1.4C-Bootable-on-1400.img`):

- the native `cpu_model=68020` path still reaches the PIT-backed milestone
  checks in `tests/integration/test_boot_native_pit_irq.py`
- those tests currently prove:
  - native level-6 handler reachability at `$0018E0`
  - later monitor reachability at `$001DBE`

For the **real boot image** (`AMOS_1-3_Boot_OS.img`), which is the one that
matters for the `AMOSL.INI` question:

- native boot still does **not** reach `AMOSL.INI`
- the tracer in `trace_native_amosl_ini_path.py` now confirms this on the
  current tree with no ambiguity:
  - target `AMOSL.INI` read is still `LBA 3335`
  - the highest native disk read remains `LBA 3326`
  - so the current real-boot frontier is still **earlier than `AMOSL.INI`**
- a clean `cpu_model=68020` run on that image now ends at:
  - `pc=$001C72`
  - LED history `06 0B 00 0E 0F 00`
  - `JOBCUR=$00FFFE11`
  - `SYSBAS=$000070C0`
  - `JCB+$20=$000B`
  - `JCB+$38=$003E8000`
- the final low-memory sysvars at that later stop are clearly invalid:
  - `JOBCUR=$00FFFE11`
  - `JOBQ=$00000000`
  - `DDBCHN=$00007038`
  - `ZSYDSK=$00006C3A`

The strongest concrete lead on the real boot image is now the late
command-file/job-queue regime around the old `3326` ceiling:

- `trace_native_sysvar_corruption.py` records the real early sysvar setup:
  - `ZSYDSK`: `$00000000 -> $00007030` at `pc=$033186`
  - `JOBCUR`: `$00000000 -> $00007038` at `pc=$00819A`
  - `ZSYDSK`: `$00007030 -> $00007AC2` at `pc=$0081B6`
- after that, the only repeating late sysvar motion before the ceiling is:
  - `pc=$001230`: `JOBCUR $7038 -> 0`
  - `pc=$001338`: `JOBCUR 0 -> $7038`
- `trace_native_amosl_ini_path.py` shows the same real-boot state with:
  - `JCB+$20=$000B`
  - `JCB+$38=$003E8000`
  - no reads beyond `LBA 3326`

So the next real target is not ACIA output and not the selector-image PIT
frontier. It is the earlier real-boot filesystem / command-file / job path,
especially the `$001230/$001338` `JOBCUR` ping-pong and the
later drift from the clean `$7038/$7AC2` regime into the final bad sysvars.

Additional current-tree refinement:

- the old scheduler hypothesis from `docs/HANDOFF-2026-03-10.md` still
  reproduces in part:
  - on the natural path, `USP` is still zero at the first late queue
    producer/dequeue cycle
  - first clean natural hits now confirm:
    - `$00122C = MOVE.L (A3),($041C).W`
    - `$001230 = CLR.L (A3)+`
    - `$001338 = CLR.L 120(A0)` i.e. clear `JCB+$78`
  - at those first hits:
    - `A0 = $7038`
    - `A3 = $70B0` at `$00122C/$001230`
    - `A3 = $70B4` at `$001338`
    - `USP = $00000000`
    - `JCB+$78/$70B0 = 0`
    - `JCB+$7C/$70B4 = 0`
    - `JCB+$80/$70B8 = 0` at dequeue time, then later becomes `$00007724`
- but the older one-shot `USP=$00032400` seed at `$006B7A` no longer reaches
  the deeper late `AMOSL.INI` miss path on the current tree
- instead, that seed now stabilizes the run in the later runnable-chain loop:
  - `$0013D2 = MOVE USP,A6`
  - `$0013F2 = MOVE.L 120(A6),D7`
  - `$0013F6 = BEQ ...`
  - `$0013F8 = MOVEA.L D7,A6`
  - `$0013FA = BRA $0013F2`
- on that seeded path:
  - the run still stops at `last_lba = 3326`
  - it still does not reach `AMOSL.INI`
  - final stable loop is `$0013F2/$0013F6/$0013F8/$0013FA`
  - `last_a086_pc` stays zero and `saw_55aa/56BC/56D2` stay false

So the current real-boot next step is to explain the natural producer side
around `$00122C/$001230/$001338`, and then the seeded-path runnable-chain loop
around `$0013D2/$0013FA`, rather than assuming the older March-10 `USP` seed
still carries the run into the repaired late `AMOSL.INI` path.

The native timer/wake source is no longer an unresolved mystery. The loaded
monitor is actively using a second PIT-style timer block at
`$FFFE60-$FFFE67`, not just the MC6840 PTM at `$FFFE10-$FFFE1F`.

What is now implemented in code:

- `alphasim/devices/timer8253.py` adds the minimal native timer block the
  monitor actually touches:
  - counter/data ports at `$FFFE60`, `$FFFE62`, `$FFFE64`
  - control/latch writes at `$FFFE61`, `$FFFE63`, `$FFFE66`
  - level-6 autovectored interrupt support for the native timer path
- the device is wired in `alphasim/main.py`
- direct regressions now cover:
  - `tests/devices/test_timer8253.py`
  - `tests/integration/test_boot_native_pit_irq.py`
  - `tests/cpu/test_rte.py`

What the new trace work proved before the patch:

- the live delayed-queue path does **not** arm `$FFFE13` on this image
- at `$0019A8`, the native path sees `($0403).W = $A4`, takes `BMI $0019D8`,
  and programs `$FFFE62/$FFFE63` instead
- the vector-30 ISR at `$0018E0` also branches to `$001944` on this path and
  acknowledges `$FFFE60/$FFFE61`, confirming the MC6840-only theory was wrong
- the monitor also initializes the same block near `$00F182` with:
  - `$FFFE66 <- $B6`
  - `$FFFE64 <- $14`, `$00`
  - `$FFFE66 <- $30`, `$FFFE61 <- $00`
  - `$FFFE66 <- $70`, `$FFFE63 <- $00`

What this changes in practice:

- clean native `cpu_model=68020` boot now reaches the native level-6 handler
  at `$0018E0`
- the old `$001C90/$001CAC` idle-loop frontier is gone
- the run reaches later loaded-monitor code at:
  - `$001DBE` by about `4,043,337` instructions
  - and then `$001EB2` by `8,000,000` instructions
- by that later point:
  - `QHEAD = $00000000`
  - `EVBUSY = $0000`
  - `JOBCUR = $0000A86E`
  - LED history remains `06 0B 00 0E 0F 00`

Important continuity note:

- the old boot-shaped low-memory SCSI milestone tests are now stale because
  the native PIT wake path bypasses that previous dead-end
- those boot milestones were retired and replaced with:
  - direct device coverage in `tests/devices/test_scsi_bus.py`
  - direct RTE-helper coverage in `tests/cpu/test_rte.py`
  - native PIT boot coverage in `tests/integration/test_boot_native_pit_irq.py`

Current verification baseline:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/cpu/test_rte.py tests/devices/test_scsi_bus.py tests/devices/test_timer6840.py tests/devices/test_timer8253.py tests/integration/test_boot_native_cpu_probe.py tests/integration/test_boot_native_pit_irq.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'
```

Latest results:

- focused PIT/native subset: `13 passed`
- broader integration subset: `22 passed, 1 skipped, 1 deselected`

The post-SCSI native frontier is now narrowed further than the earlier
`JCB+$78` theory: the live blocker is the delayed-event queue/timer wake path,
not a missing successor-job link.

What still holds from the last code fix:

- privilege-violation handlers now stack the faulting opcode PC
- the synthetic 68000-style compatibility frame for vectors 8/9/26 matches
  the monitor helpers well enough to eliminate the older bogus jumps to
  `$190000`, `$083A10`, and `$1784B6`
- the first vector-26 helper still returns to `PC=$001C98`, `SR=$0019`

What the new queue trace proves on the clean native `cpu_model=68020` path:

- the first scheduler dequeue at `$001C48/$001C4C` still clears:
  - `JOBCUR = $0000A86E -> 0`
  - `JCB+$78 = $00000000`
- but that is no longer the best causal frontier
- the machine also has a live delayed-event queue:
  - `$001A2A` writes `($042A).L = $00007BC2`
  - `$001980` sets `($046E).W = $00FF`
- the queued block at `$7BC2` is not the older `$1B00` callback node:
  - `link = $00000000`
  - `delay = $0000C350`
  - `callback = $00000000`
  - `owner = $00000000`
- that block comes directly from the loaded monitor's `$00A2F0` path, which
  seeds the work area and then queues it

The important negative findings are now:

- by `8,000,000` instructions, none of the timeout-service PCs are reached:
  - `$001902`
  - `$001910`
  - `$001920`
  - `$001924`
  - `$00227C`
  - `$002280`
- the queue-consumer/callback side also never runs in that window:
  - `$00199C`
  - `$001B00`
  - `$001D10`
  - `$001D80`
  - `$001D86`
- the queued delay value at `$7BC6` stays unchanged at `$0000C350`
- after the first dequeue, the machine sits in the idle/wait loop around
  `$001C90/$001CAC` with:
  - `QHEAD = $00007BC2`
  - `EVBUSY = $00FF`
  - `WAKE0 = $0000`
  - no new interrupt taken in the open interrupt window at `$001C94`

So the next real target is now:

- what periodic source is supposed to service/decrement the queued block at
  `$7BC2`
- why the native timeout-service path never runs before the later low-memory
  corruption at `pc=$002584`

For continuity, the current queue trace is preserved in:

- `trace_native_delay_queue.py`

## Where We Left Off

The missing `$FFFE40-$FFFE5F` hardware bank is now implemented in the emulator
as [alphasim/devices/rtc_direct_bank.py](./alphasim/devices/rtc_direct_bank.py),
wired in [alphasim/main.py](./alphasim/main.py), and covered by:

- [tests/devices/test_rtc_direct_bank.py](./tests/devices/test_rtc_direct_bank.py)
- [tests/integration/test_boot_native_clock_bank.py](./tests/integration/test_boot_native_clock_bank.py)

This is a real dependency, not a guess:

- with a one-time forced `SYSTEM |= $00008000` at the first `$003EBE`
  hit, native code now touches `$FFFE5A/$FFFE4C..$FFFE58`
- in that forced-path probe the run reaches `PC=$0078E4` after `4,043,540`
  instructions with LED history `06 0B 00 0E 0F 00`

So the higher path no longer depends on an ad hoc scratch stub.

## Current Frontier

The next blocker is upstream of the `$003EBE` gate.

What is now concrete for the default `cpu_model=68010` path:

- first clean hit of `$00ED40` occurs at roughly `937,891` instructions
  with:
  - `D1 = $00000002`
  - `SYSTEM = $00300404` before the ladder updates it
  - after the ladder, `SYSTEM = $00300424`
- the last live writes to `D1` before that point are now:
  - `$00F8F2: MOVEQ #1,D1`
  - `$00F8F8: MOVEQ #2,D1`
- that `D1 = 2` result is selected because unsupported `MOVEC CACR`
  now raises the illegal-instruction exception and the monitor follows its
  built-in fallback path
- immediately before that helper, the clean run repeatedly does:
  - `MOVE.L ($0400).W,D6`
  - mask/test helper
  - `D6` collapses back to zero each time against `SYSTEM=$00300404`

Most important selector experiment:

- forcing `SYSTEM |= $08000000` once at `$00F7B8` changes the clean result:
  - `D1` at `$00ED40` becomes `$00000008`
  - the run takes the direct `$00F898 -> MOVEQ #8,D1` path instead of
    falling through to the `$00F8AE/$00F8EA` fallback
  - the native ladder then sets the missing low `$00008000` bit itself
  - resulting `SYSTEM` after the ladder is `$0830A404`
- forcing `SYSTEM |= $00040000`, `SYSTEM |= $00080000`, or a simple latched
  byte at `$FFFF59` did **not** change the clean `D1=$00000004` result

Additional constraints from the latest pass:

- all three monitor files on disk seed the same low `SYSTEM` image:
  - `AMOSL.MON`, `AMS4.MON`, and `TEST4.MON` each have
    `SYSTEM=$00300400` at offset `$0400`
- the clean native path performs only two `SYSTEM` writes before `$00ED40`:
  - `$032568 -> $00300404`
  - `$00ED4C -> $00300424`
  - there is still **no** native pre-`$00ED40` writer for `$08000000`
- `AMS4.MON` reaches the same selector result as `AMOSL.MON` after the
  `MOVEC` fix:
  - `D1=$00000002` at `$00ED40`
  - `SYSTEM` updates to `$00300424`
- `TEST4.MON` diverges elsewhere before it reaches `$00ED40`, so it is not a
  clean comparator for this selector issue
- the first visible hardware probe in the surrounding setup path is at
  `$FFFF59` from inside the `$00F982` helper:
  - clean run: read `$FF`, write `$55`, read `$FF`, restore `$FF`
  - an explicit byte latch at `$FFFF59` changes the readback to `$55`, but
    still does **not** change the later `D1=$00000004` result
- real Capstone disassembly of the selector family now explains the branch:
  - `$00F85E`: `SYSTEM & $04000000` -> direct `D1=4`
  - `$00F872`: `SYSTEM & $00080000` -> direct `D1=4`
  - `$00F886`: `SYSTEM & $08000000` -> direct `D1=8`
  - otherwise `$00F89E -> $00F8AE` fallback runs
- the `$00F8AE` fallback is a CPU-feature probe, not a storage/device probe:
  - it installs a temporary exception return at `$00F90A`
  - executes `MOVEC CACR,D6`
  - ORs `#$80000200` into `D7`
  - writes `D7` back to `CACR`
  - reads `CACR` back into `D7`
  - restores the original `CACR`
  - then classifies:
    - bit 31 survives -> `D1=16`
    - else bit 9 survives -> `D1=8`
    - else -> `D1=4`
- the later `$00ED40` ladder is now decoded exactly:
  - `D1 bit 1` -> `SYSTEM |= $00000020`
  - `D1 bit 2` -> `SYSTEM |= $00002000`
  - `D1 bit 3` -> `SYSTEM |= $00002000 | $00008000`
  - `D1 bit 4` -> `SYSTEM |= $00002000 | $00008000 | $10000000` and extra
    setup
- the emulator’s current `MOVEC` implementation in
  `alphasim/cpu/instructions.py` returns zero for unsupported control
  registers such as `CACR` instead of trapping or behaving by CPU model
  - this is now fixed: unsupported `MOVEC` control registers trap as illegal
    instruction (vector 4), which lets the monitor take its own fallback path
- there is now a minimal CPU-model selector in the emulator:
  - default `cpu_model=68010` keeps `MOVEC CACR` illegal and yields `D1=2`
  - `cpu_model=68020` exposes a minimal `CACR` where bit 9 sticks
  - `cpu_model=68030` / `68040` expose a minimal `CACR` where bits 31 and 9
    stick
- with `cpu_model=68020`, the selector now reaches `$00ED40` naturally with:
  - `D1 = $00000008`
  - `SYSTEM = $00300404` before the ladder
  - the downstream `$00ED40` ladder therefore has a native path that can set
    low `$00008000` without any ad hoc `SYSTEM` forcing
- a direct ad hoc native probe with `cpu_model=68020` and no other forcing now
  advances well beyond the selector:
  - after `5,000,000` instructions it reaches `PC=$00A2B4`
  - LED history is `06 0B 00 0E 0F 00`
  - no ACIA transmit has occurred yet
  - no `AMOSL.INI` read has occurred yet
  - the last observed disk read at that cutoff was `LBA=2302`
- the current `68020` frontier is now narrowed to the monitor-side
  `$FFFFC8/$FFFFC9` SCSI handshake code in loaded RAM:
  - the hot loop at the later cutoff is `PC=$00A2B4/$00A2B6/$00A2BA`
  - that code decodes as:
    - `$00A2B4: MOVE.B (A5),D7`
    - `$00A2B6: ANDI.B #$02,D7`
    - `$00A2BA: BNE $00A2B4`
    - followed by `$00A2BC/$00A2C0` clearing `$FFFFC8/$FFFFC9`
  - at that point:
    - `A5 = $FFFFC8`
    - `A4 = $003E0188`
    - `A6 = $00007BC2`
    - `SYSTEM = $0030A404`
- the first concrete selection sequence on that path is now measured:
  - the monitor writes `$00`, `$01`, `$11` to `$FFFFC8`
  - the emulator moves the SCSI bus interface to `COMMAND`
  - after that, the monitor repeatedly reads `$FFFFC8` and currently sees
    raw status `$16` forever
  - no CDB byte writes to `$FFFFC9` occur in the traced window
- the surrounding loaded monitor code around `$00A2F0` is also now decoded:
  - it initializes a small timeout/work block at `A2=$007BC2`
    - `+0 = 0`
    - `+4 = $0000C350`
    - `+8 = 0`
  - then calls `LINE-A $A044`
  - `A044` currently runs through the existing low-memory delayed-callback
    queue path (`$001A14/$00197C/...`) using the same free-list node family
  - after that setup, execution returns to the `$00A30A/$00A320` handshake
    logic against `$FFFFC8`
- one direct protocol experiment is now ruled out:
  - forcing the SCSI status builder to expose `REQ` during `COMMAND`
    (`$17` instead of `$16`) does **not** change behavior
  - the monitor still polls `$FFFFC8` repeatedly and still does not emit any
    CDB writes or DMA activity in the traced window
- that alias-handshake frontier is now repaired in real emulator code:
  - the SCSI bus interface now models the monitor's observed two-stage
    selection handshake
  - after the first `00/00/01/11` sequence, `$FFFFC8` now reports `$14`
    (pending command handshake) instead of going straight to `$16`
  - data writes during that pending stage are ignored
  - the second `00/00/01/11` sequence then enters real `COMMAND` phase
- with that protocol fix in place, the native `68020` path now reaches the
  first real low-memory command submission:
  - the monitor emits `READ(10)` CDB `28 00 00 00 00 02 00 00 01 00`
  - the SCSI bus interface executes `READ lba=2 count=1`
  - this happens at the low-memory alias command writer around
    `$00A320/$00A348/$00A352`
  - the path also advances beyond the earlier idle wait and reaches LED `$12`
    in longer runs

So the strongest current diagnosis is:

- the previous `D1=4` selector result was an emulator bug caused by silent
  `MOVEC CACR` zero reads
- the clean native path is now explicitly taking the monitor’s fallback
  classification path and arriving at `D1=2`
- AM-1400-style higher CPU classification no longer needs a fake `SYSTEM`
  write; it can now be exercised natively by selecting `cpu_model=68020`
- the first successful low-memory `READ(10)` is now followed by a different
  native frontier than the old PTM-driven low-memory loop:
  - by LED `$12`, the machine is in the idle/scheduler wait around
    `$001C6C..$001CB6`
  - the active JCB at `$A86E` has:
    - `JCB+$0C = $003E017C`
    - `JCB+$20 = $00000A01`
    - `JCB+$78 = $00000000`
    - `JCB+$7C = $0000B440`
    - `JCB+$80 = $0000B0B8`
  - `SYSTEM = $0030A404`
  - `DEVTBL = $003E014E`
  - `DDBCHN = $00007BA2`
  - `ZSYDSK = $0000B440`
  - `SYSBAS = $00000000`
  - `JOBCUR = $00000000`
  - `DRVVEC = $0000632C`
- on this `68020` path, the PTM is *not* the current wake source:
  - by LED `$12`, the MC6840 still sits at power-on defaults
    (`CR1=$00 CR2=$01 CR3=$00`, all latches zero, no flags set)
  - no live PTM access is observed in the measured window that reaches the
    LED `$12` idle loop
- the scheduler-side producer edge is now sharper:
  - `$001D80`: `MOVE.L ($041C).W,120(A0)` writes the old `JOBCUR` into the
    successor link
  - `$001D86`: `MOVE.L A0,($041C).W` restores `JOBCUR = $A86E`
  - but `JOBCUR` is already zero at `$001D80`, so `JCB+$78` is written as
    zero and the scheduler later drops back into the idle loop
- the ACIA side is now the strongest new hardware lead:
  - clean native state at the LED `$12` idle loop:
    - port 0 control register stays `$00`
    - `DRVVEC` remains the dummy stub `$632C`
    - the ACIA level-1 vector is live at `vector64 = $00001358`
  - injecting a byte at the idle loop with the clean `CR=$00` state does
    nothing useful:
    - `RDRF` becomes set
    - `JOBCUR` stays zero
    - `PC` stays in the `$001C90` idle window
  - forcing `CR=$95` (RX IRQ enabled) and then injecting a byte immediately
    breaks the machine out of the idle loop and into live level-1 interrupt
    work

So the next real target is no longer the alias SCSI bus or the PTM. It is the
native console wake path after LED `$12`: why port 0 remains at `CR=$00`, and
what exact ACIA configuration/IRQ behavior is required for the existing
level-1 handler at vector `$1358` to resume native progress cleanly.

## Useful Commands

Quick verification baseline:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/devices/test_timer6840.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/cpu/test_movec.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/devices/test_scsi_bus.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/devices/test_rtc_direct_bank.py tests/devices/test_rtc_msm5832.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration/test_boot_native_cpu_probe.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration/test_boot_native_scsi_dma_irq.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration/test_boot_native_scsi_alias_command.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration/test_boot_native_clock_bank.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'
printf 'VER\nDIR\nBYE\n' | python3 patch_driver_v7.py 2>/dev/null
```

Current selector tracer:

```bash
python3 trace_system_selector.py
python3 trace_system_selector.py --cpu-model 68020
python3 trace_system_selector.py --system-mask 0x08000000
```

Current SCSI-handshake probes:

```bash
python3 - <<'PY'
from pathlib import Path
from alphasim.config import SystemConfig
from alphasim.main import build_system
from alphasim.devices.scsi_bus import SCSIBusInterface

config = SystemConfig(
    rom_even_path=Path("roms/AM-178-01-B05.BIN"),
    rom_odd_path=Path("roms/AM-178-00-B05.BIN"),
    ram_size=0x400000,
    config_dip=0x0A,
    disk_image_path=Path("images/HD0-V1.4C-Bootable-on-1400.img"),
    cpu_model="68020",
)
cpu, bus, led, _acia = build_system(config)
scsi = next(dev for _, _, dev in bus._devices if isinstance(dev, SCSIBusInterface))
log = []
scsi.trace_callback = log.append
cpu.reset()
for _ in range(4_200_000):
    cycles = cpu.step()
    bus.tick(cycles)
    if len(log) >= 120:
        break
print(cpu.pc & 0xFFFFFF, [f"{x:02X}" for x in led.history], log[:20], log[-20:])
PY
```

Current idle/ACIA frontier tracer:

```bash
python3 trace_native_idle_acia.py
python3 trace_native_idle_acia.py --force-rx-irq
```

Ad hoc frontier probe:

```bash
python3 - <<'PY'
from tests.integration.boot_helpers import RecordingTarget, build_native_boot_system, find_amosl_ini_start_block, run_native_boot
from tests.integration.test_boot_native_cpu_probe import SELECTOR_IMAGE

cpu, bus, led, acia, sasi = build_native_boot_system(SELECTOR_IMAGE, cpu_model="68020")
recording_target = RecordingTarget(sasi.target)
sasi.target = recording_target
state = {"first_tx": None, "first_ini_read": None}

def tx_callback(port, value):
    if state["first_tx"] is None:
        state["first_tx"] = (port, value, cpu.pc, cpu.cycles, tuple(led.history))

acia.tx_callback = tx_callback
cpu.reset()
target_lba = find_amosl_ini_start_block()
target_lba = None if target_lba is None else target_lba + 1

def stop():
    if state["first_ini_read"] is None and target_lba is not None:
        for read in recording_target.read_calls[-2:]:
            if read.lba >= target_lba:
                state["first_ini_read"] = (read.lba, cpu.pc, cpu.cycles, tuple(led.history))
                break
    return state["first_tx"] is not None or state["first_ini_read"] is not None

result = run_native_boot(cpu, bus, led, stop=stop, max_instructions=5_000_000)
print(result, state, recording_target.read_calls[-1] if recording_target.read_calls else None)
PY
```

Interpretation:

- plain run should show `ed40_d1=$00000002`
- `--cpu-model 68020` should show `ed40_d1=$00000008`
- forced `0x08000000` run should show `ed40_d1=$00000008` and a
  `system_after` value with low `$00008000` present

## 2026-03-25 Update

- Two native exception fixes are now in tree in
  [alphasim/cpu/exceptions.py](/Volumes/RAID0/repos/am_emulator/alphasim/cpu/exceptions.py):
  - vector `8` uses the normal `68000` short frame `[SR][PC]` again
  - `RTE` preserves supervisor mode for the low-byte SR helpers at
    `$003DDA/$003DE2`, similar to the existing vector-26 quirk
- Added regression coverage in
  [tests/cpu/test_rte.py](/Volumes/RAID0/repos/am_emulator/tests/cpu/test_rte.py)

What this fixed:

- the native privilege helper at `$000AFC` now sees the correct fault PC via
  `MOVEA.L 6(A7),A1`; at `$000B04`, `A1` is now `$00001290` instead of the
  bogus `$12900000/$12900009`
- the old bogus jump into the RAM memtest fill pattern at `$010000`
  (`$52525252`) is gone
- the real boot image no longer falls into the previous low-memory corruption
  regime with `JOBCUR=$00FFFE11`

Current real-boot frontier:

- a direct `12,000,000`-instruction probe on `AMOS_1-3_Boot_OS.img` now ends at:
  - `PC=$001278`
  - `A7=$000006F2`
  - `SR=$2704`
  - `JOBCUR=$00000000`
  - `SVSTK=$000006F4`
- disk activity still stops at `LBA 3326`
- there is still no native `AMOSL.INI` read (`target LBA 3335`) and no ACIA TX

The new steady loop after `5M` instructions is the native scheduler /
interrupt-window family:

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

So the next target is no longer stack corruption. It is why the machine sits in
that stable native loop with `JOBCUR=0` during pre-`AMOSL.INI` filesystem /
command-file work, instead of making the next command-file/job transition
toward `AMOSL.INI`.

Verification:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/cpu/test_rte.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/integration -k 'not test_native_boot_reads_amosl_ini_before_terminal_output'
python3 trace_native_stack_provenance.py
```

Results:

- `tests/cpu/test_rte.py` -> `4 passed`
- integration subset -> `22 passed, 1 skipped, 1 deselected`
