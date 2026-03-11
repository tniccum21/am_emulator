#!/usr/bin/env python3
"""Run a batch of native AMOSL.INI miss-path experiments."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
TRACER = REPO_ROOT / "trace_native_cmdfile_jobq.py"
LOG_DIR = Path("/tmp/native_cmdfile_matrix")

COMMON_ARGS = [
    "--force-a060-gate",
    "--suppress-timer-after-a060=5000",
    "--max-after-fix=4000000",
]


@dataclass(frozen=True)
class Variant:
    name: str
    extra_args: tuple[str, ...]


VARIANTS = (
    Variant("base", ()),
    Variant("preserve_desc12", ("--preserve-desc12-at-1d14",)),
    Variant("promote_desc12", ("--promote-a086-to-desc12",)),
    Variant("promote_a060_block", ("--promote-a086-to-a060-block",)),
    Variant(
        "preserve_plus_promote_desc12",
        ("--preserve-desc12-at-1d14", "--promote-a086-to-desc12"),
    ),
    Variant(
        "promote_a060_block_prime",
        ("--promote-a086-to-a060-block", "--prime-a086-target-from-desc"),
    ),
    Variant(
        "preserve_plus_promote_desc12_prime",
        (
            "--preserve-desc12-at-1d14",
            "--promote-a086-to-desc12",
            "--prime-a086-target-from-desc",
        ),
    ),
)


def parse_summary(stdout: str) -> dict[str, str]:
    for line in reversed(stdout.splitlines()):
        if not line.startswith("SUMMARY "):
            continue
        result: dict[str, str] = {}
        for field in line.split()[1:]:
            key, value = field.split("=", 1)
            result[key] = value
        return result
    raise ValueError("missing SUMMARY line")


def run_variant(variant: Variant) -> tuple[dict[str, str], Path]:
    cmd = [sys.executable, str(TRACER), *COMMON_ARGS, *variant.extra_args]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{variant.name}.log"
    log_path.write_text(proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""))
    summary = parse_summary(proc.stdout)
    summary["returncode"] = str(proc.returncode)
    summary["log"] = str(log_path)
    return summary, log_path


def fmt_bool(value: str) -> str:
    return "Y" if value == "1" else "."


def format_row(name: str, summary: dict[str, str]) -> str:
    pre = f"{summary['last_a086_pre_a6']}->{summary['last_a086_a6']}"
    return (
        f"{name:<34} "
        f"lba={summary['last_lba']:<4} "
        f"ini={fmt_bool(summary['reached_amosl_ini'])} "
        f"reason={summary['reason']:<12} "
        f"desc12={summary['desc12']} "
        f"a060={summary['a060_block']} "
        f"a086={pre} "
        f"prom={summary['a086_promotions']:<2} "
        f"prime={summary['a086_primes']:<2} "
        f"pres={summary['preserve_hits']:<2} "
        f"55AA={fmt_bool(summary['saw_55aa'])} "
        f"56BC={fmt_bool(summary['saw_56bc'])} "
        f"56D2={fmt_bool(summary['saw_56d2'])} "
        f"pc={summary['pc']}"
    )


def main() -> int:
    print("Running native miss-path experiment matrix...")
    results: list[tuple[Variant, dict[str, str]]] = []
    for variant in VARIANTS:
        print(f"  {variant.name}")
        summary, _ = run_variant(variant)
        results.append((variant, summary))

    print("\nSummary:")
    for variant, summary in results:
        print(format_row(variant.name, summary))

    winners = [
        (variant, summary)
        for variant, summary in results
        if int(summary["last_lba"]) > 3326 or summary["reached_amosl_ini"] == "1"
    ]
    print("\nConclusion candidate:")
    if winners:
        variant, summary = winners[0]
        print(
            f"  {variant.name} advanced disk I/O to LBA {summary['last_lba']} "
            f"(AMOSL.INI reached={summary['reached_amosl_ini']})."
        )
    else:
        print(
            "  No tested variant advanced disk I/O past LBA 3326. "
            "If any variant changed the A086 call shape but still stalled, the next target is "
            "request contents/branch-state rather than descriptor +$12 alone."
        )

    print("\nLogs:")
    for variant, summary in results:
        print(f"  {variant.name}: {summary['log']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
