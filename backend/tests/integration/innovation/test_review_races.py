"""Committed independent PG connections: assignment, decision and read races."""

import asyncio
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.innovation.evidence_models import AchievementEvidence
from app.modules.innovation.evidence_schemas import EvidenceIntentCreate
from app.modules.innovation.evidence_service import EvidenceService
from app.modules.innovation.models import (
    AchievementDraft,
    OperationsGrant,
    OwnerProfile,
    OwnerQualification,
    ProjectDraft,
)
from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementRevision,
    AchievementWorkflow,
    ReviewConflict,
    RevisionEvidence,
)
from app.modules.innovation.review_operations_service import ReviewOperationsService
from app.modules.innovation.review_schemas import (
    ReviewDecisionCommand,
    ReviewVersionCommand,
    SavedRevisionCommand,
    WithdrawCommand,
)
from app.modules.innovation.review_service import AchievementReviewService
from app.modules.notifications.models import Notification, NotificationDelivery
from tests.integration.innovation.test_achievement_evidence import (
    PDF,
    Scanner,
    intent,
    world,
)
from tests.integration.innovation.test_achievement_evidence import clock as clock
from tests.integration.innovation.test_achievement_evidence import storage as storage
from tests.integration.innovation.test_achievement_review_operations import account

pytestmark = pytest.mark.integration


@pytest.fixture
async def committed(db_engine, clock, storage):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        owner, parent, achievement, _ = await world(seed, clock)
        uid, pid, aid = owner.id, parent.id, achievement.id
        seed.add(
            OwnerProfile(
                user_id=uid, name="姓名", student_no="001", major="专业", grade="2026"
            )
        )
        parent.summary, parent.direction, parent.stage, parent.team_status = (
            "介绍",
            "方向",
            "阶段",
            "团队",
        )
        achievement.description = "成果说明"
        op1, _ = await account(seed, clock, operator=True)
        op2, _ = await account(seed, clock, operator=True)
        op_ids = [op1.id, op2.id]
        owner_args = {
            "actor": Actor(user_id=uid, role=Role.STUDENT),
            "project_id": pid,
            "achievement_id": aid,
        }
        evidence = EvidenceService(clock=clock, storage=storage, scanner=Scanner())
        signed, _ = await evidence.create_intent(
            seed, **owner_args, payload=EvidenceIntentCreate(**intent())
        )
        storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
        await evidence.complete(seed, **owner_args, evidence_id=signed.evidence.id)
        service = AchievementReviewService(clock=clock)
        initial = await service.workflow(seed, **owner_args)
        submitted = await service.submit(
            seed,
            **owner_args,
            payload=SavedRevisionCommand(
                request_id=uuid4(),
                workflow_version=initial.version,
                project_version=parent.version,
                achievement_version=achievement.version,
                evidence_ids=[signed.evidence.id],
            ),
        )
        assert submitted.review_case is not None
    try:
        yield (
            sessions,
            owner_args,
            [Actor(user_id=oid, role=Role.STUDENT) for oid in op_ids],
            submitted,
            signed.evidence.id,
        )
    finally:
        async with sessions() as cleanup:
            for model, predicate in (
                (NotificationDelivery, NotificationDelivery.user_id == uid),
                (Notification, Notification.user_id == uid),
                (ReviewConflict, ReviewConflict.project_id == pid),
                (AchievementWorkflow, AchievementWorkflow.achievement_id == aid),
                (AchievementReviewCase, AchievementReviewCase.achievement_id == aid),
                (RevisionEvidence, RevisionEvidence.achievement_id == aid),
                (AchievementRevision, AchievementRevision.achievement_id == aid),
                (AchievementEvidence, AchievementEvidence.achievement_id == aid),
                (AchievementDraft, AchievementDraft.id == aid),
                (OperationsGrant, OperationsGrant.user_id.in_(op_ids)),
                (OwnerQualification, OwnerQualification.user_id == uid),
                (OwnerProfile, OwnerProfile.user_id == uid),
                (AuditLog, AuditLog.actor_user_id.in_([uid, *op_ids])),
                (ProjectDraft, ProjectDraft.id == pid),
                (UserSession, UserSession.user_id.in_([uid, *op_ids])),
                (User, User.id.in_([uid, *op_ids])),
            ):
                await cleanup.execute(delete(model).where(predicate))
            await cleanup.commit()


async def test_two_operators_only_one_claims(committed, clock, storage):
    sessions, _, operators, submitted, _ = committed
    service = ReviewOperationsService(clock=clock, storage=storage)
    case = submitted.review_case

    async def claim(actor):
        async with sessions() as db:
            pid = await db.scalar(text("SELECT pg_backend_pid()"))
            try:
                result = await service.claim(
                    db,
                    actor=actor,
                    case_id=case.id,
                    payload=ReviewVersionCommand(version=case.version),
                )
            except BusinessError as exc:
                result = exc
            return pid, result

    results = await asyncio.wait_for(
        asyncio.gather(*(claim(actor) for actor in operators)), 8
    )
    assert len({pid for pid, _ in results}) == 2
    assert sum(isinstance(result, BusinessError) for _, result in results) == 1


