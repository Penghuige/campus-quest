"""S2 post-run checker: oversell and fairness (spec §8.2, §31.4).

The S2 guarantees live in two partial unique indexes (one active claim
per Assignment; one active claim per user per task). This checker proves
the OBSERVED data honors them after a claim rush, cross-checks the
summary's winner/loser counts, and demands zero 5xx among the losers
("stable business rejection", not infrastructure failure).

CLI: ``uv run python -m loadtest.check_s2 --run-dir <runs/ts/s2>`` —
exit 0 green, 1 violations; writes ``checks.json`` into the run dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.tasks.models import ACTIVE_CLAIM_STATUSES, Assignment, AssignmentClaim
from loadtest.env import loadtest_env

_ACTIVE_STATUSES = {status.value for status in ACTIVE_CLAIM_STATUSES}

#: The §8.4 claim-refusal family — a loser seeing anything else (or a
#: bare 4xx with no envelope) is not a stable business rejection
_ALLOWED_LOSER_CODES = frozenset(
    {
        "ASSIGNMENT_LIMIT_REACHED",
        "NO_ASSIGNMENT_AVAILABLE",
        "TASK_ACTIVE_CLAIM_EXISTS",
        "TASK_NOT_CLAIMABLE",
    }
)


async def run_checks(
    db: AsyncSession, world: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    """Oversell/fairness checks against one world + its run summary."""

    task_id = UUID(world["task_id"])
    violations: list[str] = []

    racers = [u for u in world["users"] if u.get("claim_id") is None]
    racer_ids = {UUID(u["user_id"]) for u in racers}
    preseeded_claim_ids = {
        UUID(u["claim_id"]) for u in world["users"] if u.get("claim_id") is not None
    }
    race_slots = max(0, int(world["slots_total"]) - len(preseeded_claim_ids))

    all_claims = (
        await db.scalars(
            select(AssignmentClaim).where(AssignmentClaim.task_id == task_id)
        )
    ).all()
    if not all_claims:
        violations.append(
            f"world task {task_id} has no claims in DB (re-seeded or wiped?)"
        )
    active = [c for c in all_claims if c.status in _ACTIVE_STATUSES]
    missing_preseeded = preseeded_claim_ids - {c.id for c in all_claims}
    for claim_id in sorted(missing_preseeded):
        violations.append(f"pre-seeded claim {claim_id} missing from DB")

    # winners = RACER-owned active claims (the S1 pre-seeded claims occupy
    # their own assignments and are asserted above instead)
    winners = [c for c in active if c.user_id in racer_ids]
    assignments = (
        await db.scalars(select(Assignment).where(Assignment.task_id == task_id))
    ).all()
    occupied = [a for a in assignments if a.availability_status == "OCCUPIED"]

    # (a) no oversell: winners == min(slots, racers) — under a full rush,
    # every race slot is taken and no more
    expected = min(race_slots, len(racers))
    if len(winners) != expected:
        violations.append(
            f"winners mismatch: DB active racer claims={len(winners)} "
            f"expected min(race_slots={race_slots}, racers={len(racers)})"
            f"={expected}"
        )

    # (b) no user holds two active claims on this task
    per_user = Counter(c.user_id for c in active)
    double = [uid for uid, n in per_user.items() if n > 1]
    for uid in double:
        violations.append(f"user {uid} holds {per_user[uid]} active claims")

    # (d) occupancy matches claims (racers' + pre-seeded)
    if len(occupied) != len(winners) + (
        len(preseeded_claim_ids) - len(missing_preseeded)
    ):
        violations.append(
            f"occupancy mismatch: OCCUPIED assignments={len(occupied)} "
            f"vs active claims={len(active)}"
        )

    # (c) summary agrees; losers rejected cleanly (no 5xx -> err 0)
    claim_stats = summary.get("by_name", {}).get("s2:claim", {})
    summary_ok = claim_stats.get("ok", 0)
    summary_rejected = claim_stats.get("rejected", 0)
    summary_err = claim_stats.get("err", 0)
    if summary_ok != len(winners):
        violations.append(
            f"summary winners mismatch: s2:claim ok={summary_ok} "
            f"vs DB active racer claims={len(winners)}"
        )
    expected_losers = len(racers) - len(winners)
    if summary_rejected != expected_losers:
        violations.append(
            f"summary losers mismatch: rejected={summary_rejected} "
            f"expected {expected_losers}"
        )
    if summary_err:
        violations.append(
            f"{summary_err} claim-rush requests failed with 5xx "
            "(losers must be clean business rejections)"
        )

    # (c, RF3) losers carry stable business codes — the §8.4 family.
    codes = summary.get("codes", {}).get("s2:claim", {})
    unstable = sorted(set(codes) - _ALLOWED_LOSER_CODES)
    if unstable:
        violations.append(
            f"unstable loser codes (expected only the §8.4 family): {unstable}"
        )
    if summary_rejected and not codes:
        violations.append(
            "no code evidence for rejected claims — the scenario did not "
            "capture BusinessError envelopes (verifiability gap)"
        )

    return {
        "ok": not violations,
        "violations": violations,
        "metrics": {
            "racers": len(racers),
            "race_slots": race_slots,
            "winners": len(winners),
            "losers": len(racers) - len(winners),
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", type=Path, required=True, help="e.g. loadtest/runs/<ts>/s2"
    )
    parser.add_argument(
        "--world",
        type=Path,
        default=None,
        help="world.json (default: <run-dir>/../world.json)",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    summary = json.loads((args.run_dir / "summary.json").read_text())
    world_path = args.world or (args.run_dir.parent / "world.json")
    world = json.loads(world_path.read_text())

    engine = create_async_engine(loadtest_env().database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            report = await run_checks(db, world, summary)
    finally:
        await engine.dispose()

    destination = args.run_dir / "checks.json"
    destination.write_text(json.dumps(report, indent=2, default=str))
    print(f"check_s2: {'OK' if report['ok'] else 'VIOLATIONS'} -> {destination}")
    for violation in report["violations"]:
        print(f"  ! {violation}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
