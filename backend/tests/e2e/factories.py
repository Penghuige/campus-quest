# backend/tests/e2e/factories.py
"""Deterministic seeding + FK-ordered cleanup for the full-system e2e
suite (plan 10 task 1).

Everything seeds through the ORM directly (the composition-smoke
``_seed_world`` style): the e2e suite owns its world-building and only
the FLOW under test goes through public APIs/workers. Every seed call
COMITS inside its own session — committed mode means later task modules
(the API request transaction, the per-job worker engines) must see
these rows, which an uncommitted insert would not deliver.

Uniqueness across runs (and against any leftover rows from a crashed
earlier run) comes from the ``run`` marker every factory requires; ids
are server-generated UUIDs, so fixture id independence needs no extra
machinery — the smoke test pins it anyway.

``clean_world`` is the teardown half: committed DELETEs in FK order
(the cleanup-harness / composition-smoke precedent) scoped to THIS
test's seeded ids, plus the honor-definition snapshot delta (the
approve path lazily creates global ``honors`` rows — deleting only the
delta never touches another suite's seeded definitions). Deferred to
later task modules, deliberately: audit rows (no FKs, nothing blocks on
them) and ``student_whitelist`` entries (flow-scoped, created through
the public API with run-prefixed student numbers, not by these
factories).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.modules.community.models import (
    Comment,
    CommentReaction,
    CommentReport,
    CommentRevision,
    CommentVote,
    TaskRating,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import (
    RecoveryCode,
    StaffInvitation,
    StudentWhitelist,
    TotpCredential,
    User,
    UserSession,
)
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
    RewardReviewGrant,
)
from app.modules.rankings.honor_models import Honor, UserHonor
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
    SubmissionValidation,
    UploadIntent,
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
from app.modules.tasks.models import Assignment, AssignmentClaim, Task, TaskCollaborator

#: Shared plaintext for every seeded account (returned on each fixture
#: so callers and the frontend ``username:password`` contract never
#: guess it). Test-only, never a real credential.
DEFAULT_PASSWORD = "correct-horse-battery"

#: A legal CSV schema the seeded Task accepts (the composition-smoke
#: shape): url unique string + title string.
CSV_SUBMISSION_SCHEMA: dict[str, Any] = {
    "required_columns": [
        {"name": "url", "type": "string", "unique": True},
        {"name": "title", "type": "string"},
    ]
}


@dataclass(frozen=True, slots=True)
class UserFixture:
    """One seeded account; ``user_id`` is the DB id, ``username`` the
    login handle (student number for students)."""

    user_id: UUID
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class TaskFixture:
    """One seeded PUBLISHED Task with its AVAILABLE Assignments."""

    task_id: UUID
    assignment_ids: list[UUID]
    owner_teacher_id: UUID


@dataclass(frozen=True, slots=True)
class RewardItemFixture:
    """One seeded enabled RewardItem with bounded stock."""

    reward_item_id: UUID
    name: str
    point_cost: int


def _now() -> datetime:
    return datetime.now(UTC)


def _unique_phone(run: str) -> str:
    """A run-unique E.164 mobile number for the ACTIVE-student rule.

    Deterministic in ``run`` (hex digits -> an 8-digit tail): unlike
    ``hash()``, stable across processes, so a rerun of the same ``run``
    reuses the same number instead of colliding with leftovers."""
    digits = str(int(run[:8], 16) % 10**8).zfill(8)
    return f"+86138{digits}"


async def seed_student(
    factory: async_sessionmaker[AsyncSession], *, run: str
) -> UserFixture:
    """One ACTIVE STUDENT with a bound phone, committed (visible to the
    API request transactions and the per-job worker engines alike).

    The username is a REAL student number's shape — 6-20 ASCII DIGITS
    (the register flow's own band, and the login form's client mirror):
    the run marker's first 11 hex chars fold to zero-padded decimal, so
    usernames stay run-unique AND domain-legal (a hex-lettered handle
    would be untypable into the student login)."""
    digits = str(int(run[:11], 16) % 10**11).zfill(11)
    async with factory() as db:
        student = User(
            username=f"2025{digits}001",
            password_hash=hash_password(DEFAULT_PASSWORD),
            nickname=f"端到端同学{run[:4]}",
            phone_e164=_unique_phone(run),
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
        db.add(student)
        await db.commit()
        return UserFixture(
            user_id=student.id,
            username=student.username,
            password=DEFAULT_PASSWORD,
        )


async def seed_teacher_confirmed_totp(
    factory: async_sessionmaker[AsyncSession], *, run: str
) -> UserFixture:
    """One ACTIVE TEACHER with a CONFIRMED TOTP credential — the
    management-guard precondition teacher-surface routes check (the
    composition-smoke stand-in secret shape; flow tests that must
    actually answer a TOTP prompt derive codes from the same bytes)."""
    async with factory() as db:
        teacher = User(
            username=f"t{run}",
            password_hash=hash_password(DEFAULT_PASSWORD),
            nickname=f"端到端教师{run[:4]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        db.add(teacher)
        await db.flush()
        db.add(
            TotpCredential(
                user_id=teacher.id,
                secret_encrypted=f"e2e-totp-stand-in:{run}".encode(),
                confirmed_at=_now(),
            )
        )
        await db.commit()
        return UserFixture(
            user_id=teacher.id,
            username=teacher.username,
            password=DEFAULT_PASSWORD,
        )


async def seed_admin(
    factory: async_sessionmaker[AsyncSession], *, run: str
) -> UserFixture:
    """One ACTIVE ADMIN account (no TOTP stand-in: the admin surface
    authenticates with password sessions in these flows)."""
    async with factory() as db:
        admin = User(
            username=f"a{run}",
            password_hash=hash_password(DEFAULT_PASSWORD),
            nickname=f"端到端管理员{run[:4]}",
            phone_e164=None,
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
        db.add(admin)
        await db.commit()
        return UserFixture(
            user_id=admin.id,
            username=admin.username,
            password=DEFAULT_PASSWORD,
        )


async def seed_task_with_assignments(
    factory: async_sessionmaker[AsyncSession],
    *,
    teacher_id: UUID,
    run: str,
    assignment_count: int = 3,
    duration_minutes: int = 4320,
) -> TaskFixture:
    """One PUBLISHED RELATIVE CSV Task owned by ``teacher_id`` with
    ``assignment_count`` AVAILABLE Assignments, committed.

    The contract matches the composition smoke: ``duration_minutes`` (3
    days by default — an immediate submission is on-time), 100 base
    points, CSV-only policy against ``CSV_SUBMISSION_SCHEMA`` — the shape
    tasks 2-4 build their claim/submit/deadline stories on. The deadline
    flows (task 3) pass a shorter duration so a +2h/+7h late submit is a
    small clock step, not a multi-day one. Claim-time snapshots (deadline,
    reward policy) stay the claim service's job; nothing is pre-claimed
    here."""
    if assignment_count < 1:
        raise ValueError("assignment_count must be >= 1")
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    async with factory() as db:
        now = _now()
        task = Task(
            owner_teacher_id=teacher_id,
            title=f"端到端数据采集任务{run[:6]}",
            description="端到端 fixtures 播种的采集任务。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=duration_minutes,
            submission_schema=CSV_SUBMISSION_SCHEMA,
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
            published_at=now - timedelta(days=1),
        )
        db.add(task)
        await db.flush()
        assignments = [
            Assignment(
                task_id=task.id,
                platform="xiaohongshu",
                keyword=f"端到端{run[:4]}{index}",
                availability_status=AssignmentAvailability.AVAILABLE,
            )
            for index in range(assignment_count)
        ]
        db.add_all(assignments)
        await db.commit()
        return TaskFixture(
            task_id=task.id,
            assignment_ids=[assignment.id for assignment in assignments],
            owner_teacher_id=teacher_id,
        )


async def seed_reward_item(
    factory: async_sessionmaker[AsyncSession], *, run: str, point_cost: int = 50
) -> RewardItemFixture:
    """One enabled RewardItem with bounded stock (10) and no availability
    window or per-term limit, committed — redeemable immediately."""
    async with factory() as db:
        item = RewardItem(
            name=f"端到端奖励卡{run[:6]}",
            description="端到端 fixtures 播种的兑换奖品。",
            point_cost=point_cost,
            stock=10,
            per_user_term_limit=None,
            available_from=None,
            available_until=None,
            enabled=True,
            requires_manual_review=False,
            fulfillment_instructions="端到端测试交付说明",
        )
        db.add(item)
        await db.commit()
        return RewardItemFixture(
            reward_item_id=item.id, name=item.name, point_cost=point_cost
        )


async def seed_admin_confirmed_totp(
    factory: async_sessionmaker[AsyncSession], *, run: str
) -> UserFixture:
    """One ACTIVE ADMIN with a CONFIRMED TOTP credential.

    The admin management surfaces (whitelist import, reward catalogue)
    and the redemption-review guard both require the confirmed-credential
    ROW (spec §33.4) — the stand-in secret shape is the teacher
    factory's; flows that must ANSWER a TOTP prompt do their own setup
    through the invitation surface instead."""
    async with factory() as db:
        admin = User(
            username=f"a{run}",
            password_hash=hash_password(DEFAULT_PASSWORD),
            nickname=f"端到端管理员{run[:4]}",
            phone_e164=None,
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
        db.add(admin)
        await db.flush()
        db.add(
            TotpCredential(
                user_id=admin.id,
                secret_encrypted=f"e2e-totp-stand-in:{run}".encode(),
                confirmed_at=_now(),
            )
        )
        await db.commit()
        return UserFixture(
            user_id=admin.id,
            username=admin.username,
            password=DEFAULT_PASSWORD,
        )


async def seed_whitelist_entry(
    factory: async_sessionmaker[AsyncSession], *, student_number: str
) -> None:
    """One ENABLED whitelist row, committed — the register flow's
    precondition (spec §5.1). World-building only: the whitelist ADMIN
    CRUD stays behind its own suite."""
    async with factory() as db:
        db.add(StudentWhitelist(student_number=student_number, enabled=True))
        await db.commit()


@dataclass(frozen=True, slots=True)
class ClaimFixture:
    """One ORM-seeded CLAIMED claim mirroring the claim service's
    snapshots (claim_service lines: status/deadlines/policy/base/schema
    version/lock NONE + assignment OCCUPIED)."""

    claim_id: UUID
    assignment_id: UUID
    task_id: UUID


async def seed_claim(
    factory: async_sessionmaker[AsyncSession], *, task: TaskFixture, student_id: UUID
) -> ClaimFixture:
    """Claim ``task``'s FIRST assignment for ``student_id`` directly
    through the ORM, committed.

    The browser suite needs a pre-submittable claim the specs can deep
    link to; driving the real claim route needs the page's login first,
    so world-building seeds the row with exactly the fields
    ``ClaimService.claim_random_assignment`` would have written
    (snapshots via ``compute_claim_deadlines`` + the V1 policy dict),
    minus its notification intents (inbox tests do not depend on them)."""
    from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
    from app.modules.tasks.deadlines import compute_claim_deadlines

    async with factory() as db:
        task_row = await db.get(Task, task.task_id)
        assert task_row is not None
        assignment = await db.get(Assignment, task.assignment_ids[0])
        assert assignment is not None
        claimed_at = _now()
        deadlines = compute_claim_deadlines(task_row, claimed_at)
        claim = AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task_row.id,
            user_id=student_id,
            status=ClaimStatus.CLAIMED,
            claimed_at=claimed_at,
            deadline_at=deadlines.deadline_at,
            grace_deadline_at=deadlines.grace_deadline_at,
            reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
            base_reward_points_snapshot=task_row.base_reward_points,
            submission_schema_version=task_row.submission_schema_version,
            reward_lock_status=RewardLockStatus.NONE,
        )
        assignment.availability_status = AssignmentAvailability.OCCUPIED
        db.add(claim)
        await db.commit()
        return ClaimFixture(
            claim_id=claim.id,
            assignment_id=assignment.id,
            task_id=task_row.id,
        )


async def seed_points_balance(
    factory: async_sessionmaker[AsyncSession],
    *,
    student_id: UUID,
    amount: int,
    source_id: UUID,
) -> datetime:
    """One ASSIGNMENT_REWARD ledger entry + wallet projection, committed.

    The browser redemption flows need spendable points BEFORE any test
    runs; the reward EARNING flow itself is the task-2 chain's job, so
    world-building posts the closing entry directly (the ledger service's
    exact column shape: ``ASSIGNMENT_CLAIM`` source, balance+ranking
    effects, effective-at now). Returns the ranking_effective_at the
    caller should hand the ranking projection."""
    now = _now()
    async with factory() as db:
        db.add(
            PointsLedger(
                user_id=student_id,
                ledger_type="ASSIGNMENT_REWARD",
                amount=amount,
                source_type="ASSIGNMENT_CLAIM",
                source_id=source_id,
                affects_balance=True,
                affects_ranking=True,
                ranking_effective_at=now,
            )
        )
        db.add(
            PointWallet(
                user_id=student_id,
                available_points=amount,
                earned_points=amount,
            )
        )
        await db.commit()
    return now


async def snapshot_honor_ids(
    factory: async_sessionmaker[AsyncSession],
) -> set[UUID]:
    """The GLOBAL honor-definition ids as they stand NOW — pass this to
    ``clean_world`` so teardown deletes only the definitions THIS test's
    approve paths lazily created (the composition-smoke discipline)."""
    async with factory() as db:
        return set((await db.execute(select(Honor.id))).scalars().all())


async def clean_world(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: Sequence[UUID] = (),
    task_ids: Sequence[UUID] = (),
    reward_item_ids: Sequence[UUID] = (),
    honor_ids_before: set[UUID] | None = None,
    whitelist_numbers: Sequence[str] = (),
) -> None:
    """Remove this test's committed rows in FK order, then commit.

    Scoped to the ids the seed factories returned (never another test's
    rows); ``honor_ids_before`` is the pre-test ``snapshot_honor_ids``
    set — the create-delta after it is removed last, once the deleted
    users have released their ``display_honor_id`` pointers. The
    self-referencing FKs along the way (sessions' ``replaced_by``,
    ledger ``reversal_of_id``) are plain NO ACTION constraints, so
    single-statement deletes of mutually-linked rows are safe.
    ``whitelist_numbers`` removes this run's student_whitelist rows (the
    register-flow precondition the flow itself seeded).
    """
    users = list(user_ids)
    tasks = list(task_ids)
    items = list(reward_item_ids)
    if not users and not tasks and not items and not whitelist_numbers:
        return
    async with factory() as db:
        if whitelist_numbers:
            await db.execute(
                delete(StudentWhitelist).where(
                    StudentWhitelist.student_number.in_(list(whitelist_numbers))
                )
            )
        claims = select(AssignmentClaim.id).where(AssignmentClaim.task_id.in_(tasks))
        submissions = select(Submission.id).where(Submission.claim_id.in_(claims))
        comments = select(Comment.id).where(
            Comment.task_id.in_(tasks) | Comment.user_id.in_(users)
        )

        # Community children first, then comments/ratings.
        await db.execute(
            delete(CommentRevision).where(CommentRevision.comment_id.in_(comments))
        )
        await db.execute(
            delete(CommentVote).where(CommentVote.comment_id.in_(comments))
        )
        await db.execute(
            delete(CommentReaction).where(CommentReaction.comment_id.in_(comments))
        )
        await db.execute(
            delete(CommentReport).where(CommentReport.comment_id.in_(comments))
        )
        await db.execute(delete(Comment).where(Comment.id.in_(comments)))
        await db.execute(
            delete(TaskRating).where(
                TaskRating.task_id.in_(tasks) | TaskRating.user_id.in_(users)
            )
        )

        # Submissions chain: upload_intents FIRST (their
        # finalized_submission_id FK points at submissions), then the
        # per-submission children, then the rows themselves.
        await db.execute(delete(UploadIntent).where(UploadIntent.claim_id.in_(claims)))
        await db.execute(
            delete(SubmissionValidation).where(
                SubmissionValidation.submission_id.in_(submissions)
            )
        )
        await db.execute(
            delete(SubmissionReview).where(
                SubmissionReview.submission_id.in_(submissions)
            )
        )
        await db.execute(
            delete(RewardLockHistory).where(RewardLockHistory.claim_id.in_(claims))
        )
        await db.execute(delete(Submission).where(Submission.id.in_(submissions)))
        await db.execute(delete(AssignmentClaim).where(AssignmentClaim.id.in_(claims)))

        # Task graph.
        await db.execute(delete(Assignment).where(Assignment.task_id.in_(tasks)))
        await db.execute(
            delete(TaskCollaborator).where(TaskCollaborator.task_id.in_(tasks))
        )
        await db.execute(delete(Task).where(Task.id.in_(tasks)))

        # Points/rewards: reservations before the redemptions they
        # freeze, redemptions before the items they reference.
        await db.execute(
            delete(PointReservation).where(PointReservation.user_id.in_(users))
        )
        await db.execute(
            delete(RewardRedemption).where(
                RewardRedemption.user_id.in_(users)
                | RewardRedemption.reward_item_id.in_(items)
                | RewardRedemption.decided_by.in_(users)
            )
        )
        await db.execute(delete(RewardItem).where(RewardItem.id.in_(items)))
        await db.execute(
            delete(RewardReviewGrant).where(
                RewardReviewGrant.teacher_id.in_(users)
                | RewardReviewGrant.granted_by.in_(users)
            )
        )
        # Ledger rows may reference EACH OTHER (reversal_of_id, a NO
        # ACTION self-FK): one single-statement DELETE of a set whose
        # members are mutually linked is fine ONLY when the whole set
        # goes in one statement — but a reversal row can point at a row
        # owned by ANOTHER test's user (the E4 recovery seed links its
        # reversal to its own reward, yet ordering across suites left
        # one orphaned link), so sever the pointers first: a plain
        # UPDATE ... SET reversal_of_id = NULL scoped to this test's
        # users cannot touch another test's rows.
        await db.execute(
            update(PointsLedger)
            .where(
                PointsLedger.user_id.in_(users),
                PointsLedger.reversal_of_id.is_not(None),
            )
            .values(reversal_of_id=None)
        )
        await db.execute(delete(PointsLedger).where(PointsLedger.user_id.in_(users)))
        await db.execute(delete(PointWallet).where(PointWallet.user_id.in_(users)))

        # Notifications: deliveries before the notifications they serve.
        await db.execute(
            delete(NotificationDelivery).where(NotificationDelivery.user_id.in_(users))
        )
        await db.execute(delete(Notification).where(Notification.user_id.in_(users)))

        # Identity: honors grants, staff artifacts, sessions, 2FA, then
        # the users themselves.
        await db.execute(delete(UserHonor).where(UserHonor.user_id.in_(users)))
        await db.execute(
            delete(StaffInvitation).where(StaffInvitation.created_by.in_(users))
        )
        await db.execute(delete(UserSession).where(UserSession.user_id.in_(users)))
        await db.execute(
            delete(TotpCredential).where(TotpCredential.user_id.in_(users))
        )
        await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id.in_(users)))
        await db.execute(delete(User).where(User.id.in_(users)))

        # LAST: honor definitions this test lazily created (the
        # snapshot delta — never another suite's seeded rows), now that
        # no user points at them.
        if honor_ids_before is not None:
            honor_ids_now = set((await db.execute(select(Honor.id))).scalars().all())
            created = honor_ids_now - honor_ids_before
            if created:
                await db.execute(delete(Honor).where(Honor.id.in_(created)))
        await db.commit()
