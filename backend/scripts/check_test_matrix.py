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
   path) whose FILE part exists on disk — backend suites (``tests/``)
   resolve against the backend root, frontend suites
   (``frontend/e2e/`` or ``frontend/src/``) against the repository
   root — and, when a ``::name`` part is present, that the name is
   really defined in the file: backend citations as ``(async) def
   name(``, frontend citations (Playwright titles may contain spaces)
   as a substring of the file text;
3. gap rows reference a gap id (G-\\d+) that the file's gap section
   also lists;
4. mismatch rows appear in the file's mismatch section;
5. the header carries a 覆盖章节 line pinning the spec sections the
   matrix reconciles — format ``> 覆盖章节：§5 · §9 | path/to/spec.md#§3``
   (groups separated by ``|``; a group without a path defaults to the
   main design spec) — and every claimed section must exist as a
   ``## N.`` heading in the referenced spec.

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
_REPO_ROOT = _BACKEND_ROOT.parent

_COVERED = ("已覆盖", "covered")
_GAP = ("缺口", "gap")
_MISMATCH = ("不匹配", "mismatch")
_PROCESS = ("流程行", "process")
# PR-3 (2026-10-19): a retired row's route family left the tree — its
# citation names dead files BY DESIGN; the row points at the behavior's
# new door instead, so it is citation-exempt like a process row.
_RETIRED = ("已退役", "retired")

# A covered row's citation: a backend suite path (tests/...) or a
# frontend suite path (frontend/e2e/... or frontend/src/...), each with
# an optional ::name suffix. The two roots differ (backend root vs
# repository root), so _cite_file resolves the prefix. The name may
# contain spaces (frontend Playwright titles), so it captures up to the
# closing backtick rather than a word-char run.
_TEST_CITE = re.compile(r"`(tests/[^`:]+|frontend/(?:e2e|src)/[^`:]+)(?:::([^`]+))?`")
_GAP_ID = re.compile(r"G-\d+")

# The spec-drift linkage: every matrix declares which spec sections it
# reconciles. A group is "§5 · §9" (main design spec) or
# "docs/.../some-design.md#§3 · §4" (explicit spec file).
_SECTION_LINE = re.compile(r"^>\s*覆盖章节：\s*(.+)$", re.MULTILINE)
_SECTION_REF = re.compile(r"§(\d+)")

_MAIN_SPEC_REL = "docs/superpowers/specs/2026-09-19-campusquest-design.md"


def _cite_file(cited: str) -> Path:
    """Resolve one cited suite path to its on-disk file: backend suites
    hang off the backend root, frontend suites off the repository root."""
    if cited.startswith("frontend/"):
        return _REPO_ROOT / cited
    return _BACKEND_ROOT / cited


def _cite_name_ok(cited_file: Path, name: str) -> bool:
    """A cited test name must be defined in the cited file: backend
    suites define tests as ``(async) def name(`` (a dotted citation
    pins its final segment); frontend suites embed the cited title as
    a substring of the spec text."""
    text = cited_file.read_text(encoding="utf-8", errors="replace")
    if cited_file.suffix == ".py":
        return f"def {name.split('.')[-1]}(" in text
    return name in text


def _status_of(cells: list[str]) -> str | None:
    status = cells[-1].strip()
    for marker in _COVERED + _GAP + _MISMATCH + _PROCESS + _RETIRED:
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
    mismatch_section = text.split("不匹配明细", 1)[-1] if "不匹配明细" in text else ""

    section_match = _SECTION_LINE.search(text)
    if section_match is None:
        violations.append(
            f"{path.name}: header declares no 覆盖章节 line (spec drift linkage)"
        )
    else:
        for group in section_match.group(1).split("|"):
            group = group.strip()
            if "#" in group:
                spec_rel, _, claimed = group.partition("#")
                spec_rel = spec_rel.strip()
            else:
                spec_rel = _MAIN_SPEC_REL
                claimed = group
            sections = _SECTION_REF.findall(claimed)
            if not sections:
                continue
            spec_path = _REPO_ROOT / spec_rel
            if not spec_path.exists():
                violations.append(
                    f"{path.name}: 覆盖章节 references missing spec {spec_rel}"
                )
                continue
            spec_text = spec_path.read_text(encoding="utf-8")
            for number in sections:
                if f"\n## {number}." not in spec_text and not spec_text.startswith(
                    f"## {number}."
                ):
                    violations.append(
                        f"{path.name}: 覆盖章节 claims {spec_rel.split('/')[-1]}"
                        f" §{number}, which has no '## {number}.' heading"
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
            for cited_file, cited_name in citations:
                file_path = _cite_file(cited_file)
                if not file_path.exists():
                    violations.append(
                        f"{path.name}: row {row} cites missing file {cited_file}"
                    )
                elif cited_name and not _cite_name_ok(file_path, cited_name):
                    violations.append(
                        f"{path.name}: row {row} cites missing test "
                        f"{cited_name.strip()} in {cited_file}"
                    )
        elif status == "gap":
            ids = _GAP_ID.findall(" ".join(cells))
            if not ids:
                violations.append(f"{path.name}: gap row {row} carries no gap id (G-N)")
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