async def waiting(sessions, pid):
    async def poll():
        async with sessions() as observer:
            while True:
                # No time-based guess: observe a genuine PostgreSQL lock wait.
                if await observer.scalar(
                    text(
                        "SELECT wait_event_type='Lock' FROM pg_stat_activity "
                        "WHERE pid=:pid"
                    ),
                    {"pid": pid},
                ):
                    return
                await observer.rollback()
                await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize("first", ["approve", "withdraw"])
async def test_approval_and_withdrawal_have_one_committed_winner(
    committed, clock, storage, first
):
    sessions, owner_args, operators, submitted, _ = committed
    ops = ReviewOperationsService(clock=clock, storage=storage)
    owner_service = AchievementReviewService(clock=clock)
    case = submitted.review_case
    async with sessions() as assign:
        claimed = await ops.claim(
            assign,
            actor=operators[0],
            case_id=case.id,
            payload=ReviewVersionCommand(version=case.version),
        )
    decision = ReviewDecisionCommand(
        request_id=uuid4(),
        version=claimed.version,
        revision_id=case.revision_id,
        decision="APPROVED",
    )
    withdraw = WithdrawCommand(
        workflow_version=submitted.version,
        case_id=case.id,
        case_version=claimed.version,
    )
    async with sessions() as approve_db, sessions() as withdraw_db:
        pids = {
            "approve": await approve_db.scalar(text("SELECT pg_backend_pid()")),
            "withdraw": await withdraw_db.scalar(text("SELECT pg_backend_pid()")),
        }
        assert pids["approve"] != pids["withdraw"]

        async def run(which):
            try:
                if which == "approve":
                    return await ops.decision(
                        approve_db,
                        actor=operators[0],
                        case_id=case.id,
                        payload=decision,
                    )
                return await owner_service.withdraw(
                    withdraw_db, **owner_args, payload=withdraw
                )
            except BusinessError as exc:
                return exc

        tasks = []
        try:
            async with sessions() as blocker:
                await blocker.scalar(
                    select(User.id)
                    .where(User.id == owner_args["actor"].user_id)
                    .with_for_update()
                )
                tasks.append(asyncio.create_task(run(first)))
                await waiting(sessions, pids[first])
                second = "withdraw" if first == "approve" else "approve"
                tasks.append(asyncio.create_task(run(second)))
                await waiting(sessions, pids[second])
                await blocker.commit()
            outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 8)
            assert not isinstance(outcomes[0], BusinessError) and isinstance(
                outcomes[1], BusinessError
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with sessions() as check:
        row = await check.get(AchievementWorkflow, owner_args["achievement_id"])
        assert row.first_review_state == ("APPROVED" if first == "approve" else "DRAFT")
        count = await check.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == owner_args["actor"].user_id)
        )
        assert count == (1 if first == "approve" else 0)


async def test_read_rechecks_revocation_after_external_io(committed, clock, storage):
    sessions, _, operators, submitted, eid = committed
    service = ReviewOperationsService(clock=clock, storage=storage)
    case = submitted.review_case
    async with sessions() as assign:
        await service.claim(
            assign,
            actor=operators[0],
            case_id=case.id,
            payload=ReviewVersionCommand(version=case.version),
        )
    started, release = Event(), Event()
    original = storage.read_bounded_object

    def delayed(**kwargs):
        started.set()
        if not release.wait(10):
            raise TimeoutError("test read barrier timed out")
        return original(**kwargs)

    storage.read_bounded_object = delayed

    async def read():
        async with sessions() as db:
            return await service.read_content(
                db, actor=operators[0], case_id=case.id, evidence_id=eid
            )

    job = asyncio.create_task(read())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with sessions() as revoke:
            audit = await revoke.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "IE_REVIEW_EVIDENCE_READ",
                    AuditLog.target_id == str(case.id),
                )
            )
            assert audit == 1, "file access audit was not durably committed"
            await asyncio.wait_for(
                revoke.execute(
                    update(OperationsGrant)
                    .where(OperationsGrant.user_id == operators[0].user_id)
                    .values(enabled=False)
                ),
                3,
            )
            await revoke.commit()
        release.set()
        with pytest.raises(BusinessError) as error:
            await asyncio.wait_for(job, 5)
        assert error.value.status_code == 403
    finally:
        release.set()
        await asyncio.gather(job, return_exceptions=True)
        storage.read_bounded_object = original
