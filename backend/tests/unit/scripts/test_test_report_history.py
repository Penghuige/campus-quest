# backend/tests/unit/scripts/test_test_report_history.py
"""Header CI strip + per-commit history (dashboard 2026-10-26).

`make test-report` now records the commit and its GitHub Actions verdict,
archives one report per commit under reports/history/<short-sha>/, and
writes a landing page + history index that the static viewer serves.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_report.py"
_SHA = "4059fbd7b915011db80bb2b29f4671e03538e0ad"


def _load():
    spec = importlib.util.spec_from_file_location("test_report_hist", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load()


def _ci_payload() -> dict:
    return {
        "commit": _SHA,
        "status": "completed",
        "conclusion": "success",
        "run_id": 40199928552,
        "run_url": "https://github.com/Penghuige/campus-quest/actions/runs/40199928552",
        "fetched_at": "2026-10-26T01:00:00+08:00",
        "jobs": [
            {
                "name": "Backend (Python 3.12)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Frontend (Node 22)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Pixel visual regression (Playwright)",
                "status": "completed",
                "conclusion": "success",
            },
        ],
        "error": None,
    }


def _run(
    module,
    tmp_path: Path,
    *,
    with_ci: bool = True,
    keep: int = 20,
    ci_override: dict | None = None,
):
    artifacts = tmp_path / "reports" / "artifacts"
    artifacts.mkdir(parents=True)
    ci = tmp_path / "reports" / "artifacts" / "ci.json"
    if with_ci:
        payload = ci_override if ci_override is not None else _ci_payload()
        ci.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "reports" / "test-report.html"
    argv = [
        "--artifacts-dir",
        str(artifacts),
        "--output",
        str(output),
        "--repo-root",
        str(tmp_path),
        "--commit",
        _SHA,
        "--generated-at",
        "2026-10-26T01:00:00+08:00",
        "--history-dir",
        str(tmp_path / "reports" / "history"),
        "--keep-history",
        str(keep),
    ]
    if with_ci:
        argv += ["--ci-file", str(ci)]
    code = module.main(argv)
    return code, output


def test_header_renders_commit_and_ci_jobs(module, tmp_path: Path) -> None:
    code, output = _run(module, tmp_path)
    assert code == 0
    html = output.read_text(encoding="utf-8")
    assert "4059fbd" in html
    assert "Backend (Python 3.12)" in html
    assert "Pixel visual regression (Playwright)" in html
    assert "ci-chip ci-success" in html


def test_without_ci_file_the_header_says_not_recorded(module, tmp_path: Path) -> None:
    code, output = _run(module, tmp_path, with_ci=False)
    assert code == 0
    assert "CI 未记录" in output.read_text(encoding="utf-8")


def test_main_archives_history_and_landing(module, tmp_path: Path) -> None:
    code, output = _run(module, tmp_path)
    assert code == 0
    history = tmp_path / "reports" / "history"
    archived = history / "4059fbd" / "test-report.html"
    meta = history / "4059fbd" / "meta.json"
    assert archived.exists() and archived.read_text(
        encoding="utf-8"
    ) == output.read_text(encoding="utf-8")
    payload = json.loads(meta.read_text(encoding="utf-8"))
    assert payload["commit"] == _SHA
    assert payload["ci"]["conclusion"] == "success"
    assert (history / "index.html").exists()
    landing = tmp_path / "reports" / "index.html"
    assert landing.exists()
    assert "test-report.html" in landing.read_text(encoding="utf-8")


def test_history_prunes_to_keep_limit(module, tmp_path: Path) -> None:
    history = tmp_path / "reports" / "history"
    for index, sha in enumerate(["aaaaaaa", "bbbbbbb", "ccccccc"]):
        directory = history / sha
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(
            json.dumps(
                {"commit": sha, "generated_at": f"2026-10-2{index}T00:00:00+08:00"}
            ),
            encoding="utf-8",
        )
    code, _ = _run(module, tmp_path, keep=2)
    assert code == 0
    remaining = sorted(
        path.name for path in history.iterdir() if path.is_dir() and len(path.name) == 7
    )
    assert remaining == ["4059fbd", "ccccccc"]


def test_keep_history_clamps_to_at_least_one(module, tmp_path: Path) -> None:
    code, _ = _run(module, tmp_path, keep=0)
    assert code == 0
    history = tmp_path / "reports" / "history"
    remaining = [
        path.name for path in history.iterdir() if path.is_dir() and len(path.name) == 7
    ]
    assert remaining == ["4059fbd"]


def test_job_state_maps_gh_statuses(module) -> None:
    assert module._job_state("completed", "success") == "success"
    assert module._job_state("completed", "failure") == "failure"
    assert module._job_state("completed", "timed_out") == "timed_out"
    assert module._job_state("queued", None) == "pending"
    assert module._job_state("in_progress", None) == "pending"
    assert module._job_state("weird", None) == "neutral"


def test_detect_commit_reads_head(module) -> None:
    repo_root = _SCRIPT.parents[2]
    assert len(module.detect_commit(repo_root)) == 40
    assert all(char in "0123456789abcdef" for char in module.detect_commit(repo_root))


def test_detect_commit_degrades_to_empty_outside_a_repo(module, tmp_path: Path) -> None:
    assert module.detect_commit(tmp_path) == ""


def test_garbage_ci_file_degrades_to_a_note(module, tmp_path: Path) -> None:
    artifacts = tmp_path / "reports" / "artifacts"
    artifacts.mkdir(parents=True)
    bad = artifacts / "ci.json"
    bad.write_text("{not json", encoding="utf-8")
    code = module.main(
        [
            "--artifacts-dir",
            str(artifacts),
            "--output",
            str(tmp_path / "reports" / "test-report.html"),
            "--repo-root",
            str(tmp_path),
            "--commit",
            _SHA,
            "--ci-file",
            str(bad),
        ]
    )
    assert code == 0
    html = (tmp_path / "reports" / "test-report.html").read_text(encoding="utf-8")
    assert "CI 记录读取失败" in html
    assert "CI 未记录" in html


def test_header_ci_chip_failure_and_pending_states(module, tmp_path: Path) -> None:
    payload = _ci_payload()
    payload["jobs"] = [
        {
            "name": "Backend (Python 3.12)",
            "status": "completed",
            "conclusion": "failure",
        },
        {"name": "Frontend (Node 22)", "status": "in_progress", "conclusion": None},
    ]
    payload["conclusion"] = "failure"
    code, output = _run(module, tmp_path, ci_override=payload)
    assert code == 0
    html = output.read_text(encoding="utf-8")
    assert "ci-chip ci-failure" in html
    assert "ci-chip ci-pending" in html


def test_header_ci_unavailable_shows_reason(module, tmp_path: Path) -> None:
    payload = _ci_payload()
    payload["status"] = "unavailable"
    payload["conclusion"] = None
    payload["error"] = "gh not found"
    payload["jobs"] = []
    code, output = _run(module, tmp_path, ci_override=payload)
    assert code == 0
    html = output.read_text(encoding="utf-8")
    assert "CI 不可用" in html
    assert "gh not found" in html


def test_landing_links_report_and_history(module, tmp_path: Path) -> None:
    code, _ = _run(module, tmp_path)
    assert code == 0
    landing = (tmp_path / "reports" / "index.html").read_text(encoding="utf-8")
    assert 'href="test-report.html"' in landing
    assert 'href="history/index.html"' in landing
    assert "4059fbd" in landing
