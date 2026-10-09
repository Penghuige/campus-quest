"""Seed a believable demo world into the running dev database.

Usage (from backend/, dev-stack env exported — same shape as
seed_demo_accounts.py):
  uv run python scripts/seed_demo_content.py

Builds on the demo ACCOUNTS (seed_demo_accounts.py) and seeds the
content layer: published tasks across rarities/deadlines, a reward
catalogue, and one student in every interesting lifecycle state —
completed with granted points (ledger + wallet + ranking projection),
pending teacher review, validation-failed with a report, a fresh
in-progress claim, a pending reward redemption, community activity,
and notifications.

Seeding mirrors the exact ORM writes the services perform (the
e2e-factories / integration-test precedent): snapshots via
``compute_claim_deadlines`` + ``REWARD_POLICY_SNAPSHOT_V1``, the
review/approve field migration, the redemption reservation shape, and
a real CSV object per submitted file in MinIO so downloads work. The
ranking projection is rebuilt from the ledger afterwards via
``RankingRedisProjection.rebuild_all`` (PG stays the source of truth).

Idempotence: refuses to run when demo tasks (title prefix 【演示】)
already exist — the world is seeded once; per-run markers are not
needed on a curated demo box.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.modules.community.models import (
    Comment,
    CommentReaction,
    CommentVote,
    TaskRating,
)
from app.modules.identity.models import User
from app.modules.notifications.enums import NotificationEventType
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)
from app.modules.rankings.redis_projection import RankingRedisProjection
from app.modules.submissions.models import (
    RewardLockHistory,
    Submission,
    SubmissionReview,
    SubmissionValidation,
)
from app.modules.system.models import SystemSetting
from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
from app.modules.tasks.deadlines import compute_claim_deadlines
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

MARKER = "【演示】"
TERM_KEY = "2026-2027-1"
CSV_SCHEMA = {
    "required_columns": [
        {"name": "url", "type": "string", "unique": True},
        {"name": "title", "type": "string"},
    ]
}
DEMO_CSV = "url,title\nhttps://example.com/campus/demo-entry,演示采集数据\n"

_FIRST_LOCK_REASON = "首次机器校验通过，锁定奖励档位。"
_CONFIRM_LOCK_REASON = "人工审核通过，奖励锁确认。"
_INVALIDATE_REASON = "机器校验未通过，奖励锁失效，等待重新提交。"


def _put_demo_csv(settings, object_key: str) -> int:
    """Upload one real demo CSV object so presigned downloads work."""
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
    )
    body = DEMO_CSV.encode()
    client.put_object(Bucket=settings.s3_bucket, Key=object_key, Body=body)
    return len(body)


async def _student(session, username: str) -> User:
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        raise SystemExit(
            f"missing demo account {username} — run seed_demo_accounts.py first"
        )
    return user


def _report(
    row_count: int,
    *,
    duplicates: int = 0,
    errors: list | None = None,
    preview: list | None = None,
) -> dict:
    """A validation_report JSONB matching the persisted contract
    (schemas.ValidationReportPayload.from_persisted reads exactly these
    keys — the wire model 500s on any missing one)."""
    return {
        "parser_version": "demo-seed-1",
        "file_type": "CSV",
        "row_count": row_count,
        "detected_columns": ["url", "title"],
        "missing_required_columns": [],
        "extra_columns": [],
        "type_error_counts": {},
        "null_ratios": {"url": 0.0, "title": 0.0},
        "duplicate_counts": {"url": duplicates},
        "warnings": [],
        "errors": errors or [],
        "duration_ms": 1800.0,
        "preview_rows": preview
        if preview is not None
        else [["https://example.com/campus/demo-entry", "演示采集数据"]],
    }


class World:
    """Accumulates seeded rows and the wallet totals to write at the end."""

    def __init__(self):
        self.tasks: list[Task] = []
        self.wallets: dict[UUID, int] = {}  # user_id -> earned points

    def earn(self, user_id: UUID, points: int) -> None:
        self.wallets[user_id] = self.wallets.get(user_id, 0) + points


async def _task(
    session,
    world: World,
    teacher_id: UUID,
    *,
    title,
    description,
    rarity,
    points,
    duration_minutes,
    slots,
    keyword,
    allowed=("CSV",),
    schema=CSV_SCHEMA,
) -> Task:
    task = Task(
        owner_teacher_id=teacher_id,
        title=f"{MARKER}{title}",
        description=description,
        task_type=TaskType.DATA_CRAWL,
        rarity=rarity,
        base_reward_points=points,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=duration_minutes,
        submission_schema=schema,
        submission_schema_version=1 if schema is not None else None,
        allowed_file_types=list(allowed),
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
        published_at=datetime.now(UTC) - timedelta(days=2),
    )
    session.add(task)
    await session.flush()
    session.add_all(
        Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"{keyword}{index}",
            availability_status=AssignmentAvailability.AVAILABLE,
        )
        for index in range(1, slots + 1)
    )
    world.tasks.append(task)
    return task


async def _claim(
    session,
    task: Task,
    assignment_index: int,
    student: User,
    *,
    claimed_hours_ago: float = 6.0,
) -> AssignmentClaim:
    assignment = (
        await session.scalars(select(Assignment).where(Assignment.task_id == task.id))
    ).all()[assignment_index]
    claimed_at = datetime.now(UTC) - timedelta(hours=claimed_hours_ago)
    deadlines = compute_claim_deadlines(task, claimed_at)
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=student.id,
        status=ClaimStatus.CLAIMED,
        claimed_at=claimed_at,
        deadline_at=deadlines.deadline_at,
        grace_deadline_at=deadlines.grace_deadline_at,
        reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
        base_reward_points_snapshot=task.base_reward_points,
        submission_schema_version=task.submission_schema_version,
        reward_lock_status=RewardLockStatus.NONE,
    )
    assignment.availability_status = AssignmentAvailability.OCCUPIED
    session.add(claim)
    await session.flush()
    return claim


async def _submit(
    session,
    settings,
    claim: AssignmentClaim,
    *,
    hours_ago: float,
    validation_status: str,
    review_status: str,
    report: dict,
) -> Submission:
    """One submitted version + its validation row + a real MinIO object.

    Mirrors the upload/validation/lock writes: submission snapshot fields,
    a machine-validation row (same report on both the submission's
    denormalized column and the run row), and the PROVISIONAL reward lock
    at tier 100 (submitted well inside the deadline window)."""
    submitted_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    object_key = f"submissions/{claim.id}/{uuid4()}.csv"
    size = _put_demo_csv(settings, object_key)
    submission = Submission(
        claim_id=claim.id,
        version=1,
        object_key=object_key,
        original_filename="采集结果.csv",
        declared_type="CSV",
        file_size=size,
        submitted_at=submitted_at,
        validation_status=validation_status,
        review_status=review_status,
        validation_report=report,
        retention_until=submitted_at + timedelta(days=180),
    )
    session.add(submission)
    await session.flush()
    claim.latest_submission_id = submission.id
    session.add(
        SubmissionValidation(
            submission_id=submission.id,
            report=report,
            parser_version="demo-seed-1",
            started_at=submitted_at + timedelta(seconds=1),
            finished_at=submitted_at + timedelta(seconds=3),
            duration_ms=1800,
            status=validation_status,
        )
    )
    if validation_status == "VALIDATED":
        claim.status = ClaimStatus.UNDER_REVIEW
        claim.reward_lock_status = RewardLockStatus.PROVISIONAL
        claim.reward_tier_locked = 100
        claim.locked_reward_points = claim.base_reward_points_snapshot
        claim.reward_locked_at = submitted_at
        session.add(
            RewardLockHistory(
                claim_id=claim.id,
                submission_id=submission.id,
                lock_status_from=RewardLockStatus.NONE.value,
                lock_status_to=RewardLockStatus.PROVISIONAL.value,
                reward_tier_locked=100,
                locked_reward_points=claim.base_reward_points_snapshot,
                changed_by=claim.user_id,
                reason=_FIRST_LOCK_REASON,
            )
        )
    else:
        claim.status = ClaimStatus.REVISION_REQUIRED
        claim.reward_lock_status = RewardLockStatus.INVALIDATED
        session.add(
            RewardLockHistory(
                claim_id=claim.id,
                submission_id=submission.id,
                lock_status_from=RewardLockStatus.NONE.value,
                lock_status_to=RewardLockStatus.INVALIDATED.value,
                reward_tier_locked=None,
                locked_reward_points=None,
                changed_by=claim.user_id,
                reason=_INVALIDATE_REASON,
            )
        )
    await session.flush()
    return submission


async def _complete(
    session,
    world: World,
    teacher_id: UUID,
    claim: AssignmentClaim,
    submission: Submission,
    *,
    hours_ago: float,
) -> None:
    """The approve path's writes: review row, CONFIRMED lock, terminal
    claim/assignment, and the granted-points ledger entry."""
    now = datetime.now(UTC) - timedelta(hours=hours_ago)
    submission.review_status = "APPROVED"
    submission.reviewer_id = teacher_id
    submission.reviewed_at = now
    points = claim.locked_reward_points
    claim.status = ClaimStatus.COMPLETED
    claim.terminal_at = now
    claim.reward_lock_status = RewardLockStatus.CONFIRMED
    session.add(
        RewardLockHistory(
            claim_id=claim.id,
            submission_id=submission.id,
            lock_status_from=RewardLockStatus.PROVISIONAL.value,
            lock_status_to=RewardLockStatus.CONFIRMED.value,
            reward_tier_locked=claim.reward_tier_locked,
            locked_reward_points=points,
            changed_by=teacher_id,
            reason=_CONFIRM_LOCK_REASON,
        )
    )
    session.add(
        SubmissionReview(
            submission_id=submission.id,
            reviewer_id=teacher_id,
            action="APPROVE",
            note="数据完整规范，审核通过。",
        )
    )
    assignment = await session.get(Assignment, claim.assignment_id)
    assert assignment is not None
    assignment.availability_status = AssignmentAvailability.COMPLETED
    session.add(
        PointsLedger(
            user_id=claim.user_id,
            ledger_type="ASSIGNMENT_REWARD",
            amount=points,
            source_type="ASSIGNMENT_CLAIM",
            source_id=claim.id,
            affects_balance=True,
            affects_ranking=True,
            ranking_effective_at=now,
        )
    )
    world.earn(claim.user_id, points)


async def _notify(
    session,
    student: User,
    event_type: str,
    title: str,
    body: str,
    *,
    hours_ago: float,
    read: bool = False,
) -> None:
    created_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    notification = Notification(
        user_id=student.id,
        event_key=f"demo:{uuid4()}",
        event_type=event_type,
        title=title,
        body=body,
        read_at=created_at + timedelta(minutes=10) if read else None,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(notification)
    await session.flush()
    session.add(
        NotificationDelivery(
            notification_id=notification.id,
            user_id=student.id,
            event_key=notification.event_key,
            channel="SMS",
            status="SENT",
            scheduled_at=created_at,
            sent_at=created_at,
            created_at=created_at,
        )
    )


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    world = World()

    async with maker() as session:
        existing = await session.scalar(
            select(Task.id).where(Task.title.like(f"{MARKER}%")).limit(1)
        )
        if existing is not None:
            raise SystemExit(
                "demo content already seeded (tasks with "
                f"{MARKER} prefix exist) — nothing to do"
            )

        teacher = await session.scalar(
            select(User).where(User.email_normalized == "teacher@campus.example.edu")
        )
        admin = await session.scalar(
            select(User).where(User.email_normalized == "admin@campus.example.edu")
        )
        if teacher is None or admin is None:
            raise SystemExit(
                "staff demo accounts missing — run seed_demo_accounts.py first"
            )
        students = {
            username: await _student(session, username)
            for username in (
                "20250001",
                "20250002",
                "20250003",
                "20250004",
                "20250005",
                "20250006",
                "20250007",
                "20250008",
                "20250009",
                "20250010",
            )
        }

        # Term setting (audit-lite: the admin sets the current term).
        term = await session.get(SystemSetting, "CURRENT_ACADEMIC_TERM")
        if term is None:
            session.add(
                SystemSetting(
                    key="CURRENT_ACADEMIC_TERM",
                    value=TERM_KEY,
                    updated_by_user_id=admin.id,
                )
            )

        # ---- Task catalogue -------------------------------------------------
        t_observe = await _task(
            session,
            world,
            teacher.id,
            title="图书馆自习区学习行为观察记录",
            description="连续三天记录图书馆三楼自习区的座位使用与学习行为，"
            "按 CSV 模板提交观察数据。要求真实观察，禁止编造。",
            rarity=TaskRarity.NORMAL,
            points=60,
            duration_minutes=5 * 24 * 60,
            slots=6,
            keyword="自习观察",
            allowed=("CSV", "XLSX"),
        )
        t_canteen = await _task(
            session,
            world,
            teacher.id,
            title="校园食堂人气菜品评选数据采集",
            description="收集同学对两个食堂各窗口菜品的评价与推荐数据，"
            "每条记录需附来源链接。任务周期较短，请尽早提交。",
            rarity=TaskRarity.RARE,
            points=100,
            duration_minutes=3 * 24 * 60,
            slots=4,
            keyword="食堂菜品",
            allowed=("CSV", "XLSX"),
        )
        # The §10.1 document-family showcase: DOCX/PDF only, no schema.
        await _task(
            session,
            world,
            teacher.id,
            title="期末复习资料整理与共享协作",
            description="整理一门课程的期末复习要点并制作共享文档，"
            "审核标准高、周期长、奖励丰厚。优秀作品将入选"
            "院系资料库。提交 DOCX 或 PDF 文档。",
            rarity=TaskRarity.EPIC,
            points=180,
            duration_minutes=14 * 24 * 60,
            slots=2,
            keyword="复习资料",
            allowed=("DOCX", "PDF"),
            schema=None,
        )
        await _task(
            session,
            world,
            teacher.id,
            title="校园文创设计众筹数据采集",
            description="采集校园文创设计众筹活动的方案与预约数据，"
            "审核标准最高、周期充裕、名额唯一。数据将用于"
            "文创中心选品决策。",
            rarity=TaskRarity.LEGENDARY,
            points=260,
            duration_minutes=10 * 24 * 60,
            slots=1,
            keyword="文创众筹",
            allowed=("CSV", "XLSX"),
        )
        t_checkin = await _task(
            session,
            world,
            teacher.id,
            title="新生校园地标打卡数据采集",
            description="前往校园十个地标打卡拍照并记录位置信息，"
            "适合新生熟悉校园。名额充足，随时领取。",
            rarity=TaskRarity.NORMAL,
            points=40,
            duration_minutes=7 * 24 * 60,
            slots=8,
            keyword="地标打卡",
        )

        # ---- Lifecycle scenarios -------------------------------------------
        # Completed: 20250001 (60pt), 20250003 (60 + 100 = top rank).
        c = await _claim(
            session, t_observe, 0, students["20250001"], claimed_hours_ago=30
        )
        s = await _submit(
            session,
            settings,
            c,
            hours_ago=26,
            validation_status="VALIDATED",
            review_status="PENDING_REVIEW",
            report=_report(1),
        )
        await _complete(session, world, teacher.id, c, s, hours_ago=20)

        c = await _claim(
            session, t_observe, 1, students["20250003"], claimed_hours_ago=50
        )
        s = await _submit(
            session,
            settings,
            c,
            hours_ago=46,
            validation_status="VALIDATED",
            review_status="PENDING_REVIEW",
            report=_report(1),
        )
        await _complete(session, world, teacher.id, c, s, hours_ago=40)
        c = await _claim(
            session, t_canteen, 0, students["20250003"], claimed_hours_ago=28
        )
        s = await _submit(
            session,
            settings,
            c,
            hours_ago=24,
            validation_status="VALIDATED",
            review_status="PENDING_REVIEW",
            report=_report(1),
        )
        await _complete(session, world, teacher.id, c, s, hours_ago=18)

        # Completed + pending redemption: 20250002 (earned 100, reserved 30).
        c = await _claim(
            session, t_canteen, 1, students["20250002"], claimed_hours_ago=26
        )
        s = await _submit(
            session,
            settings,
            c,
            hours_ago=22,
            validation_status="VALIDATED",
            review_status="PENDING_REVIEW",
            report=_report(1),
        )
        await _complete(session, world, teacher.id, c, s, hours_ago=16)

        # Pending teacher review: 20250004 on the observation task.
        c = await _claim(
            session, t_observe, 2, students["20250004"], claimed_hours_ago=8
        )
        await _submit(
            session,
            settings,
            c,
            hours_ago=4,
            validation_status="VALIDATED",
            review_status="PENDING_REVIEW",
            report=_report(1),
        )

        # Validation failed (revision required): 20250005 on the canteen task.
        c = await _claim(
            session, t_canteen, 2, students["20250005"], claimed_hours_ago=10
        )
        await _submit(
            session,
            settings,
            c,
            hours_ago=6,
            validation_status="VALIDATION_FAILED",
            review_status="PENDING_REVIEW",
            report=_report(
                3,
                duplicates=2,
                errors=[
                    {
                        "code": "DUPLICATE_VALUE",
                        "message": "url 列存在重复值，唯一性校验未通过。",
                        "row": 2,
                        "column": "url",
                        "value": "https://example.com/campus/same-entry",
                    },
                    {
                        "code": "DUPLICATE_VALUE",
                        "message": "url 列存在重复值，唯一性校验未通过。",
                        "row": 3,
                        "column": "url",
                        "value": "https://example.com/campus/same-entry",
                    },
                ],
                preview=[
                    ["https://example.com/campus/demo-entry", "演示采集数据"],
                    ["https://example.com/campus/same-entry", "重复条目一"],
                    ["https://example.com/campus/same-entry", "重复条目二"],
                ],
            ),
        )

        # Fresh in-progress claim: 20250006 on the landmark task.
        await _claim(session, t_checkin, 0, students["20250006"], claimed_hours_ago=1)

        # Ranking depth: small bare rewards for 20250007-20250010.
        for index, username in enumerate(
            ("20250007", "20250008", "20250009", "20250010"), start=1
        ):
            student = students[username]
            amount = 30 - index * 5  # 25 / 20 / 15 / 10
            session.add(
                PointsLedger(
                    user_id=student.id,
                    ledger_type="ASSIGNMENT_REWARD",
                    amount=amount,
                    source_type="ASSIGNMENT_CLAIM",
                    source_id=uuid4(),
                    affects_balance=True,
                    affects_ranking=True,
                    ranking_effective_at=datetime.now(UTC) - timedelta(hours=5 * index),
                )
            )
            world.earn(student.id, amount)

        # ---- Reward catalogue + pending redemption -------------------------
        milk_tea = RewardItem(
            name=f"{MARKER}食堂奶茶券",
            description="校内合作饮品店任选一杯。",
            point_cost=30,
            stock=20,
            per_user_term_limit=None,
            available_from=None,
            available_until=None,
            enabled=True,
            requires_manual_review=False,
            fulfillment_instructions="凭学号到店核销。",
        )
        blind_box = RewardItem(
            name=f"{MARKER}校园文创盲盒",
            description="校徽系列文创随机一款。",
            point_cost=120,
            stock=5,
            per_user_term_limit=None,
            available_from=None,
            available_until=None,
            enabled=True,
            requires_manual_review=True,
            fulfillment_instructions="审核通过后一周内发放至宿舍信箱。",
        )
        seat = RewardItem(
            name=f"{MARKER}自习室黄金座预约权",
            description="下月图书馆三楼靠窗座位优先预约权。",
            point_cost=200,
            stock=3,
            per_user_term_limit=None,
            available_from=None,
            available_until=None,
            enabled=True,
            requires_manual_review=True,
            fulfillment_instructions="审核通过后由图书馆老师开通权限。",
        )
        session.add_all([milk_tea, blind_box, seat])
        await session.flush()

        redemption = RewardRedemption(
            user_id=students["20250002"].id,
            reward_item_id=milk_tea.id,
            status="REQUESTED",
            term_key=TERM_KEY,
            points=30,
            created_at=datetime.now(UTC) - timedelta(hours=3),
        )
        session.add(redemption)
        await session.flush()
        session.add(
            PointReservation(
                user_id=redemption.user_id,
                redemption_id=redemption.id,
                points=30,
                status="ACTIVE",
                created_at=redemption.created_at,
            )
        )

        # ---- Community on the observation task ------------------------------
        s1, s2, s3, s4, s5 = (
            students[key]
            for key in ("20250001", "20250002", "20250003", "20250004", "20250005")
        )
        root1 = Comment(
            task_id=t_observe.id,
            user_id=s1.id,
            parent_id=None,
            content="任务描述很清晰，CSV 模板的列要求一目了然，照着填就行。",
            is_anonymous=False,
        )
        root2 = Comment(
            task_id=t_observe.id,
            user_id=s3.id,
            parent_id=None,
            content="建议三天观察期排好时间表，最后一天赶工真的会来不及。",
            is_anonymous=False,
        )
        anon = Comment(
            task_id=t_observe.id,
            user_id=s5.id,
            parent_id=None,
            content="第一次做这类采集任务，流程比想象中顺畅，校验反馈也很快。",
            is_anonymous=True,
        )
        session.add_all([root1, root2, anon])
        await session.flush()
        reply = Comment(
            task_id=t_observe.id,
            user_id=s2.id,
            parent_id=root1.id,
            content="同感！提交之后机器校验几乎是秒过的。",
            is_anonymous=False,
        )
        session.add(reply)
        await session.flush()
        session.add_all(
            [
                CommentVote(comment_id=root1.id, user_id=s2.id, value=1),
                CommentVote(comment_id=root1.id, user_id=s3.id, value=1),
                CommentVote(comment_id=root2.id, user_id=s1.id, value=1),
                CommentReaction(comment_id=root1.id, user_id=s4.id, emoji="👍"),
                CommentReaction(comment_id=anon.id, user_id=s2.id, emoji="👍"),
                CommentReaction(comment_id=anon.id, user_id=s3.id, emoji="❤️"),
                TaskRating(task_id=t_observe.id, user_id=s1.id, rating=5),
                TaskRating(task_id=t_observe.id, user_id=s3.id, rating=4),
            ]
        )

        # ---- Notifications --------------------------------------------------
        await _notify(
            session,
            s1,
            NotificationEventType.SUBMISSION_APPROVED.value,
            "提交已通过审核",
            "「图书馆自习区学习行为观察记录」的提交已由老师审核通过，60 积分已到账。",
            hours_ago=20,
            read=True,
        )
        await _notify(
            session,
            s3,
            NotificationEventType.SUBMISSION_APPROVED.value,
            "提交已通过审核",
            "「校园食堂人气菜品评选数据采集」的提交已通过审核，100 积分已到账。",
            hours_ago=18,
            read=False,
        )
        await _notify(
            session,
            s4,
            NotificationEventType.ASSIGNMENT_DEADLINE_4H.value,
            "任务即将截止",
            "「图书馆自习区学习行为观察记录」距截止不足 4 小时，请尽快完成提交。",
            hours_ago=2,
            read=False,
        )
        await _notify(
            session,
            s5,
            NotificationEventType.SUBMISSION_VALIDATION_FAILED.value,
            "提交未通过机器校验",
            "检测到 2 条重复的 url 记录，请修正后重新提交。",
            hours_ago=6,
            read=False,
        )
        await _notify(
            session,
            s2,
            NotificationEventType.ACCOUNT_SECURITY.value,
            "兑换申请已提交",
            "「食堂奶茶券」兑换申请已提交，等待审核。30 积分已被冻结。",
            hours_ago=3,
            read=False,
        )

        # ---- Wallets ---------------------------------------------------------
        redemption_user = students["20250002"].id
        for user_id, earned in world.wallets.items():
            available = earned - 30 if user_id == redemption_user else earned
            session.add(
                PointWallet(
                    user_id=user_id,
                    available_points=available,
                    earned_points=earned,
                )
            )

        await session.commit()

    # Ranking projection: rebuild every board from the ledger (PG is the
    # source of truth; Redis holds exactly what the ledger derives).
    async with (
        maker() as session,
        aioredis.from_url(settings.redis_url, decode_responses=True) as redis,
    ):
        result = await RankingRedisProjection().rebuild_all(session, redis)
    await engine.dispose()

    print(
        f"seeded: {len(world.tasks)} tasks, wallets for "
        f"{len(world.wallets)} students, rewards x3, 1 pending redemption, "
        "1 pending review, 1 validation-failed, community + notifications"
    )
    print(f"ranking rebuild: {result}")


if __name__ == "__main__":
    asyncio.run(main())
