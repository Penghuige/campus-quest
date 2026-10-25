# backend/tests/unit/scripts/test_test_report.py
"""Contract tests for the offline test-report generator.

The generator lives in ``backend/scripts/test_report.py`` (stdlib only).
These tests pin the adapter mapping (pytest JUnit, Node's junit reporter,
Playwright JSON, exploration manifest), the fixed seven-layer contract,
HTML escaping, and the CLI exit codes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_report.py"
_REPO_ROOT = Path(__file__).resolve().parents[4]

_LAYERS = [
    ("backend-unit", "后端单测 + worker", "backend-unit.xml"),
    ("backend-integration", "后端集成", "backend-integration.xml"),
    ("backend-e2e", "后端全栈 e2e", "backend-e2e.xml"),
    ("frontend-unit", "前端单测", "frontend-unit.xml"),
    ("playwright", "Playwright 电池", "playwright.json"),
    ("pixel", "像素回归", "playwright.json"),
    ("exploration", "探索层", "exploration.json"),
]


def _load_report_module() -> Any:
    spec = importlib.util.spec_from_file_location("test_report_under_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def report_module() -> Any:
    return _load_report_module()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _junit(*testcases: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite name="pytest">'
        + "".join(testcases)
        + "</testsuite></testsuites>"
    )


def _playwright_spec(
    title: str,
    file: str,
    *,
    status: str,
    project: str = "chromium",
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "file": file,
        "tests": [
            {
                "projectName": project,
                "status": status,
                "results": results
                if results is not None
                else [{"status": "passed", "duration": 12}],
            }
        ],
    }


def _report_with(
    module: Any,
    layer_cases: dict[str, list[Any]],
    *,
    notes: list[str] | None = None,
) -> Any:
    layers = [
        module.LayerReport(
            key=key,
            title=title,
            artifact=artifact,
            cases=layer_cases.get(key, []),
        )
        for key, title, artifact in _LAYERS
    ]
    return module.Report(
        branch="test-dashboard",
        command="make test-report",
        generated_at="2026-10-25 10:42:00",
        layers=layers,
        notes=notes or [],
        artifacts_dir=Path("reports/artifacts"),
    )


# ---------------------------------------------------------------------------
# JUnit adapter
# ---------------------------------------------------------------------------


def test_junit_maps_pytest_statuses_durations_and_locators(
    report_module: Any, tmp_path: Path
) -> None:
    path = _write(
        tmp_path / "backend-unit.xml",
        _junit(
            '<testcase classname="tests.unit.test_mod" name="test_ok" time="0.25" />',
            '<testcase classname="tests.unit.test_mod" name="test_bad" time="1.5">'
            '<failure message="boom">Traceback line</failure></testcase>',
            '<testcase classname="tests.unit.test_mod.TestThing" '
            'name="test_class_case" time="0.01" />',
            '<testcase classname="tests.unit.test_mod" name="test_skip" time="0">'
            '<skipped message="needs PG" /></testcase>',
        ),
    )

    cases = report_module.parse_junit(path)

    assert [case.status for case in cases] == [
        "passed",
        "failed",
        "passed",
        "skipped",
    ]
    assert cases[0].locator == "tests/unit/test_mod.py::test_ok"
    assert cases[0].duration_ms == 250.0
    assert cases[1].duration_ms == 1500.0
    assert "boom" in cases[1].message
    assert "Traceback line" in cases[1].message
    assert cases[2].locator == ("tests/unit/test_mod.py::TestThing::test_class_case")
    assert cases[3].message == "needs PG"


def test_node_junit_locator_is_classname_based(
    report_module: Any, tmp_path: Path
) -> None:
    """Node's junit reporter emits classname="test" and no file path."""
    path = _write(
        tmp_path / "frontend-unit.xml",
        "<testsuites>"
        '<testcase name="passes plainly" time="0.001" classname="test" />'
        '<testcase name="fails loudly" time="0.002" classname="test">'
        '<failure message="1 !== 2">stack</failure></testcase>'
        "</testsuites>",
    )

    cases = report_module.parse_junit(path, locator_style="node")

    assert [case.status for case in cases] == ["passed", "failed"]
    assert cases[0].locator == "test::passes plainly"
    assert cases[1].message.startswith("1 !== 2")


