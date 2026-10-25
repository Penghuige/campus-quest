# backend/tests/unit/scripts/test_ci_record.py
"""CI record fetcher (dashboard 2026-10-26): GitHub Actions status for one
commit, normalized into a small JSON the report generator renders.

The fetcher shells out to `gh`; tests inject a fake runner and never touch
the network. Failure shapes (no run, gh missing, gh error) must degrade to
a record with a status — never an exception that would sink `make stack`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ci_record.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ci_record_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ci():
    return _load_module()


_RUNS = [
    {
        "databaseId": 40199928552,
        "headSha": "4059fbd7b915011db80bb2b29f4671e03538e0ad",
        "status": "completed",
        "conclusion": "success",
        "url": "https://github.com/Penghuige/campus-quest/actions/runs/40199928552",
        "workflowName": "CI",
    }
]

_VIEW = {
    "status": "completed",
    "conclusion": "success",
    "url": "https://github.com/Penghuige/campus-quest/actions/runs/40199928552",
    "jobs": [
        {
            "name": "Backend (Python 3.12)",
            "status": "completed",
            "conclusion": "success",
        },
        {"name": "Frontend (Node 22)", "status": "completed", "conclusion": "success"},
        {
            "name": "Pixel visual regression (Playwright)",
            "status": "completed",
            "conclusion": "success",
        },
    ],
}


def test_normalize_a_found_run(ci) -> None:
    record = ci.normalize(
        "4059fbd7b915011db80bb2b29f4671e03538e0ad",
        json.dumps(_RUNS),
        json.dumps(_VIEW),
        fetched_at="2026-10-26T01:00:00+08:00",
    )
    assert record.status == "completed"
    assert record.conclusion == "success"
    assert record.run_id == 40199928552
    assert record.short_commit == "4059fbd"
    assert [job.name for job in record.jobs][0] == "Backend (Python 3.12)"
    assert record.error is None


def test_no_run_is_none_not_unavailable(ci) -> None:
    record = ci.normalize("deadbeef", "[]", None, fetched_at="t")
    assert record.status == "none"
    assert record.jobs == ()
    assert record.run_id is None


def test_gh_error_is_unavailable_with_message(ci) -> None:
    record = ci.normalize(
        "deadbeef", "", None, fetched_at="t", error="gh: not authenticated"
    )
    assert record.status == "unavailable"
    assert record.error == "gh: not authenticated"


def test_fetch_shells_list_then_view(ci) -> None:
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        stdout = json.dumps(_RUNS if len(calls) == 1 else _VIEW)
        return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    record = ci.fetch(
        Path("/repo"), "4059fbd7b915011db80bb2b29f4671e03538e0ad", runner=runner
    )
    assert record.conclusion == "success"
    assert calls[0][:4] == ["gh", "run", "list", "--commit"]
    assert calls[1][:4] == ["gh", "run", "view", "40199928552"]
    assert "--json" in calls[0] and "--json" in calls[1]


def test_fetch_missing_gh_degrades(ci) -> None:
    def runner(argv, **kwargs):
        raise FileNotFoundError("gh")

    record = ci.fetch(Path("/repo"), "deadbeef", runner=runner)
    assert record.status == "unavailable"
    assert "gh" in (record.error or "")


def test_fetch_nonzero_exit_degrades(ci) -> None:
    def runner(argv, **kwargs):
        return type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "API rate limit"}
        )()

    record = ci.fetch(Path("/repo"), "deadbeef", runner=runner)
    assert record.status == "unavailable"
    assert "rate limit" in (record.error or "")


def test_write_and_load_roundtrip(ci, tmp_path: Path) -> None:
    record = ci.normalize(
        "4059fbd7b915011db80bb2b29f4671e03538e0ad",
        json.dumps(_RUNS),
        json.dumps(_VIEW),
        fetched_at="t",
    )
    out = tmp_path / "ci.json"
    ci.write_record(record, out)
    loaded = ci.load_record(out)
    assert loaded.conclusion == "success"
    assert loaded.run_url == record.run_url


def test_main_writes_record_with_injected_fetch(
    ci, tmp_path: Path, monkeypatch
) -> None:
    fixed = ci.normalize(
        "4059fbd7b915011db80bb2b29f4671e03538e0ad",
        json.dumps(_RUNS),
        json.dumps(_VIEW),
        fetched_at="2026-10-26T01:00:00+08:00",
    )
    monkeypatch.setattr(ci, "fetch", lambda repo_root, commit, **kwargs: fixed)
    out = tmp_path / "nested" / "ci.json"
    code = ci.main(
        [
            "--commit",
            "4059fbd7b915011db80bb2b29f4671e03538e0ad",
            "--output",
            str(out),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert out.exists()
    loaded = ci.load_record(out)
    assert loaded.conclusion == "success"
    assert loaded.run_id == 40199928552
