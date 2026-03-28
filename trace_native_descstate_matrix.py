#!/usr/bin/env python3
"""Run descriptor-state experiments around the native AMOSL.INI miss path."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
TRACER = REPO_ROOT / "trace_native_cmdfile_jobq.py"
LOG_DIR = Path("/tmp/native_descstate_matrix")

COMMON_ARGS = [
    "--force-a060-gate",
    "--suppress-timer-after-a060=5000",
    "--max-after-fix=4000000",
    "--preserve-desc12-at-1d14",
]


@dataclass(frozen=True)
class Variant:
    name: str
    extra_args: tuple[str, ...]


VARIANTS = (
    Variant("base_preserve", ()),
    Variant(
        "rec0104_at_54d0",
        ("--seed-desc-long-at-pc=0x54D0:0x0E:0x00000104",),
    ),
    Variant(
        "flag0e_at_54d0",
        ("--seed-desc-word-at-pc=0x54D0:0x00:0x410E",),
    ),
    Variant(
        "flag0e_at_5140",
        ("--seed-desc-word-at-pc=0x5140:0x00:0x410E",),
    ),
    Variant(
        "rec0104_at_5140",
        ("--seed-desc-long-at-pc=0x5140:0x0E:0x00000104",),
    ),
    Variant(
        "full_image_at_5140",
        (
            "--seed-desc-word-at-pc=0x5140:0x00:0x410E",
            "--seed-desc-long-at-pc=0x5140:0x0E:0x00000104",
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


def last_desc_line(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        if "JCB name desc $007074:" in line:
            return line.strip()
    return "JCB name desc <missing>"


def run_variant(variant: Variant) -> dict[str, str]:
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
    summary["desc_line"] = last_desc_line(proc.stdout)
    summary["log"] = str(log_path)
    return summary


def fmt_bool(value: str) -> str:
    return "Y" if value == "1" else "."


def format_row(name: str, summary: dict[str, str]) -> str:
    return (
        f"{name:<22} "
        f"lba={summary['last_lba']:<4} "
        f"ini={fmt_bool(summary['reached_amosl_ini'])} "
        f"reason={summary['reason']:<12} "
        f"pc={summary['pc']} "
        f"desc12={summary['desc12']} "
        f"seed54={summary.get('desc_seed_54d0_hits', '0'):<2} "
        f"seedpc={summary.get('desc_seed_pc_hits', '0'):<2} "
        f"a086_d1={summary.get('last_a086_d1', '00000000')} "
        f"a086_d6={summary.get('last_a086_d6', '00000000')}"
    )


def main() -> int:
    print("Running native descriptor-state matrix...")
    results: list[tuple[Variant, dict[str, str]]] = []
    for variant in VARIANTS:
        print(f"  {variant.name}")
        results.append((variant, run_variant(variant)))

    print("\nSummary:")
    for variant, summary in results:
        print(format_row(variant.name, summary))

    print("\nFinal descriptor images:")
    for variant, summary in results:
        print(f"  {variant.name}: {summary['desc_line']}")

    print("\nLogs:")
    for variant, summary in results:
        print(f"  {variant.name}: {summary['log']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
