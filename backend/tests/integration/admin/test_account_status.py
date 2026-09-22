# backend/tests/integration/admin/test_account_status.py
"""Account-status governance against real PostgreSQL (Plan 08 T8).

Scenario map (the plan's steps, verbatim semantics):

- **Legal transitions (step 1):** ACTIVE->SUSPENDED, ACTIVE->BANNED,
  SUSPENDED->ACTIVE, and the explicit BANNED->ACTIVE unban, each with a
  reason; every other transition (including anything from
  PENDING_PHONE) is a typed 409 carrying from/to, changes nothing, and
  audits nothing. STUDENT/TEACHER actors are refused (Admin-only). A
  blank reason is a 400 before any read. An unknown user id is a typed
  404.
- **Same-transaction audit (step 3):** every committed transition
  leaves exactly one audit row — action, actor (id + role snapshot),
  before/after status snapshots (G11: status only), the free-text
  reason, and the AuditContext correlation pair.
- **History untouched (step 1):** a seeded ledger row, a seeded audit
  row, and a seeded claim ride through a full suspend -> reactivate ->
  ban -> unban cycle byte-identical (negative assertions).
- **Enforcement of already-issued tokens (step 2):** after suspend/ban,
  the Student's session is STILL live (enforcement is the per-request
  status re-check, not session revocation), and the service-layer gates
  refuse the actor: the claim flow's user-row-lock gate, the upload
  flow's locked-account gate, and the community write gates all raise
  their typed ACCOUNT_NOT_ACTIVE errors.

Harness notes: savepoint-wrapped ``db_session``; the services commit
inside it and the outer rollback keeps tests hermetic. Direct-insert
password stub (the claim-quota discipline): the registration service is
not exercised here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.models import AuditLog
from app.modules.community.gates import (
    CommenterAccountNotActiveError,
    require_community_writer,
    require_student_writer,
)
from app.modules.identity.account_admin_service import (
    AUDIT_USER_BANNED,
    AUDIT_USER_REACTIVATED,
    AUDIT_USER_SUSPENDED,
    AccountAdminService,
    AccountAdminTargetNotFoundError,
    InvalidAccountTransitionError,
)
from app.modules.identity.dependencies import get_access_token_codec
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.identity.repository import UserRepository
from app.modules.identity.session_service import SessionService
from app.modules.points.models import PointsLedger
from app.modules.submissions.upload_service import UploadService
from app.modules.tasks.claim_service import AccountNotActiveError, ClaimService
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

_NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
# Direct-insert password stub (argon2 hash of an unguessable test secret).
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
_REASON = "测试治理操作"
_AUDIT_CONTEXT = AuditContext(request_id="acct-test-0001", ip_address="203.0.113.9")


def _actor(role: Role) -> Actor:
    return Actor(user_id=uuid.uuid4(), role=role)


async def _seed_user(
    db: AsyncSession, *, role: Role = Role.STUDENT, status: UserStatus
) -> User:
    user = User(
        username=uuid.uuid4().hex,
        password_hash=_PASSWORD_HASH,
        nickname="治理测试用户",
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


def _service() -> AccountAdminService:
    return AccountAdminService()


async def _transition_audit_rows(db: AsyncSession, user: User) -> list[AuditLog]:
    return list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action.in_(
                    [AUDIT_USER_SUSPENDED, AUDIT_USER_BANNED, AUDIT_USER_REACTIVATED]
                ),
                AuditLog.target_id == str(user.id),
            )
        )
    )


# --- legal transitions and their audit (plan steps 1+3) -------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method_name", "action", "from_status", "to_status"),
    [
        (
            "suspend_user",
            AUDIT_USER_SUSPENDED,
            UserStatus.ACTIVE,
            UserStatus.SUSPENDED,
        ),
        ("ban_user", AUDIT_USER_BANNED, UserStatus.ACTIVE, UserStatus.BANNED),
        (
            "reactivate_user",
            AUDIT_USER_REACTIVATED,
            UserStatus.SUSPENDED,
            UserStatus.ACTIVE,
        ),
        (
            "reactivate_user",
            AUDIT_USER_REACTIVATED,
            UserStatus.BANNED,
            UserStatus.ACTIVE,
        ),
    ],
)
async def test_legal_transition_commits_and_audits_in_one_transaction(
    db_session: AsyncSession,
    method_name: str,
    action: str,
    from_status: UserStatus,
    to_status: UserStatus,
) -> None:
    admin = _actor(Role.ADMIN)
    user = await _seed_user(db_session, status=from_status)
    service = _service()

    result = await getattr(service, method_name)(
        db_session,
        admin,
        user.id,
        reason=_REASON,
        audit_context=_AUDIT_CONTEXT,
    )

    assert UserStatus(result.status) is to_status
    reloaded = await db_session.get(User, user.id)
    assert reloaded is not None and UserStatus(reloaded.status) is to_status

    audits = await _transition_audit_rows(db_session, user)
    assert len(audits) == 1
    row = audits[0]
    assert row.action == action
    assert row.target_type == "user"
    assert row.actor_user_id == admin.user_id
    assert row.actor_role == Role.ADMIN.value
    assert row.reason == _REASON
    assert row.before_snapshot == {"status": from_status.value}
    assert row.after_snapshot == {"status": to_status.value}
    assert row.request_id == _AUDIT_CONTEXT.request_id
    assert row.ip_address == _AUDIT_CONTEXT.ip_address


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method_name", "from_status"),
    [
        ("suspend_user", UserStatus.SUSPENDED),
        ("suspend_user", UserStatus.BANNED),
        ("suspend_user", UserStatus.PENDING_PHONE),
        ("ban_user", UserStatus.SUSPENDED),
        ("ban_user", UserStatus.BANNED),
        ("ban_user", UserStatus.PENDING_PHONE),
        ("reactivate_user", UserStatus.ACTIVE),
        ("reactivate_user", UserStatus.PENDING_PHONE),
    ],
)
async def test_illegal_transition_is_a_typed_409_and_writes_nothing(
    db_session: AsyncSession, method_name: str, from_status: UserStatus
) -> None:
    user = await _seed_user(db_session, status=from_status)

    with pytest.raises(InvalidAccountTransitionError) as exc_info:
        await getattr(_service(), method_name)(
            db_session, _actor(Role.ADMIN), user.id, reason=_REASON
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["from"] == from_status.value
    assert exc_info.value.details["operation"] in {"suspend", "ban", "reactivate"}
    reloaded = await db_session.get(User, user.id)
    assert reloaded is not None and UserStatus(reloaded.status) is from_status
    assert await _transition_audit_rows(db_session, user) == []


@pytest.mark.integration
@pytest.mark.parametrize("reason", ["", "   "])
async def test_blank_reason_is_refused_before_any_read(
    db_session: AsyncSession, reason: str
) -> None:
    user = await _seed_user(db_session, status=UserStatus.ACTIVE)

    with pytest.raises(BusinessError) as exc_info:
        await _service().suspend_user(
            db_session, _actor(Role.ADMIN), user.id, reason=reason
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert await _transition_audit_rows(db_session, user) == []


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TEACHER])
async def test_non_admin_actor_is_refused(db_session: AsyncSession, role: Role) -> None:
    user = await _seed_user(db_session, status=UserStatus.ACTIVE)

    with pytest.raises(BusinessError) as exc_info:
        await _service().suspend_user(db_session, _actor(role), user.id, reason=_REASON)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED
    reloaded = await db_session.get(User, user.id)
    assert reloaded is not None and UserStatus(reloaded.status) is UserStatus.ACTIVE
    assert await _transition_audit_rows(db_session, user) == []


@pytest.mark.integration
async def test_unknown_user_is_a_typed_404(db_session: AsyncSession) -> None:
    with pytest.raises(AccountAdminTargetNotFoundError) as exc_info:
        await _service().suspend_user(
            db_session, _actor(Role.ADMIN), uuid.uuid4(), reason=_REASON
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# --- history untouched (plan step 1, negative assertions) -----------------------------


async def _seed_history(db: AsyncSession, user: User) -> dict[str, Any]:
    """One ledger row, one audit row, one claim: the frozen history the
    governance operations must leave byte-identical."""
    operator = User(
        username=uuid.uuid4().hex,
        password_hash=_PASSWORD_HASH,
        nickname="历史操作管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(operator)
    await db.flush()
    ledger = PointsLedger(
        user_id=user.id,
        ledger_type="ADMIN_ADJUSTMENT",
        amount=500,
        source_type="MANUAL_TEST",
        source_id=uuid.uuid4(),
        affects_balance=True,
        affects_ranking=False,
        operator_id=operator.id,
        reason="历史调分，不可被治理操作改写",
    )
    db.add(ledger)

    probe = AuditLog(
        actor_user_id=uuid.uuid4(),
        actor_role=Role.ADMIN.value,
        action="HISTORICAL_PROBE",
        target_type="comment",
        target_id=str(uuid.uuid4()),
        details={"kept": True},
    )
    db.add(probe)

    owner = User(
        username=uuid.uuid4().hex,
        password_hash=_PASSWORD_HASH,
        nickname="历史任务教师",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(owner)
    await db.flush()
    task = Task(
        owner_teacher_id=owner.id,
        title="历史任务",
        description="负断言测试用",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"columns": [{"name": "note", "type": "string"}]},
        submission_schema_version=2,
        allowed_file_types=["CSV"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
    )
    db.add(task)
    await db.flush()
    assignment = Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword="历史关键词",
        availability_status=AssignmentAvailability.OCCUPIED,
    )
    db.add(assignment)
    await db.flush()
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=user.id,
        status=ClaimStatus.COMPLETED,
        claimed_at=_NOW - timedelta(days=2),
        deadline_at=_NOW - timedelta(days=1),
        grace_deadline_at=_NOW,
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=2,
        reward_lock_status=RewardLockStatus.NONE,
        terminal_at=_NOW - timedelta(hours=12),
    )
    db.add(claim)
    await db.flush()
    return {"ledger": ledger, "probe": probe, "claim": claim}


def _frozen_facts(facts: dict[str, Any]) -> dict[str, Any]:
    ledger, probe, claim = facts["ledger"], facts["probe"], facts["claim"]
    return {
        "ledger": (
            ledger.amount,
            ledger.reason,
            ledger.source_id,
            ledger.ledger_type,
            ledger.created_at,
        ),
        "probe": (probe.action, probe.target_id, probe.details, probe.created_at),
        "claim": (
            claim.status,
            claim.claimed_at,
            claim.deadline_at,
            claim.terminal_at,
            claim.base_reward_points_snapshot,
        ),
    }


@pytest.mark.integration
async def test_full_governance_cycle_leaves_history_untouched(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, status=UserStatus.ACTIVE)
    facts = await _seed_history(db_session, user)
    before = _frozen_facts(facts)

    service = _service()
    admin = _actor(Role.ADMIN)
    await service.suspend_user(db_session, admin, user.id, reason="暂停")
    await service.reactivate_user(db_session, admin, user.id, reason="恢复")
    await service.ban_user(db_session, admin, user.id, reason="封禁")
    await service.reactivate_user(db_session, admin, user.id, reason="解封")

    assert _frozen_facts(facts) == before
    reloaded = await db_session.get(User, user.id)
    assert reloaded is not None and UserStatus(reloaded.status) is UserStatus.ACTIVE
    # The cycle wrote its four governance rows and nothing else.
    assert len(await _transition_audit_rows(db_session, user)) == 4


# --- enforcement: already-issued tokens (plan step 2) --------------------------------


class _NeverStorage:
    """Duck-typed storage stub: the upload account gate refuses the call
    before any storage interaction, so this never runs."""


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method_name", "status"),
    [
        ("suspend_user", UserStatus.SUSPENDED),
        ("ban_user", UserStatus.BANNED),
    ],
)
async def test_non_active_account_with_live_token_is_refused_by_service_gates(
    db_session: AsyncSession, method_name: str, status: UserStatus
) -> None:
    """Plan step 2: the access token stays valid (sessions are NOT
    revoked — enforcement is the per-request status re-check), and every
    state-changing student surface refuses the actor at its
    service-layer gate: claim (user-row-lock gate), upload (locked
    account gate), community write gates."""
    user = await _seed_user(db_session, status=UserStatus.ACTIVE)
    session_row, _tokens = await SessionService(
        clock=FrozenClock(_NOW), access_codec=get_access_token_codec()
    ).issue_session(db_session, user=user, now=_NOW)
    await getattr(_service(), method_name)(
        db_session, _actor(Role.ADMIN), user.id, reason=_REASON
    )

    # The token's session is still live: the block is the status gate,
    # not revocation.
    live = await UserRepository().find_with_live_session(
        db_session,
        user_id=user.id,
        session_id=session_row.id,
        now=_NOW + timedelta(minutes=1),
    )
    assert live is not None and UserStatus(live.status) is status

    with pytest.raises(AccountNotActiveError):
        await ClaimService(clock=FrozenClock(_NOW)).claim_random_assignment(
            db_session, user.id, uuid.uuid4()
        )
    with pytest.raises(AccountNotActiveError):
        await UploadService(
            clock=FrozenClock(_NOW), storage=_NeverStorage()
        ).create_upload_intent(
            db_session,
            Actor(user_id=user.id, role=Role.STUDENT),
            uuid.uuid4(),
            "data.csv",
            "CSV",
            128,
        )
    with pytest.raises(CommenterAccountNotActiveError):
        await require_community_writer(db_session, user.id)
    with pytest.raises(CommenterAccountNotActiveError):
        await require_student_writer(db_session, user.id)
