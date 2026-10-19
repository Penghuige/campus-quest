#!/usr/bin/env python3
"""Coverage-floor direction gate: ratchet floors are one-way.

Compares both coverage-ratchet.json files against the branch's merge
base with origin/<base ref> and fails when any floor value decreases or
a floor key disappears — otherwise every PR could silently lower the
bar with `--update` (docs/quality/quality-gates.md ratchet rules).

An intentional, documented decrease passes by setting
CQ_ALLOW_FLOOR_DECREASE=1; CI wires that to the `[floor-adjust]` marker
in the PR body so the lowering stays a deliberate, reviewer-visible act.
Keys starting with "_" (metadata such as _comment/_recorded) are ignored.

Stdlib only — CI runs this before the Python environment is installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

RATCHET_FILES = (
    "backend/coverage-ratchet.json",
    "frontend/coverage-ratchet.json",
)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def base_commit(base_ref: str) -> str | None:
    remote = f"origin/{base_ref}"
    if git("rev-parse", "--verify", "--quiet", remote, check=False).returncode != 0:
        return None
    result = git("merge-base", "HEAD", remote, check=False)
    return result.stdout.strip() if result.returncode == 0 else remote


def load_at(commit: str, path: str) -> dict | None:
    result = git("show", f"{commit}:{path}", check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def floor_violations(old: object, new: object, trail: str) -> list[str]:
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return [f"{trail}: structure changed"]
        violations = []
        for key, old_value in old.items():
            if key.startswith("_"):
                continue
            location = f"{trail}:{key}"
            if key not in new:
                violations.append(f"{location}: floor key removed (was {old_value!r})")
            else:
                violations.extend(floor_violations(old_value, new[key], location))
        return violations
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        if not isinstance(new, (int, float)) or isinstance(new, bool):
            return [f"{trail}: floor is no longer numeric (was {old}, now {new!r})"]
        if new < old - 1e-9:
            return [f"{trail}: floor lowered {old} -> {new}"]
    return []


def main() -> int:
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    commit = base_commit(base_ref)
    if commit is None:
        if os.environ.get("GITHUB_BASE_REF"):
            sys.exit(f"origin/{base_ref} is unavailable in CI; cannot verify floors")
        print(f"[ratchet-direction] no origin/{base_ref}; skipping (offline clone?)")
        return 0

    violations: list[str] = []
    for path in RATCHET_FILES:
        old = load_at(commit, path)
        if old is None:
            continue
        with open(path, encoding="utf-8") as handle:
            new = json.load(handle)
        violations.extend(floor_violations(old, new, path))

    if not violations:
        print(f"[ratchet-direction] OK: no floor lowered vs {commit[:10]}")
        return 0

    lines = "\n".join(f"  - {violation}" for violation in violations)
    if os.environ.get("CQ_ALLOW_FLOOR_DECREASE") == "1":
        print(f"[ratchet-direction] ALLOWED ([floor-adjust] marker present):\n{lines}")
        return 0
    print(
        f"[ratchet-direction] FAIL: coverage floors only move up.\n{lines}\n"
        "If a decrease is intentional, document the reason in the ratchet "
        "file and mark the PR body with [floor-adjust]."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
