# backend/tests/integration/submissions/test_approve_honors.py
"""The approve path's lifetime-honor trigger (spec §18; plan 05 final
review I3).

``ReviewService.approve_submission`` hands the completed user to honor
evaluation AFTER the grant transaction commits, through the
``ClaimCompletedHonorsPort`` seam the composition root binds to the
rankings module's ``ClaimCompletedHonorsTrigger``. Two properties:

- **The trigger fires and grants:** approving a student's FIRST
  completed claim crosses the TOTAL_COMPLETED-1 threshold (首次完成
  任务), and the lazily created definition row plus the UserHonor land
  in the trigger's OWN transaction (committed by the defensive wrapper,
  never by the approve transaction);
- **Failure-tolerant by contract:** an honor evaluation that raises
  must NEVER fail an approval that already committed — the wrapper
  rolls the honor transaction back, logs, and the approve result stands
  (claim COMPLETED, reward granted). A lost trigger self-heals on any
  later evaluation (the honor service's facts-not-deltas rule).

The DAILY_RANK / MONTHLY_RANK honors have NO producer on this path by
design: their rank+period facts exist only after a period closes, which
is the Plan 07/08 scheduled-beat producer's job (see honor_service's
module docstring).

Harness: the shared ``db_session`` fixture (outer transaction rolled
back per test); the world is seeded directly through the tasks models
the way test_review_flow.py builds them, and the points port is the
in-memory fake — the honor facts this file exercises (completed claim
count) do not depend on the ledger row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.rankings.honor_models import Honor, UserHonor
from app.modules.rankings.honor_service import (
    ClaimCompletedHonorsTrigger,
    HonorService,
)
from app.modules.submissions.enums import ValidationStatus
from app.modules.submissions.models import Submission
from app.modules.submissions.review_service import (
    ApprovalResult,
    ClaimCompletedHonorsPort,
    ReviewService,
)
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task
from tests.fakes.points import FakePointsRewardPort

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_DEADLINE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
_GRACE = _DEADLINE + timedelta(hours=24)
_REVIEWED_AT = _DEADLINE + timedelta(hours=2)


class _ExplodingHonors:
    """A ClaimCompletedHonorsPort whose evaluation always fails — the
    failure-tolerance contract's probe."""

    def __init__(self) -> None:
        self.calls = 0

    async def on_claim_completed(self, session: AsyncSession, user_id: UUID) -> None:
        self.calls += 1
        raise RuntimeError("injected honor evaluation failure")


async def _seed_world(db_session: AsyncSession) -> tuple[User, User, AssignmentClaim]:
    """Owner + student, a PUBLISHED task, an OCCUPIED assignment, an
    UNDER_REVIEW claim with a PROVISIONAL lock (on-time tier 100), and
    the current VALIDATED submission (the review-flow seed shape)."""
    owner = User(
        username=f"t{uuid4().hex[:8]}",
        password_hash=_PASSWORD_HASH,
        nickname="测试教师",
        phone_e164=None,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    student = User(
        username=f"2025s{uuid4().hex[:8]}",
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add_all((owner, student))
    await db_session.flush()
    task = Task(
        owner_teacher_id=owner.id,
        title="食堂档口排队时长采集",
        description="采集午市高峰各档口的排队时长。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"required_columns": [{"name": "url", "type": "string"}]},
        submission_schema_version=1,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
    )
    db_session.add(task)
    await db_session.flush()
    assignment = Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword="食堂排队",
        availability_status=AssignmentAvailability.OCCUPIED.value,
    )
    db_session.add(assignment)
    await db_session.flush()
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=student.id,
        status=ClaimStatus.UNDER_REVIEW.value,
        claimed_at=_DEADLINE - timedelta(days=3),
        deadline_at=_DEADLINE,
        grace_deadline_at=_GRACE,
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.PROVISIONAL.value,
        reward_tier_locked=100,
        locked_reward_points=100,
        reward_locked_at=_DEADLINE,
    )
    db_session.add(claim)
    await db_session.flush()
    submission = Submission(
        claim_id=claim.id,
        version=1,
        object_key=f"submissions/{claim.id}/{uuid4()}",
        original_filename="排队.csv",
        declared_type="CSV",
        file_size=128,
        submitted_at=_DEADLINE,
        validation_status=ValidationStatus.VALIDATED.value,
        review_status="PENDING_REVIEW",
        retention_until=_DEADLINE + timedelta(days=180),
    )
    db_session.add(submission)
    await db_session.flush()
    claim.latest_submission_id = submission.id
    await db_session.flush()
    return owner, student, claim


def _service(
    honors: ClaimCompletedHonorsPort | None,
) -> ReviewService:
    return ReviewService(
        clock=FrozenClock(_REVIEWED_AT),
        events=InMemoryEventCollector(),
        points=FakePointsRewardPort(),
        honors=honors,
    )


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


async def _owned_honors(
    db_session: AsyncSession, user_id: UUID
) -> list[tuple[Honor, UserHonor]]:
    return list(
        (
            await db_session.execute(
                select(Honor, UserHonor)
                .join(UserHonor, UserHonor.honor_id == Honor.id)
                .where(UserHonor.user_id == user_id)
            )
        ).all()
    )


# --- the trigger fires and grants ----------------------------------------------------


@pytest.mark.integration
async def test_approve_grants_the_first_completion_honor_after_commit(
    db_session: AsyncSession,
) -> None:
    """Approving the student's FIRST completed claim crosses the
    TOTAL_COMPLETED-1 threshold: the real rankings trigger grants 首次
    完成任务 in its own post-commit transaction, while the approve
    result itself is untouched by the honors write."""
    owner, student, claim = await _seed_world(db_session)
    service = _service(ClaimCompletedHonorsTrigger(honors=HonorService()))

    result = await service.approve_submission(
        db_session, _actor(owner), claim.latest_submission_id
    )

    assert isinstance(result, ApprovalResult)
    assert result.already_reviewed is False
    assert result.grant is not None
    assert result.grant.points_granted == 100
    reloaded = await db_session.get(AssignmentClaim, claim.id)
    assert reloaded is not None
    assert reloaded.status == ClaimStatus.COMPLETED.value

    granted = await _owned_honors(db_session, student.id)
    assert [
        (honor.name, honor.honor_type, honor.period) for honor, _user_honor in (granted)
    ] == [("首次完成任务", "TOTAL_COMPLETED", None)]


@pytest.mark.integration
async def test_approve_survives_honor_evaluation_failure(
    db_session: AsyncSession,
) -> None:
    """The failure-tolerance contract: an honor evaluation that raises
    after the approve committed is logged and swallowed — the claim
    stays COMPLETED, the grant stands, and no honor rows leak from the
    aborted honor transaction."""
    owner, student, claim = await _seed_world(db_session)
    exploding = _ExplodingHonors()
    service = _service(exploding)

    result = await service.approve_submission(
        db_session, _actor(owner), claim.latest_submission_id
    )

    assert isinstance(result, ApprovalResult)
    assert result.already_reviewed is False  # the approval committed first
    assert exploding.calls == 1  # the trigger ran (and failed) exactly once
    reloaded = await db_session.get(AssignmentClaim, claim.id)
    assert reloaded is not None
    assert reloaded.status == ClaimStatus.COMPLETED.value
    assert reloaded.reward_lock_status == RewardLockStatus.CONFIRMED.value
    assert await _owned_honors(db_session, student.id) == []