def test_junit_error_element_is_a_failure(report_module: Any, tmp_path: Path) -> None:
    path = _write(
        tmp_path / "backend-e2e.xml",
        _junit(
            '<testcase classname="tests.e2e.test_x" name="test_broken">'
            '<error message="setup crashed">stack</error></testcase>',
        ),
    )

    cases = report_module.parse_junit(path)

    assert cases[0].status == "failed"
    assert "setup crashed" in cases[0].message


def test_malformed_junit_raises_report_error(
    report_module: Any, tmp_path: Path
) -> None:
    path = _write(tmp_path / "broken.xml", "<testsuites><testcase")

    with pytest.raises(report_module.ReportError):
        report_module.parse_junit(path)


# ---------------------------------------------------------------------------
# Playwright adapter
# ---------------------------------------------------------------------------


def test_playwright_nested_suites_projects_and_statuses(
    report_module: Any, tmp_path: Path
) -> None:
    auth_file = "e2e/auth.spec.ts"
    payload = {
        "suites": [
            {
                "title": auth_file,
                "file": auth_file,
                "specs": [
                    _playwright_spec("an admin logs in", auth_file, status="expected"),
                    _playwright_spec(
                        "the reset loop",
                        auth_file,
                        status="unexpected",
                        results=[
                            {
                                "status": "failed",
                                "duration": 80,
                                "errors": [
                                    {"message": "Expected 200, got 500"},
                                ],
                            }
                        ],
                    ),
                ],
                "suites": [
                    {
                        "title": "nested describe",
                        "specs": [
                            _playwright_spec(
                                "flakes once",
                                auth_file,
                                status="flaky",
                                project="firefox",
                                results=[
                                    {
                                        "status": "failed",
                                        "duration": 10,
                                        "errors": [{"message": "first attempt"}],
                                    },
                                    {"status": "passed", "duration": 20},
                                ],
                            ),
                            _playwright_spec(
                                "skipped by guard",
                                auth_file,
                                status="skipped",
                                project="webkit",
                                results=[{"status": "skipped", "duration": 0}],
                            ),
                        ],
                    }
                ],
            },
            {
                "title": "e2e/visual-regression.spec.ts",
                "file": "e2e/visual-regression.spec.ts",
                "specs": [
                    _playwright_spec(
                        "student-dashboard",
                        "e2e/visual-regression.spec.ts",
                        status="expected",
                        results=[{"status": "passed", "duration": 500}],
                    )
                ],
            },
        ]
    }
    path = _write(tmp_path / "playwright.json", json.dumps(payload))

    cases = report_module.parse_playwright(path)

    assert [case.status for case in cases] == [
        "passed",
        "failed",
        "failed",
        "skipped",
        "passed",
    ]
    assert cases[0].name == "an admin logs in [chromium]"
    assert cases[1].name == "the reset loop [chromium]"
    assert "Expected 200, got 500" in cases[1].message
    assert cases[1].duration_ms == 80.0
    assert cases[2].name == "nested describe › flakes once [firefox]"
    assert cases[2].locator == ("e2e/auth.spec.ts::nested describe › flakes once")
    assert "flaky" in cases[2].message
    assert "first attempt" in cases[2].message
    assert cases[2].duration_ms == 30.0
    assert cases[3].status == "skipped"
    assert cases[4].duration_ms == 500.0


def test_playwright_global_errors_become_a_failed_case(
    report_module: Any, tmp_path: Path
) -> None:
    path = _write(
        tmp_path / "playwright.json",
        json.dumps(
            {
                "suites": [],
                "errors": [{"message": "webServer failed to start"}],
            }
        ),
    )

    cases = report_module.parse_playwright(path)

    assert len(cases) == 1
    assert cases[0].status == "failed"
    assert cases[0].name == "Playwright 全局错误"
    assert "webServer failed to start" in cases[0].message


def test_playwright_unknown_status_fails_closed(
    report_module: Any, tmp_path: Path
) -> None:
    payload = {
        "suites": [
            {
                "title": "e2e/auth.spec.ts",
                "file": "e2e/auth.spec.ts",
                "specs": [
                    {
                        "title": "weird",
                        "file": "e2e/auth.spec.ts",
                        "tests": [{"status": "mystery", "results": []}],
                    }
                ],
            }
        ]
    }
    path = _write(tmp_path / "playwright.json", json.dumps(payload))

    with pytest.raises(report_module.ReportError):
        report_module.parse_playwright(path)


