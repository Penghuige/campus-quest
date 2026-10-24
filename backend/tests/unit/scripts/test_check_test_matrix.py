# backend/tests/unit/scripts/test_check_test_matrix.py
"""Name-level citation validation for the spec-test matrix checker.

Campaign 2026-10-25, task 1: a covered row's `path::test_name` citation
must point at a real test. File existence alone let renamed/deleted
tests keep their matrix rows green (the AP10 incident class). Frontend
suites cite Playwright titles, which contain spaces — the citation
regex must recognize `path::free text title` for those, and the name
check becomes a substring probe. Backend suites cite pytest function
names, validated as `(async) def name(` in the cited file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_test_matrix.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_test_matrix_under_test", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_MATRIX_TEMPLATE = """# Example 规约-测试对账矩阵

> 建立日期：2026-10-25 · 域：example（campaign task 1 test fixture）
> 覆盖章节：§5

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 1 | D1 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |

## 明细

| 规则 | 预期 | 测试位置 | 状态 |
|---|---|---|---|
| D1 | the example behavior holds | `{citation}` | 已覆盖 |
"""


@pytest.fixture
def checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_checker()
    backend_root = tmp_path / "backend"
    (backend_root / "tests" / "integration" / "example").mkdir(parents=True)
    (backend_root / "tests" / "integration" / "example" / "test_example.py").write_text(
        "def test_exists():\n"
        "    pass\n"
        "\n"
        "\n"
        "async def test_async_exists():\n"
        "    pass\n",
        encoding="utf-8",
    )
    e2e = tmp_path / "frontend" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "example.spec.ts").write_text(
        'test("an admin login lands on the admin workspace", async () => {});\n',
        encoding="utf-8",
    )
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-09-19-campusquest-design.md").write_text(
        "# Spec\n\n## 5. Example Domain\n\n## 9. Deadline\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_BACKEND_ROOT", backend_root)
    monkeypatch.setattr(module, "_REPO_ROOT", tmp_path)
    return module


def _matrix_file(tmp_path: Path, citation: str) -> Path:
    path = tmp_path / "matrix.md"
    path.write_text(_MATRIX_TEMPLATE.format(citation=citation), encoding="utf-8")
    return path


def test_backend_citation_with_existing_def_passes(checker, tmp_path):
    violations = checker.check_matrix(
        _matrix_file(tmp_path, "tests/integration/example/test_example.py::test_exists")
    )
    assert violations == []


def test_async_def_counts_as_a_cited_test(checker, tmp_path):
    violations = checker.check_matrix(
        _matrix_file(
            tmp_path, "tests/integration/example/test_example.py::test_async_exists"
        )
    )
    assert violations == []


def test_backend_citation_with_missing_def_is_a_violation(checker, tmp_path):
    violations = checker.check_matrix(
        _matrix_file(
            tmp_path, "tests/integration/example/test_example.py::test_renamed_away"
        )
    )
    assert len(violations) == 1
    assert "missing test" in violations[0]


def test_bare_backend_path_citation_needs_only_the_file(checker, tmp_path):
    violations = checker.check_matrix(
        _matrix_file(tmp_path, "tests/integration/example/test_example.py")
    )
    assert violations == []


def test_frontend_spaced_title_citation_is_recognized_and_validated(checker, tmp_path):
    violations = checker.check_matrix(
        _matrix_file(
            tmp_path,
            "frontend/e2e/example.spec.ts::an admin login lands on the admin workspace",
        )
    )
    assert violations == []


def test_frontend_citation_with_absent_title_is_a_violation(checker, tmp_path):
    violations = checker.check_matrix(
        _matrix_file(
            tmp_path, "frontend/e2e/example.spec.ts::no such title in this spec"
        )
    )
    assert len(violations) == 1
    assert "missing test" in violations[0]


def test_matrix_without_coverage_section_is_flagged(checker, tmp_path):
    path = tmp_path / "matrix.md"
    text = _MATRIX_TEMPLATE.format(
        citation="tests/integration/example/test_example.py::test_exists"
    ).replace("> 覆盖章节：§5\n", "")
    path.write_text(text, encoding="utf-8")
    violations = checker.check_matrix(path)
    assert any("覆盖章节" in v for v in violations)


def test_claimed_section_must_exist_in_the_spec(checker, tmp_path):
    path = tmp_path / "matrix.md"
    text = _MATRIX_TEMPLATE.format(
        citation="tests/integration/example/test_example.py::test_exists"
    ).replace("> 覆盖章节：§5", "> 覆盖章节：§99")
    path.write_text(text, encoding="utf-8")
    violations = checker.check_matrix(path)
    assert any("§99" in v for v in violations)


def test_explicit_spec_path_groups_validate_against_that_spec(checker, tmp_path):
    other = tmp_path / "docs" / "superpowers" / "specs" / "other-design.md"
    other.write_text("# Other\n\n## 3. Other Domain\n", encoding="utf-8")
    path = tmp_path / "matrix.md"
    text = _MATRIX_TEMPLATE.format(
        citation="tests/integration/example/test_example.py::test_exists"
    ).replace(
        "> 覆盖章节：§5",
        "> 覆盖章节：§9 | docs/superpowers/specs/other-design.md#§3",
    )
    path.write_text(text, encoding="utf-8")
    assert checker.check_matrix(path) == []
    text_bad = text.replace("other-design.md#§3", "other-design.md#§7")
    path.write_text(text_bad, encoding="utf-8")
    violations = checker.check_matrix(path)
    assert any("§7" in v for v in violations)


def test_referenced_spec_file_must_exist(checker, tmp_path):
    path = tmp_path / "matrix.md"
    text = _MATRIX_TEMPLATE.format(
        citation="tests/integration/example/test_example.py::test_exists"
    ).replace(
        "> 覆盖章节：§5",
        "> 覆盖章节：docs/superpowers/specs/missing-design.md#§3",
    )
    path.write_text(text, encoding="utf-8")
    violations = checker.check_matrix(path)
    assert any("missing-design.md" in v for v in violations)
