# backend/scripts/check_test_matrix.py
"""Validate a spec-test reconciliation matrix (docs/quality/test-matrix/).

The matrix discipline (owner-approved 2026-10-07): every row whose
status is covered must cite at least one real test location; every gap
row must be registered in the matrix's gap section; every mismatch row
must appear in the mismatch section. Rows declared as process rows are
exempt from the test-link requirement by definition.

Checks, per matrix file:
1. every table row carries a status from the closed vocabulary
   (covered / gap / mismatch / process, Chinese markers included);
2. covered rows cite at least one ``path::name`` (or a bare tests/
   path) whose FILE part exists on disk relative to the backend root;
3. gap rows reference a gap id (G-\\d+) that the file's gap section
   also lists;
4. mismatch rows appear in the file's mismatch section.

Exit code: 0 = the matrix holds together; 1 = any violation (one line
per violation on stdout). Deliberately tiny: no markdown dependency —
the matrix tables are pipe-delimited rows by construction.

Usage: uv run python scripts/check_test_matrix.py [matrix.md ...]
(defaults to every docs/quality/test-matrix/*.md)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

_COVERED = ("已覆盖", "covered")
_GAP = ("缺口", "gap")
_MISMATCH = ("不匹配", "mismatch")
_PROCESS = ("流程行", "process")

_TEST_CITE = re.compile(r"`(tests/[^`:]+)(?:::[\w.]+)?`")
_GAP_ID = re.compile(r"G-\d+")


def _status_of(cells: list[str]) -> str | None:
    status = cells[-1].strip()
    for marker in _COVERED + _GAP + _MISMATCH + _PROCESS:
        if marker in status:
            return (
                "covered"
                if marker in _COVERED
                else "gap"
                if marker in _GAP
                else "mismatch"
                if marker in _MISMATCH
                else "process"
            )
    return None


def check_matrix(path: Path) -> list[str]:
    violations: list[str] = []
    text = path.read_text(encoding="utf-8")

    gap_section = text.split("缺口明细", 1)[-1] if "缺口明细" in text else ""
    mismatch_section = (
        text.split("不匹配明细", 1)[-1] if "不匹配明细" in text else ""
    )

    row_id = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in {"#", "规则引用", "状态", "数量", "明细"}:
            continue
        if not re.match(r"^[A-Z]+-?\d+$|^[EG]\d+$", cells[0]):
            continue  # header separators / summary tables
        row_id += 1
        row = cells[0]
        status = _status_of(cells)
        if status is None:
            violations.append(f"{path.name}: row {row} has no recognizable status")
            continue
        if status == "process":
            continue
        citations = _TEST_CITE.findall(" ".join(cells))
        if status == "covered":
            if not citations:
                violations.append(
                    f"{path.name}: row {row} is covered but cites no test location"
                )
                continue
            for cited_file in citations:
                if not (_BACKEND_ROOT / cited_file).exists():
                    violations.append(
                        f"{path.name}: row {row} cites missing file {cited_file}"
                    )
        elif status == "gap":
            ids = _GAP_ID.findall(" ".join(cells))
            if not ids:
                violations.append(
                    f"{path.name}: gap row {row} carries no gap id (G-N)"
                )
            for gap_id in ids:
                if gap_id not in gap_section:
                    violations.append(
                        f"{path.name}: gap {gap_id} (row {row}) is not "
                        "registered in the gap section"
                    )
        elif status == "mismatch":
            if row not in mismatch_section:
                violations.append(
                    f"{path.name}: mismatch row {row} is not detailed in the "
                    "mismatch section"
                )
    if row_id == 0:
        violations.append(f"{path.name}: no matrix rows recognized")
    return violations


def main(argv: list[str]) -> int:
    if argv[1:]:
        paths = [Path(arg) for arg in argv[1:]]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            print(f"matrix file(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 2
    else:
        paths = sorted(
            (_BACKEND_ROOT.parent / "docs" / "quality" / "test-matrix").glob("*.md")
        )
    if not paths:
        print("no matrix files found", file=sys.stderr)
        return 2
    violations: list[str] = []
    for path in paths:
        violations.extend(check_matrix(path))
    for violation in violations:
        print(violation)
    print(
        f"checked {len(paths)} matrix file(s): "
        + ("OK" if not violations else f"{len(violations)} violation(s)")
    )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