def test_visual_specs_route_to_the_pixel_layer(
    report_module: Any, tmp_path: Path
) -> None:
    payload = {
        "suites": [
            {
                "title": "e2e/auth.spec.ts",
                "file": "e2e/auth.spec.ts",
                "specs": [
                    _playwright_spec("auth flow", "e2e/auth.spec.ts", status="expected")
                ],
            },
            {
                "title": "e2e/visual-regression.spec.ts",
                "file": "e2e/visual-regression.spec.ts",
                "specs": [
                    _playwright_spec(
                        "student-dashboard",
                        "e2e/visual-regression.spec.ts",
                        status="expected",
                    )
                ],
            },
        ]
    }
    _write(tmp_path / "playwright.json", json.dumps(payload))

    report = report_module.build_report(
        tmp_path,
        branch="test-dashboard",
        command="make test-report",
        generated_at="2026-10-25 10:42:00",
    )
    layers = {layer.key: layer for layer in report.layers}

    assert [case.name for case in layers["playwright"].cases] == [
        "auth flow [chromium]"
    ]
    assert [case.name for case in layers["pixel"].cases] == [
        "student-dashboard [chromium]"
    ]
    assert layers["playwright"].missing is False
    assert layers["pixel"].missing is False


# ---------------------------------------------------------------------------
# Exploration manifest
# ---------------------------------------------------------------------------


def test_exploration_manifest_maps_all_four_states(
    report_module: Any, tmp_path: Path
) -> None:
    path = _write(
        tmp_path / "exploration.json",
        json.dumps(
            {
                "probes": [
                    {"name": "double click", "status": "passed", "duration_ms": 12},
                    {
                        "name": "expired link",
                        "status": "failed",
                        "file": "exploration/auth.py",
                        "case": "expired_link",
                        "message": "returned 200",
                    },
                    {"name": "planned probe", "status": "not_run"},
                    {"name": "needs fixture", "status": "skipped"},
                ]
            }
        ),
    )

    cases = report_module.parse_exploration(path)

    assert [case.status for case in cases] == [
        "passed",
        "failed",
        "not_run",
        "skipped",
    ]
    assert cases[1].locator == "exploration/auth.py::expired_link"
    assert cases[1].message == "returned 200"


def test_exploration_unknown_status_fails_closed(
    report_module: Any, tmp_path: Path
) -> None:
    path = _write(
        tmp_path / "exploration.json",
        json.dumps({"probes": [{"name": "x", "status": "maybe"}]}),
    )

    with pytest.raises(report_module.ReportError):
        report_module.parse_exploration(path)


def test_committed_example_manifest_is_valid(report_module: Any) -> None:
    example = (
        _REPO_ROOT / "docs" / "operations" / "test-report-exploration.example.json"
    )

    cases = report_module.parse_exploration(example)

    assert cases
    assert {"passed", "failed", "skipped", "not_run"} >= {case.status for case in cases}


# ---------------------------------------------------------------------------
# Layer assembly
# ---------------------------------------------------------------------------


def test_missing_artifacts_become_seven_not_run_placeholders(
    report_module: Any, tmp_path: Path
) -> None:
    report = report_module.build_report(
        tmp_path / "artifacts",
        branch="test-dashboard",
        command="make test-report",
        generated_at="2026-10-25 10:42:00",
    )

    assert [layer.title for layer in report.layers] == [
        title for _, title, _ in _LAYERS
    ]
    assert all(layer.missing for layer in report.layers)
    placeholders = [
        case for layer in report.layers for case in report_module.display_cases(layer)
    ]
    assert len(placeholders) == 7
    assert {case.status for case in placeholders} == {"not_run"}
    assert report_module.total_counts(report)["not_run"] == 7


