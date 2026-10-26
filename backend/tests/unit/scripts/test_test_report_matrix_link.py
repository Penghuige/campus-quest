# backend/tests/unit/scripts/test_test_report_matrix_link.py
"""Matrix-link detail cards (2026-10-26): the dashboard's detail panel shows
the spec expectation of the matrix row that cites the clicked test.

parse_matrices() reads docs/quality/test-matrix/*.md pipe tables, extracts
row ids + expectation cells + name-level citations (the same citation
grammar check_test_matrix.py enforces), and the renderer attaches
matrix/expectation fields to matching entries. Frontend citations carry a
"frontend/" prefix the generator's suite-relative file paths lack — the
lookup strips it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_report_matrix_link", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load()


_MATRIX_MD = """# Demo 规约-测试对账矩阵

> 覆盖章节：§1

| 规则 | 预期 | 测试位置 | 状态 |
|---|---|---|---|
| D1 | 用户以学号登录后落在任务广场 | `tests/unit/demo.py::test_a` | 已覆盖 |
| D2 | 恶意输入全部有界失败 | `frontend/e2e/demo.spec.ts::inputs bounded` | 已覆盖 |
| D3 | 文件级引用也建立链接 | `tests/unit/demo.py` | 已覆盖 |
"""


def _matrix_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "test-matrix"
    directory.mkdir()
    (directory / "demo.md").write_text(_MATRIX_MD, encoding="utf-8")
    return directory


def test_parse_matrices_extracts_rows_and_citations(module, tmp_path: Path) -> None:
    refs = module.parse_matrices(_matrix_dir(tmp_path))
    named = refs[("tests/unit/demo.py", "test_a")]
    assert named[0].matrix == "demo"
    assert named[0].row_id == "D1"
    assert "任务广场" in named[0].expectation
    frontend = refs[("e2e/demo.spec.ts", "inputs bounded")]
    assert frontend[0].row_id == "D2"
    file_level = refs[("tests/unit/demo.py", None)]
    assert file_level[0].row_id == "D3"


def test_parse_matrices_survives_missing_dir(module, tmp_path: Path) -> None:
    assert module.parse_matrices(tmp_path / "nope") == {}


def _render_with_matrix(module, tmp_path: Path, junit: str, matrix: Path) -> tuple:
    artifacts = tmp_path / "reports" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "backend-unit.xml").write_text(junit, encoding="utf-8")
    output = tmp_path / "reports" / "test-report.html"
    code = module.main(
        [
            "--artifacts-dir",
            str(artifacts),
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--commit",
            "aaaaaaa",
            "--matrix-dir",
            str(matrix),
        ]
    )
    return code, output


_JUNIT = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest">'
    '<testcase classname="tests.unit.demo" name="test_a" time="0.01"/>'
    "</testsuite></testsuites>"
)


def test_entry_gains_matrix_fields_when_matched(module, tmp_path: Path) -> None:
    code, output = _render_with_matrix(module, tmp_path, _JUNIT, _matrix_dir(tmp_path))
    assert code == 0
    html = output.read_text(encoding="utf-8")
    payload = json.loads(html.split('id="report-data">', 1)[1].split("</script>", 1)[0])
    matched = [e for e in payload if e.get("name") == "test_a"]
    assert matched, payload
    entry = matched[0]
    assert entry["matrix"] == "demo · D1"
    assert "任务广场" in entry["expectation"]


def test_expectation_text_is_escaped_in_payload(module, tmp_path: Path) -> None:
    directory = tmp_path / "test-matrix"
    directory.mkdir()
    (directory / "demo.md").write_text(
        _MATRIX_MD.replace("任务广场", "<script>boom</script>任务广场"),
        encoding="utf-8",
    )
    code, output = _render_with_matrix(module, tmp_path, _JUNIT, directory)
    assert code == 0
    assert "<script>boom" not in output.read_text(encoding="utf-8")


def test_detail_panel_renders_expectation_block(module) -> None:
    assert "规约预期" in module._DETAIL
