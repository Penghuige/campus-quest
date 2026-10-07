# backend/scripts/coverage_ratchet.py
"""Compare current per-module branch coverage against the ratchet file.

The ratchet (owner-approved 2026-10-07): ``coverage-ratchet.json`` pins
one branch-coverage percentage per top-level module of ``app/``. The
gate FAILS when any module drops below its pinned value — improvements
never auto-lower anything, and a raise requires running with
``--update`` (a deliberate, reviewable commit of the new floor).

Inputs: a COMBINED ``.coverage`` data file produced by the three suites
(unit + integration + workers) each run with ``--cov``; the script
reads ``coverage json`` output, never re-runs tests.

Usage:
    uv run python scripts/coverage_ratchet.py            # compare (gate)
    uv run python scripts/coverage_ratchet.py --update   # rewrite floors

Exit codes: 0 pass / 1 regression or untracked module / 2 usage error.
A module missing from the ratchet is a failure too — new code must
enter the ratchet explicitly via --update in the same PR that adds it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_RATCHET_PATH = _BACKEND_ROOT / "coverage-ratchet.json"

#: Cross-environment measurement noise (reviewer ruling 2026-10-07): the
#: same test set measured on the local stack vs the CI runner drifted
#: 0.1pp on one module (identity 90.8 local vs 90.7 CI — a single
#: environment-conditional branch). A zero-epsilon ratchet would flap on
#: that noise forever; 0.15 covers the observed drift with margin while
#: staying far below any real regression scale. Floors still mean what
#: they say: the CI-observed value, not floor+epsilon.
_EPSILON = 0.15


def _coverage_json() -> dict:
    result = subprocess.run(
        ["uv", "run", "coverage", "json", "-o", "-", "--quiet"],
        cwd=_BACKEND_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _module_of(file_path: str) -> str | None:
    parts = file_path.split("/")
    if not parts or parts[0] != "app":
        return None
    # Top-level package under app/ (modules/<name>, core, integrations,
    # workers, db, ...) — the granularity the ratchet pins.
    if parts[1] == "modules" and len(parts) > 2:
        return f"app/modules/{parts[2]}"
    return f"app/{parts[1]}"


def _current_by_module(report: dict) -> dict[str, dict]:
    per_module: dict[str, dict] = {}
    for file_path, file_stats in report["files"].items():
        module = _module_of(file_path)
        if module is None:
            continue
        bucket = per_module.setdefault(
            module,
            {
                "covered": 0,
                "missed": 0,
                "covered_branches": 0,
                "partial_branches": 0,
            },
        )
        summary = file_stats["summary"]
        bucket["covered"] += summary["covered_lines"]
        bucket["missed"] += summary["missing_lines"]
        bucket["covered_branches"] += summary.get("covered_branches", 0)
        bucket["partial_branches"] += summary.get("num_partial_branches", 0)
    return per_module


def _percent(stats: dict) -> float:
    statements = stats["covered"] + stats["missed"]
    branches = stats["covered_branches"] + stats["partial_branches"]
    total = statements + branches
    if total == 0:
        return 100.0
    hit = stats["covered"] + stats["covered_branches"]
    return round(100.0 * hit / total, 1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update", action="store_true", help="rewrite the ratchet to current levels"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the per-module table as JSON (for PR descriptions)",
    )
    args = parser.parse_args()

    report = _coverage_json()
    current = {
        module: _percent(stats) for module, stats in _current_by_module(report).items()
    }

    if args.update:
        _RATCHET_PATH.write_text(
            json.dumps(
                {module: current[module] for module in sorted(current)},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"ratchet updated: {len(current)} modules -> {_RATCHET_PATH.name}")
        return 0

    ratchet: dict[str, float] = {}
    if _RATCHET_PATH.exists():
        ratchet = json.loads(_RATCHET_PATH.read_text(encoding="utf-8"))

    violations: list[str] = []
    if args.json:
        print(json.dumps(current, indent=2, ensure_ascii=False))
    print(f"{'module':32} {'ratchet':>8} {'now':>8}")
    for module in sorted(set(ratchet) | set(current)):
        floor = ratchet.get(module)
        now = current.get(module)
        if floor is None:
            violations.append(f"{module}: not in the ratchet (run --update)")
            floor_display = "--"
        else:
            floor_display = f"{floor:.1f}"
        now_display = f"{now:.1f}" if now is not None else "--"
        marker = ""
        if floor is not None and now is not None and now < floor - _EPSILON:
            violations.append(
                f"{module}: {now:.1f} < ratchet {floor:.1f} "
                f"(more than the {_EPSILON}pp cross-environment noise "
                "allowance) — restore coverage or raise the floor via "
                "--update"
            )
            marker = "  <- REGRESSION"
        print(f"{module:32} {floor_display:>8} {now_display:>8}{marker}")

    if violations:
        print(f"\n{len(violations)} violation(s):")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("\nratchet holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
