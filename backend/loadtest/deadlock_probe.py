"""Deadlock probe: dense in-process interleaving of the two real flows.

Evidence (PR #153 run; original logs later destroyed by stack.sh's
truncate-on-restart, since fixed): the deadlock victim was
``create_upload_intent`` (user-row lock, then claim ``FOR UPDATE`` at
upload_service.py:750) and the winner a single-column
``UPDATE assignment_claims SET status='UNDER_REVIEW'`` — the reward-lock
path's flush. HTTP-level load hits the race ~0.1% of intents; this probe
drives the REAL service functions against overlapping claims orders of
magnitude denser. On a deadlock it dumps the PostgreSQL log slice, which
names both transactions' statements.

Usage (stack env required):
  uv run python -m loadtest.deadlock_probe [seconds]
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.errors import BusinessError
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor, LoggingEventPublisher
from app.modules.submissions.models import Submission
from app.modules.submissions.reward_lock_service import RewardLockService
from app.modules.submissions.router import get_object_storage
from app.modules.submissions.upload_service import UploadService
from app.modules.tasks.models import AssignmentClaim
from loadtest.seed import seed_world

CLAIMS = 8
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0


class _NoDispatcher:
    """create_upload_intent never dispatches; only finalize does."""

    async def dispatch(
        self, *args: object, **kwargs: object
    ) -> None:  # pragma: no cover
        return None


async def _build_services() -> tuple[UploadService, RewardLockService]:
    settings = get_settings()
    uploads = UploadService(
        clock=SystemClock(),
        storage=get_object_storage(),
        upload_url_ttl=timedelta(seconds=settings.upload_url_ttl_seconds),
        intent_ttl=timedelta(seconds=settings.upload_intent_ttl_seconds),
        max_upload_bytes=settings.max_upload_bytes_default,
        dispatcher=_NoDispatcher(),  # type: ignore[arg-type]
    )
    reward = RewardLockService(clock=SystemClock(), events=LoggingEventPublisher())
    return uploads, reward


async def main() -> int:
    started = datetime.now(UTC)
    world = await seed_world(
        claim_slots=CLAIMS,
        race_users=0,
        race_slots=0,
        deadline_in_minutes=30,
        out_dir=Path("loadtest/runs/deadlock-probe"),
        force=True,
    )
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=4 * CLAIMS + 8)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uploads, reward = await _build_services()

    # One VALIDATED submission per claim: the state the worker's success
    # path presents to the reward-lock service.
    pairs: list[tuple[UUID, UUID, UUID]] = []  # (claim, user, submission)
    async with maker() as db:
        claims = (
            await db.scalars(
                select(AssignmentClaim).where(
                    AssignmentClaim.task_id == UUID(world["task_id"])
                )
            )
        ).all()
        now = datetime.now(UTC)
        for claim in claims:
            submission = Submission(
                claim_id=claim.id,
                version=1,
                object_key=f"loadtest/deadlock-probe/{claim.id}",
                original_filename="probe.csv",
                declared_type="CSV",
                file_size=10,
                submitted_at=now - timedelta(minutes=1),
                retention_permanent=True,
                validation_status="VALIDATED",
            )
            db.add(submission)
            claim.status = "VALIDATING"
            pairs.append((claim.id, claim.user_id, submission.id))
        await db.commit()

    counters = {"intent_ok": 0, "intent_business": 0, "reward_ok": 0}
    deadlocked = asyncio.Event()
    report: list[str] = []

    async def _worker_loop(claim_id: UUID, submission_id: UUID) -> None:
        while not deadlocked.is_set() and time.monotonic() - t0 < DURATION:
            async with maker() as db:
                try:
                    claim = await db.get(AssignmentClaim, claim_id)
                    assert claim is not None
                    claim.status = "VALIDATING"
                    await db.commit()
                    await reward.on_validation_passed(db, submission_id)
                    counters["reward_ok"] += 1
                except BusinessError:
                    counters["reward_ok"] += 1  # raced state; locks still danced
                except DBAPIError as exc:
                    if "deadlock" in str(exc).lower():
                        report.append(f"WORKER flow deadlocked: {exc}")
                        deadlocked.set()
                    else:
                        report.append(f"WORKER dbapi: {exc}")

    async def _intent_loop(claim_id: UUID, user_id: UUID) -> None:
        actor = Actor(user_id=user_id, role=Role.STUDENT)
        while not deadlocked.is_set() and time.monotonic() - t0 < DURATION:
            async with maker() as db:
                try:
                    await uploads.create_upload_intent(
                        db,
                        actor,
                        claim_id=claim_id,
                        filename="probe.csv",
                        declared_type="CSV",
                        size=1024,
                    )
                    counters["intent_ok"] += 1
                except BusinessError:
                    counters["intent_business"] += 1  # locks still taken first
                except DBAPIError as exc:
                    if "deadlock" in str(exc).lower():
                        report.append(f"INTENT flow deadlocked: {exc}")
                        deadlocked.set()
                    else:
                        report.append(f"INTENT dbapi: {exc}")

    t0 = time.monotonic()
    tasks = []
    for claim_id, user_id, submission_id in pairs:
        tasks.append(_worker_loop(claim_id, submission_id))
        tasks.append(_intent_loop(claim_id, user_id))
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    await engine.dispose()

    print(
        f"probe: {elapsed:.1f}s, claims={CLAIMS}, "
        f"intent_ok={counters['intent_ok']}, "
        f"intent_business={counters['intent_business']}, "
        f"reward_ok={counters['reward_ok']}"
    )
    if report:
        print("\n".join(report))
        print(">>> dump PG log slice with:")
        print(
            "docker logs campusquest-postgres-1 --since "
            f"'{started.isoformat()}' | grep -A20 'deadlock detected'"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