def test_build_report_reads_pytest_and_exploration_artifacts(
    report_module: Any, tmp_path: Path
) -> None:
    _write(
        tmp_path / "backend-unit.xml",
        _junit(
            '<testcase classname="tests.unit.test_a" name="test_ok" time="0.1" />',
            '<testcase classname="tests.unit.test_a" name="test_bad" time="0.2">'
            '<failure message="nope" /></testcase>',
        ),
    )
    _write(
        tmp_path / "exploration.json",
        json.dumps({"probes": [{"name": "probe", "status": "passed"}]}),
    )

    report = report_module.build_report(
        tmp_path,
        branch="test-dashboard",
        command="make test-report",
        generated_at="2026-10-25 10:42:00",
    )
    layers = {layer.key: layer for layer in report.layers}
    totals = report_module.total_counts(report)

    assert [case.status for case in layers["backend-unit"].cases] == [
        "passed",
        "failed",
    ]
    assert layers["exploration"].cases[0].name == "probe"
    assert totals == {"passed": 2, "failed": 1, "skipped": 0, "not_run": 5}


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def test_html_escapes_every_data_derived_text(
    report_module: Any, tmp_path: Path
) -> None:
    evil_name = '<script>alert("x")</script>'
    evil_message = '<img src=x onerror="alert(1)">'
    case = report_module.TestCase(
        name=evil_name,
        file="tests/evil.py",
        case="test_evil",
        status="failed",
        duration_ms=1.0,
        message=evil_message,
    )
    report = _report_with(report_module, {"playwright": [case]}, notes=["<b>note</b>"])

    document = report_module.render_html(report)

    assert evil_name not in document
    assert evil_message not in document
    assert "<b>note</b>" not in document
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in document
    assert "\\u003cscript\\u003e" in document
    assert "&lt;b&gt;note&lt;/b&gt;" in document


def test_html_contains_seven_layers_legend_and_totals(
    report_module: Any, tmp_path: Path
) -> None:
    cases = {
        "backend-unit": [
            report_module.TestCase(
                name="passes", file="a.py", case="test_passes", status="passed"
            )
        ],
        "backend-integration": [
            report_module.TestCase(
                name="fails", file="b.py", case="test_fails", status="failed"
            )
        ],
        "backend-e2e": [
            report_module.TestCase(
                name="skips", file="c.py", case="test_skips", status="skipped"
            )
        ],
        "frontend-unit": [
            report_module.TestCase(
                name="not run probe", file="", case="probe", status="not_run"
            )
        ],
    }
    report = _report_with(report_module, cases)

    document = report_module.render_html(report)

    for _, title, _ in _LAYERS:
        assert title in document
    assert "总计" in document
    assert "1 绿" in document
    assert "1 红" in document
    assert "1 灰" in document
    assert "1 未运行" in document
    assert "未运行（没有结果文件）" in document
    assert 'class="square status-passed"' in document
    assert 'class="square status-failed"' in document
    assert 'class="square status-skipped"' in document
    assert 'class="square status-not_run"' in document
    assert "1 个 · 全绿" in document


def test_html_is_offline_and_mobile_safe(report_module: Any) -> None:
    report = _report_with(report_module, {})

    document = report_module.render_html(report)

    assert 'name="viewport"' in document
    assert "<link" not in document
    assert "<img" not in document
    assert "http://" not in document
    assert "https://" not in document
    assert "@media" in document
    assert "flex-wrap: wrap" in document
    assert "overflow-wrap: anywhere" in document


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_html_and_returns_zero(
    report_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "artifacts"
    _write(
        artifacts / "backend-unit.xml",
        _junit('<testcase classname="tests.unit.test_a" name="test_ok" time="0.1" />'),
    )
    output = tmp_path / "out" / "test-report.html"

    code = report_module.main(
        [
            "--artifacts-dir",
            str(artifacts),
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--branch",
            "test-dashboard",
            "--command",
            "make test-report",
            "--generated-at",
            "2026-10-25 10:42:00",
            "--note",
            "frontend dependencies are absent",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert output.is_file()
    document = output.read_text(encoding="utf-8")
    assert "后端单测 + worker" in document
    assert "frontend dependencies are absent" in document
    assert "wrote" in captured.out
    assert "1 passed" in captured.out


def test_cli_reports_malformed_playwright_json(
    report_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "artifacts"
    _write(artifacts / "playwright.json", "{not json")
    output = tmp_path / "test-report.html"

    code = report_module.main(
        [
            "--artifacts-dir",
            str(artifacts),
            "--output",
            str(output),
            "--repo-root",
            str(tmp_path),
            "--branch",
            "test-dashboard",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert not output.exists()
    assert "Playwright" in captured.err


def test_cli_reports_unknown_manifest_status(
    report_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "artifacts"
    _write(
        artifacts / "exploration.json",
        json.dumps({"probes": [{"name": "x", "status": "maybe"}]}),
    )

    code = report_module.main(
        [
            "--artifacts-dir",
            str(artifacts),
            "--output",
            str(tmp_path / "test-report.html"),
            "--repo-root",
            str(tmp_path),
            "--branch",
            "test-dashboard",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "invalid status" in captured.err
