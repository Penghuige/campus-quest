"""S1 post-run correctness checker — the capacity report's centerpiece.

Reads the run's summary.json and the world it ran against, then proves
from the database that the guarantees held under load (spec §Goals 3):

- (a) no duplicate submissions per (claim, version) — the §32 anchor;
- (b) nothing lost: the summary's completed count equals DB rows;
- (c) deadline fairness per §9.3: every submission lands in a legal
  bucket; anything at/after grace is a violation;
- (d) the validation pipeline drained: no submission stuck non-terminal;
- (e) every reward-lock row carries the §9.3 tier its submission earns.

CLI: ``uv run python -m loadtest.check_s1 --run-dir <runs/ts/s1>`` —
exit 0 green, 1 violations; writes ``checks.json`` into the run dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionValidation,
)
from app.modules.tasks.models import AssignmentClaim
from loadtest.env import loadtest_env

_TERMINAL_VALIDATION = ("VALIDATED", "VALIDATION_FAILED")
_GRACE_HOURS = 24  # spec §6: grace pinned at 1440 minutes


def tier_bucket(
    submitted_at: datetime, deadline_at: datetime, grace_deadline_at: datetime
) -> int | None:
    """The §9.3 FIXED-deadline oracle; None means past grace (illegal)."""

    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=UTC)
    if deadline_at.tzinfo is None:
        deadline_at = deadline_at.replace(tzinfo=UTC)
    if submitted_at <= deadline_at:
        return 100
    if submitted_at < deadline_at + timedelta(hours=4):
        return 80
    if submitted_at < deadline_at + timedelta(hours=12):
        return 50
    if submitted_at < grace_deadline_at:
        return 20
    return None


async def run_checks(
    db: AsyncSession, world: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    """All five checks against one world + its run summary."""

    task_id = UUID(world["task_id"])
    violations: list[str] = []

    # (0) world presence — a vacuous pass is worse than a failure: if the
    # task is gone (re-seeded, or wiped by test cleanup) every count is
    # zero and the checks below would smile at 0 == 0.
    claims = (
        await db.scalars(
            select(AssignmentClaim).where(AssignmentClaim.task_id == task_id)
        )
    ).all()
    expected_preseeded = sum(
        1 for user in world["users"] if user.get("claim_id") is not None
    )
    if not claims:
        violations.append(f"world task {task_id} not found in DB (re-seeded or wiped?)")
    elif len(claims) < expected_preseeded:
        violations.append(
            f"world missing claims: DB has {len(claims)} "
            f"< expected {expected_preseeded} pre-seeded"
        )
    claim_ids = [claim.id for claim in claims]

    submissions: Sequence[Submission] = (
        (
            await db.scalars(
                select(Submission).where(Submission.claim_id.in_(claim_ids))
            )
        ).all()
        if claim_ids
        else []
    )

    # (a) §32 anchor: (claim_id, version) uniqueness as observed data
    seen: set[tuple[UUID, int]] = set()
    for submission in submissions:
        key = (submission.claim_id, submission.version)
        if key in seen:
            violations.append(f"duplicate (claim, version) submission: {key}")
        seen.add(key)

    # (b) nothing lost: summary's completed oks == DB rows
    complete_ok = summary.get("by_name", {}).get("s1:complete", {}).get("ok", 0)
    if complete_ok != len(submissions):
        violations.append(
            "submission count mismatch: summary complete ok="
            f"{complete_ok} vs DB rows={len(submissions)}"
        )

    # (c) deadline fairness + (e) tier oracle per submission
    deadline_by_claim = {claim.id: claim for claim in claims}
    for submission in submissions:
        claim = deadline_by_claim[submission.claim_id]
        bucket = tier_bucket(
            submission.submitted_at,
            claim.deadline_at,
            claim.grace_deadline_at,
        )
        if bucket is None:
            violations.append(
                f"post-grace submission {submission.id} on claim {claim.id}"
            )

    lock_rows: Sequence[RewardLockHistory] = (
        (
            await db.scalars(
                select(RewardLockHistory).where(
                    RewardLockHistory.claim_id.in_(claim_ids)
                )
            )
        ).all()
        if claim_ids
        else []
    )
    submission_by_id = {s.id: s for s in submissions}
    for row in lock_rows:
        submission = submission_by_id.get(row.submission_id)
        if submission is None:
            continue
        claim = deadline_by_claim[submission.claim_id]
        expected = tier_bucket(
            submission.submitted_at,
            claim.deadline_at,
            claim.grace_deadline_at,
        )
        if expected is None or row.reward_tier_locked != expected:
            violations.append(
                f"tier oracle violated on submission {submission.id}: "
                f"locked={row.reward_tier_locked} expected={expected}"
            )

    # (d) drain: no submission stuck non-terminal. Drain is measured PER
    # SUBMISSION (validation finished_at - submitted_at), not window
    # span — a 4-minute run's window span is 240 s regardless of how
    # fast the pipeline actually drained (review M1).
    pending = [
        s for s in submissions if s.validation_status not in _TERMINAL_VALIDATION
    ]
    for submission in pending:
        violations.append(
            f"validation not terminal for submission {submission.id} "
            f"({submission.validation_status})"
        )
    drains: list[float] = []
    if submissions:
        finished_by_sub = dict(
            (
                await db.execute(
                    select(
                        SubmissionValidation.submission_id,
                        func.max(SubmissionValidation.finished_at),
                    )
                    .where(
                        SubmissionValidation.submission_id.in_(
                            [s.id for s in submissions]
                        )
                    )
                    .group_by(SubmissionValidation.submission_id)
                )
            ).all()
        )
        for submission in submissions:
            finished = finished_by_sub.get(submission.id)
            if finished is not None:
                drains.append((finished - submission.submitted_at).total_seconds())

    return {
        "ok": not violations,
        "violations": violations,
        "metrics": {
            "submissions": len(submissions),
            "claims": len(claims),
            "reward_lock_rows": len(lock_rows),
            "drain_max_seconds": max(drains) if drains else None,
            "drain_mean_seconds": (sum(drains) / len(drains) if drains else None),
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", type=Path, required=True, help="e.g. loadtest/runs/<ts>/s1"
    )
    parser.add_argument(
        "--world",
        type=Path,
        default=None,
        help="world.json (default: <run-dir>/../world.json)",
    )
    parser.add_argument(
        "--wait-drain",
        type=int,
        default=90,
        help="seconds to poll for the validation pipeline to reach "
        "terminal state before checking (0 = check immediately)",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    summary = json.loads((args.run_dir / "summary.json").read_text())
    world_path = args.world or (args.run_dir.parent / "world.json")
    world = json.loads(world_path.read_text())

    engine = create_async_engine(loadtest_env().database_url)
    try:
        deadline = time.monotonic() + max(0, args.wait_drain)
        while True:
            async with async_sessionmaker(engine)() as db:
                report = await run_checks(db, world, summary)
            still_draining = any(
                "not terminal" in violation for violation in report["violations"]
            )
            if not still_draining or time.monotonic() >= deadline:
                break
            await asyncio.sleep(5)
    finally:
        await engine.dispose()

    destination = args.run_dir / "checks.json"
    destination.write_text(json.dumps(report, indent=2, default=str))
    print(f"check_s1: {'OK' if report['ok'] else 'VIOLATIONS'} -> {destination}")
    for violation in report["violations"]:
        print(f"  ! {violation}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
