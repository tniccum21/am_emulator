# Resume Note

Last updated: 2026-03-23
Branch: `feature/native-boot-milestones`

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
