"""Fresh locked authorization after valid Bearer dependencies resolved an Actor."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    require_active_student_actor,
    require_admin_actor,
)
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import TotpCredential, User, UserSession
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import OwnerProfile, OwnerQualification
from app.modules.innovation.owner_schemas import OwnerProfileSave
from app.modules.innovation.owner_service import OwnerProfileService
from app.modules.innovation.qualification_schemas import (
    QualificationApply,
    QualificationApprove,
)
from app.modules.innovation.qualification_service import OwnerQualificationService

pytestmark = pytest.mark.integration
FIELDS = {
    "name": "锁后核验示例",
    "student_no": "00192345",
    "major": "示例专业",
    "grade": "2026级",
}


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


@dataclass(frozen=True)
class QualificationWorld:
    sessions: async_sessionmaker[AsyncSession]
    student: Actor
    admin: Actor


@pytest_asyncio.fixture
async def qualification_world(db_engine, clock) -> AsyncIterator[QualificationWorld]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    ids = []
    try:
        async with sessions() as seed:
            student, admin = [
                User(
                    username=f"qualification-recheck-{uuid4().hex}",
                    password_hash="unused",
                    nickname="资格核验测试",
                    role=role.value,
                    status="ACTIVE",
                )
                for role in (Role.STUDENT, Role.ADMIN)
            ]
            seed.add_all([student, admin])
            await seed.flush()
            ids = [student.id, admin.id]
            seed.add(
                TotpCredential(
                    user_id=admin.id,
                    secret_encrypted=b"unused",
                    confirmed_at=clock.now(),
                )
            )
            codec = get_access_token_codec()
            auth = SessionService(clock=clock, access_codec=codec)
            _, student_tokens = await auth.issue_session(
                seed, user=student, now=clock.now()
            )
            _, admin_tokens = await auth.issue_session(
                seed, user=admin, now=clock.now()
            )
            student_actor = await require_active_student_actor(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=student_tokens.access_token
                ),
                db=seed,
                codec=codec,
                clock=clock,
            )
            admin_actor = await require_admin_actor(
                credentials=HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=admin_tokens.access_token
                ),
                db=seed,
                codec=codec,
                clock=clock,
            )
            assert student_actor.role == Role.STUDENT
            assert admin_actor.role == Role.ADMIN
            await OwnerProfileService(clock=clock).save(
                seed,
                actor=student_actor,
                payload=OwnerProfileSave(**FIELDS, version=0),
            )
        yield QualificationWorld(sessions, student_actor, admin_actor)
    finally:
        # These committed multi-connection tests own only their random seed accounts.
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(OwnerQualification).where(OwnerQualification.user_id.in_(ids))
            )
            await cleanup.execute(
                delete(OwnerProfile).where(OwnerProfile.user_id.in_(ids))
            )
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.actor_user_id.in_(ids))
            )
            await cleanup.execute(
                delete(UserSession).where(UserSession.user_id.in_(ids))
            )
            await cleanup.execute(
                delete(TotpCredential).where(TotpCredential.user_id.in_(ids))
            )
            await cleanup.execute(delete(User).where(User.id.in_(ids)))
            await cleanup.commit()


async def create_pending(world: QualificationWorld, clock: FrozenClock) -> None:
    async with world.sessions() as db:
        result = await OwnerQualificationService(clock=clock).apply(
            db,
            actor=world.student,
            payload=QualificationApply(version=0, profile_version=1),
        )
        assert result.status == "PENDING" and result.version == 1


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"role": "TEACHER"}, "PERMISSION_DENIED"),
        ({"status": "SUSPENDED"}, "ACCOUNT_NOT_ACTIVE"),
    ],
)
async def test_student_service_rechecks_after_actor_resolution(
    qualification_world, clock, changes, expected_code
):
    world = qualification_world
    async with world.sessions() as mutation:
        await mutation.execute(
            update(User).where(User.id == world.student.user_id).values(**changes)
        )
        await mutation.commit()
    service = OwnerQualificationService(clock=clock)
    for action in ("read_owned", "apply"):
        async with world.sessions() as db:
            with pytest.raises(BusinessError) as refused:
                if action == "read_owned":
                    await service.read_owned(db, actor=world.student)
                else:
                    await service.apply(
                        db,
                        actor=world.student,
                        payload=QualificationApply(version=0, profile_version=1),
                    )
            assert refused.value.status_code == 403
            assert refused.value.code == expected_code
            await db.rollback()
    async with world.sessions() as check:
        assert await check.get(OwnerQualification, world.student.user_id) is None
        assert (
            await check.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.target_type == "ie_owner_qualification",
                    AuditLog.target_id == str(world.student.user_id),
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("role", "PERMISSION_DENIED"),
        ("status", "ACCOUNT_NOT_ACTIVE"),
        ("totp_confirmation", "TOTP_SETUP_REQUIRED"),
    ],
)
async def test_admin_service_rechecks_after_actor_resolution(
    qualification_world, clock, change, expected_code
):
    world = qualification_world
    await create_pending(world, clock)
    async with world.sessions() as mutation:
        if change == "totp_confirmation":
            statement = (
                update(TotpCredential)
                .where(TotpCredential.user_id == world.admin.user_id)
                .values(confirmed_at=None)
            )
        else:
            values = {"role": "STUDENT"} if change == "role" else {"status": "BANNED"}
            statement = (
                update(User).where(User.id == world.admin.user_id).values(**values)
            )
        await mutation.execute(statement)
        await mutation.commit()
    service = OwnerQualificationService(clock=clock)
    for action in ("queue", "reveal", "approve"):
        async with world.sessions() as db:
            with pytest.raises(BusinessError) as refused:
                if action == "queue":
                    await service.queue(db, actor=world.admin, limit=20, offset=0)
                elif action == "reveal":
                    await service.reveal(
                        db, actor=world.admin, user_id=world.student.user_id
                    )
                else:
                    await service.approve(
                        db,
                        actor=world.admin,
                        user_id=world.student.user_id,
                        payload=QualificationApprove(version=1),
                    )
            assert refused.value.status_code == 403
            assert refused.value.code == expected_code
            await db.rollback()
    async with world.sessions() as check:
        row = await check.get(OwnerQualification, world.student.user_id)
        assert row is not None and row.status == "PENDING" and row.version == 1
        assert row.approved_at is None and row.approved_by is None
        assert (
            await check.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.target_type == "ie_owner_qualification",
                    AuditLog.actor_user_id == world.admin.user_id,
                )
            )
            == 0
        )


@pytest.mark.parametrize("changes", [{"role": "TEACHER"}, {"status": "SUSPENDED"}])
async def test_admin_service_rechecks_changed_applicant(
    qualification_world, clock, changes
):
    world = qualification_world
    await create_pending(world, clock)
    async with world.sessions() as mutation:
        await mutation.execute(
            update(User).where(User.id == world.student.user_id).values(**changes)
        )
        await mutation.commit()
    service = OwnerQualificationService(clock=clock)
    for action in ("reveal", "approve"):
        async with world.sessions() as db:
            with pytest.raises(BusinessError) as refused:
                if action == "reveal":
                    await service.reveal(
                        db, actor=world.admin, user_id=world.student.user_id
                    )
                else:
                    await service.approve(
                        db,
                        actor=world.admin,
                        user_id=world.student.user_id,
                        payload=QualificationApprove(version=1),
                    )
            assert refused.value.status_code == 403
            assert refused.value.code == "PERMISSION_DENIED"
            await db.rollback()
    async with world.sessions() as check:
        row = await check.get(OwnerQualification, world.student.user_id)
        assert row is not None and row.status == "PENDING" and row.version == 1
        assert row.approved_at is None and row.approved_by is None
        assert (
            await check.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.target_type == "ie_owner_qualification",
                    AuditLog.actor_user_id == world.admin.user_id,
                )
            )
            == 0
        )


async def test_admin_cannot_reveal_or_approve_unsubmitted_saved_profile(
    qualification_world, clock
):
    world = qualification_world
    service = OwnerQualificationService(clock=clock)
    for action in ("reveal", "approve"):
        async with world.sessions() as db:
            with pytest.raises(BusinessError) as refused:
                if action == "reveal":
                    await service.reveal(
                        db, actor=world.admin, user_id=world.student.user_id
                    )
                else:
                    await service.approve(
                        db,
                        actor=world.admin,
                        user_id=world.student.user_id,
                        payload=QualificationApprove(version=1),
                    )
            assert refused.value.status_code == 404
            assert refused.value.code == "NOT_FOUND"
            await db.rollback()
    async with world.sessions() as check:
        assert await check.get(OwnerProfile, world.student.user_id) is not None
        assert await check.get(OwnerQualification, world.student.user_id) is None
        assert (
            await check.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.target_type == "ie_owner_qualification",
                    AuditLog.actor_user_id == world.admin.user_id,
                )
            )
            == 0
        )


async def test_pending_current_version_same_profile_resubmission_is_conflict(
    qualification_world, clock
):
    world = qualification_world
    await create_pending(world, clock)
    async with world.sessions() as db:
        with pytest.raises(BusinessError) as refused:
            await OwnerQualificationService(clock=clock).apply(
                db,
                actor=world.student,
                payload=QualificationApply(version=1, profile_version=1),
            )
        assert refused.value.status_code == 409
        assert refused.value.code == "CONFLICT"
        await db.rollback()
    async with world.sessions() as check:
        row = await check.get(OwnerQualification, world.student.user_id)
        assert row is not None and row.status == "PENDING" and row.version == 1
        assert row.profile_version == 1 and row.requested_at == clock.now()
        assert {key: getattr(row, key) for key in FIELDS} == FIELDS
        logs = list(
            await check.scalars(
                select(AuditLog).where(
                    AuditLog.target_type == "ie_owner_qualification",
                    AuditLog.target_id == str(world.student.user_id),
                )
            )
        )
        assert [log.action for log in logs] == ["IE_OWNER_QUALIFICATION_APPLY"]


async def test_waiting_approval_observes_applicant_suspension(
    qualification_world, clock
):
    world = qualification_world
    await create_pending(world, clock)
    started = asyncio.get_running_loop().create_future()
    task = None

    async def approve():
        async with world.sessions() as db:
            started.set_result(await db.scalar(text("select pg_backend_pid()")))
            try:
                return await OwnerQualificationService(clock=clock).approve(
                    db,
                    actor=world.admin,
                    user_id=world.student.user_id,
                    payload=QualificationApprove(version=1),
                )
            except BusinessError as exc:
                await db.rollback()
                return exc

    try:
        async with world.sessions() as suspension:
            await suspension.execute(
                update(User)
                .where(User.id == world.student.user_id)
                .values(status="SUSPENDED")
            )
            task = asyncio.create_task(approve())
            pid = await asyncio.wait_for(started, timeout=5)
            async with world.sessions() as monitor:
                async with asyncio.timeout(10):
                    while not await monitor.scalar(
                        text(
                            "select exists(select 1 from pg_stat_activity "
                            "where pid=:pid and wait_event_type='Lock')"
                        ),
                        {"pid": pid},
                    ):
                        await asyncio.sleep(0.02)
            assert not task.done()
            await suspension.commit()
        refused = await asyncio.wait_for(task, timeout=5)
        assert isinstance(refused, BusinessError)
        assert refused.status_code == 403 and refused.code == "PERMISSION_DENIED"
        async with world.sessions() as check:
            row = await check.get(OwnerQualification, world.student.user_id)
            assert row is not None and row.status == "PENDING" and row.version == 1
            assert row.approved_at is None and row.approved_by is None
            assert (
                await check.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.target_type == "ie_owner_qualification",
                        AuditLog.actor_user_id == world.admin.user_id,
                    )
                )
                == 0
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
