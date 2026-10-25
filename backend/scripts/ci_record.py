#!/usr/bin/env python3
"""Fetch the GitHub Actions record for one commit (dashboard 2026-10-26).

`make test-report` shells this in before generating the page: the report
header shows the commit's CI verdict (the three required jobs) next to the
local suite results. Every failure shape degrades to a status — a missing
`gh`, an offline machine, or a commit with no run must never sink the
stack startup:

- ``completed`` / ``queued`` / ``in_progress``: a run exists; ``jobs``
  carries the per-job statuses;
- ``none``: no run for this commit (local-only or pre-push);
- ``unavailable``: `gh` missing/failed (``error`` says why).

Standard library only. Tests inject a fake runner; the module itself
never touches the network outside the `gh` subprocess.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_JOBS = (
    "Backend (Python 3.12)",
    "Frontend (Node 22)",
    "Pixel visual regression (Playwright)",
)

Runner = Callable[..., Any]


@dataclass(frozen=True)
class CiJob:
    name: str
    status: str
    conclusion: str | None

    @property
    def state(self) -> str:
        """One of success / failure / pending / skipped / neutral."""
        if self.conclusion in {
            "success",
            "failure",
            "cancelled",
            "skipped",
            "timed_out",
        }:
            return self.conclusion
        if self.status in {"queued", "in_progress", "waiting", "requested"}:
            return "pending"
        return "neutral"


@dataclass(frozen=True)
class CiRecord:
    commit: str
    status: str
    conclusion: str | None
    run_id: int | None
    run_url: str | None
    fetched_at: str
    jobs: tuple[CiJob, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def short_commit(self) -> str:
        return self.commit[:7] if self.commit else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "short_commit": self.short_commit,
            "status": self.status,
            "conclusion": self.conclusion,
            "run_id": self.run_id,
            "run_url": self.run_url,
            "fetched_at": self.fetched_at,
            "jobs": [
                {"name": job.name, "status": job.status, "conclusion": job.conclusion}
                for job in self.jobs
            ],
            "error": self.error,
        }


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def normalize(
    commit: str,
    runs_json: str,
    run_json: str | None,
    *,
    fetched_at: str,
    error: str | None = None,
) -> CiRecord:
    """Turn the two `gh --json` payloads into one record."""
    if error is not None:
        return CiRecord(
            commit=commit,
            status="unavailable",
            conclusion=None,
            run_id=None,
            run_url=None,
            fetched_at=fetched_at,
            error=error,
        )
    runs = _parse_json(runs_json)
    if not isinstance(runs, list) or not runs:
        return CiRecord(
            commit=commit,
            status="none",
            conclusion=None,
            run_id=None,
            run_url=None,
            fetched_at=fetched_at,
        )
    run = runs[0] if isinstance(runs[0], dict) else {}
    view = _parse_json(run_json or "") if run_json else None
    view = view if isinstance(view, dict) else {}
    jobs = tuple(
        CiJob(
            name=str(job.get("name", "")),
            status=str(job.get("status", "unknown")),
            conclusion=job.get("conclusion"),
        )
        for job in view.get("jobs", [])
        if isinstance(job, dict)
    )
    run_id = run.get("databaseId")
    return CiRecord(
        commit=str(run.get("headSha") or commit),
        status=str(view.get("status") or run.get("status") or "unknown"),
        conclusion=view.get("conclusion") or run.get("conclusion"),
        run_id=run_id if isinstance(run_id, int) else None,
        run_url=str(view.get("url") or run.get("url") or "") or None,
        fetched_at=fetched_at,
        jobs=jobs,
    )


def _run_gh(argv: list[str], runner: Runner) -> str:
    result = runner(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "gh failed").strip())
    return str(result.stdout)


def fetch(
    repo_root: Path,
    commit: str,
    *,
    runner: Runner = subprocess.run,
    fetched_at: str | None = None,
) -> CiRecord:
    """Fetch one commit's CI record; never raises for remote failures."""
    stamp = fetched_at or datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        runs_json = _run_gh(
            [
                "gh",
                "run",
                "list",
                "--commit",
                commit,
                "--limit",
                "1",
                "--json",
                "databaseId,headSha,status,conclusion,url,workflowName",
            ],
            runner,
        )
        runs = _parse_json(runs_json)
        if not isinstance(runs, list) or not runs:
            return normalize(commit, runs_json, None, fetched_at=stamp)
        run_id = runs[0].get("databaseId") if isinstance(runs[0], dict) else None
        if run_id is None:
            return normalize(commit, runs_json, None, fetched_at=stamp)
        view_json = _run_gh(
            ["gh", "run", "view", str(run_id), "--json", "jobs,status,conclusion,url"],
            runner,
        )
        return normalize(commit, runs_json, view_json, fetched_at=stamp)
    except FileNotFoundError as exc:
        return normalize(
            commit, "", None, fetched_at=stamp, error=f"gh not found: {exc}"
        )
    except (RuntimeError, OSError) as exc:
        return normalize(commit, "", None, fetched_at=stamp, error=str(exc))


def write_record(record: CiRecord, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_record(path: Path) -> CiRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = tuple(
        CiJob(
            name=str(job.get("name", "")),
            status=str(job.get("status", "unknown")),
            conclusion=job.get("conclusion"),
        )
        for job in payload.get("jobs", [])
        if isinstance(job, dict)
    )
    return CiRecord(
        commit=str(payload.get("commit", "")),
        status=str(payload.get("status", "unknown")),
        conclusion=payload.get("conclusion"),
        run_id=payload.get("run_id"),
        run_url=payload.get("run_url"),
        fetched_at=str(payload.get("fetched_at", "")),
        jobs=jobs,
        error=payload.get("error"),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record one commit's CI status.")
    parser.add_argument("--commit", required=True, help="full git commit sha")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSON output (default: reports/artifacts/ci.json)",
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--fetched-at", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.repo_root or Path(__file__).resolve().parents[2]
    output: Path = args.output or repo_root / "reports" / "artifacts" / "ci.json"
    record = fetch(repo_root, args.commit, fetched_at=args.fetched_at)
    write_record(record, output)
    print(
        f"wrote {output} (CI: {record.status}"
        + (f" / {record.conclusion}" if record.conclusion else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
