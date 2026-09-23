# backend/tests/integration/audit/test_repairs.py
"""Named state-repair commands against real PostgreSQL (Plan 08 T7).

Scenario map (the plan's steps, verbatim semantics):

- **Allowed repair (step 1):** an OCCUPIED Assignment whose newest
  claim is release-terminal (ABANDONED/EXPIRED), or that has no claim
  at all, is released to AVAILABLE with reason, before/after
  availability snapshots, and the backing claim state in details —
  one audit row, same transaction.
- **Forbidden destructive repairs (step 2):** the service's public
  surface is EXACTLY the two named commands (no delete-claim-history,
  rewrite-ledger-amount, delete-review-history, or execute-SQL method
  exists to call); forged non-UUID parameters are refused as a 400
  before any read; and seeded immutable history (ledger row, claim
  history, audit probe) rides through both repairs byte-identical
  (Review Focus 5).
- **Named commands, typed parameters (step 3):** no command accepts
  table/column/raw-SQL input — the typed UUID parameters are pinned by
  signature inspection, and a table-name string is a 400, never a
  mutation.

Refusals: non-OCCUPIED assignment, an ACTIVE claim backing the
occupancy, and the COMPLETED sticky family are typed 409s that write
nothing; a non-SENDING delivery is a typed 409; non-Admin actors are
403; blank reason is 400 before any read; unknown ids are typed 404.

Harness notes: savepoint-wrapped ``db_session`` (the services commit
inside it; the outer rollback keeps tests hermetic); direct-insert
seeding with the claim-quota password stub discipline.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.models import AuditLog
from app.modules.audit.repair_service import (
    AUDIT_STATE_REPAIR_FAILED_DELIVERY,
    AUDIT_STATE_REPAIR_RELEASED_ASSIGNMENT,
    FORCED_LAST_ERROR_PREFIX,
    AssignmentOccupancyNotDanglingError,
    DeliveryNotStuckSendingError,
    RepairService,
    RepairTargetNotFoundError,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.notifications.enums import DeliveryStatus, NotificationChannel
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.points.models import PointsLedger
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
_REASON = "修复悬挂的占用状态"
_AUDIT_CONTEXT = AuditContext(request_id="repair-test-0001", ip_address="203.0.113.9")

_REPAIR_ACTIONS = (
    AUDIT_STATE_REPAIR_RELEASED_ASSIGNMENT,
    AUDIT_STATE_REPAIR_FAILED_DELIVERY,
)


def _actor(role: Role = Role.ADMIN) -> Actor:
    return Actor(user_id=uuid.uuid4(), role=role)


def _service() -> RepairService:
    return RepairService(clock=FrozenClock(_NOW))


async def _teacher(db: AsyncSession) -> User:
    teacher = User(
        username=uuid.uuid4().hex,
        password_hash=_PASSWORD_HASH,
        nickname="修复测试教师",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(teacher)
    await db.flush()
    return teacher


def _task(db: AsyncSession, owner: User) -> Task:
    task = Task(
        owner_teacher_id=owner.id,
        title="修复测试任务",
        description="T7 窄修复命令测试",
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
        notification_channels=["IN_APP"],
    )
    db.add(task)
    return task


async def _assignment(
    db: AsyncSession, task: Task, *, availability: AssignmentAvailability
) -> Assignment:
    assignment = Assignment(
        task_id=task.id,
        platform="zhihu",
        keyword=f"kw-{uuid.uuid4().hex[:8]}",
        availability_status=availability.value,
    )
    db.add(assignment)
    await db.flush()
    return assignment


async def _claim(
    db: AsyncSession,
    assignment: Assignment,
    *,
    status: ClaimStatus,
    user_id: uuid.UUID,
    claimed_at: datetime = _NOW - timedelta(hours=3),
) -> AssignmentClaim:
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user_id,
        status=status.value,
        claimed_at=claimed_at,
        deadline_at=claimed_at + timedelta(hours=48),
        grace_deadline_at=claimed_at + timedelta(hours=72),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=2,
        reward_lock_status=RewardLockStatus.NONE,
        terminal_at=(
            _NOW - timedelta(hours=1)
            if status.value in ("COMPLETED", "ABANDONED", "EXPIRED")
            else None
        ),
    )
    db.add(claim)
    await db.flush()
    return claim


async def _student(db: AsyncSession) -> User:
    student = User(
        username=uuid.uuid4().hex,
        password_hash=_PASSWORD_HASH,
        nickname="修复测试学生",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add(student)
    await db.flush()
    return student


async def _delivery(
    db: AsyncSession, *, status: DeliveryStatus, attempts: int = 1
) -> NotificationDelivery:
    student = await _student(db)
    notification = Notification(
        user_id=student.id,
        event_key=f"submission:{student.id}:approved",
        event_type="SUBMISSION_APPROVED",
        title="任务审核通过",
        body="您的提交已通过审核。",
    )
    db.add(notification)
    await db.flush()
    delivery = NotificationDelivery(
        notification_id=notification.id,
        user_id=student.id,
        event_key=notification.event_key,
        channel=NotificationChannel.IN_APP.value,
        status=status.value,
        scheduled_at=_NOW - timedelta(minutes=30),
        attempts=attempts,
    )
    db.add(delivery)
    await db.flush()
    return delivery


async def _repair_audit_rows(db: AsyncSession, target_id: str) -> list[AuditLog]:
    return list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action.in_(_REPAIR_ACTIONS),
                AuditLog.target_id == target_id,
            )
        )
    )


async def _availability(db: AsyncSession, assignment: Assignment) -> str | None:
    return await db.scalar(
        select(Assignment.availability_status).where(Assignment.id == assignment.id)
    )


async def _delivery_state(
    db: AsyncSession, delivery: NotificationDelivery
) -> tuple[str, str | None]:
    row = (
        await db.execute(
            select(NotificationDelivery.status, NotificationDelivery.last_error).where(
                NotificationDelivery.id == delivery.id
            )
        )
    ).one()
    status, last_error = row
    return status, last_error


# --- allowed repair: release_occupied_assignment (plan step 1) ------------------------


@pytest.mark.integration
@pytest.mark.parametrize("terminal", [ClaimStatus.ABANDONED, ClaimStatus.EXPIRED])
async def test_release_returns_dangling_occupied_assignment_to_available(
    db_session: AsyncSession, terminal: ClaimStatus
) -> None:
    """The plan's example repair: OCCUPIED + terminal claim is the
    dangling shape; the release lands AVAILABLE with reason,
    before/after availability snapshots, and the claim state in
    details — one audit row."""
    teacher = await _teacher(db_session)
    task = _task(db_session, teacher)
    await db_session.flush()
    assignment = await _assignment(
        db_session, task, availability=AssignmentAvailability.OCCUPIED
    )
    student = await _student(db_session)
    claim = await _claim(db_session, assignment, status=terminal, user_id=student.id)
    actor = _actor()

    result = await _service().release_occupied_assignment(
        db_session, actor, assignment.id, reason=_REASON, audit_context=_AUDIT_CONTEXT
    )

    assert (
        AssignmentAvailability(result.availability_status)
        is AssignmentAvailability.AVAILABLE
    )
    assert (
        await _availability(db_session, assignment)
        == AssignmentAvailability.AVAILABLE.value
    )

    audits = await _repair_audit_rows(db_session, str(assignment.id))
    assert len(audits) == 1
    row = audits[0]
    assert row.action == AUDIT_STATE_REPAIR_RELEASED_ASSIGNMENT
    assert row.target_type == "assignment"
    assert row.actor_user_id == actor.user_id
    assert row.actor_role == Role.ADMIN.value
    assert row.reason == _REASON
    assert row.before_snapshot == {
        "availability_status": AssignmentAvailability.OCCUPIED.value
    }
    assert row.after_snapshot == {
        "availability_status": AssignmentAvailability.AVAILABLE.value
    }
    assert row.details["claim_id"] == str(claim.id)
    assert row.details["claim_status"] == terminal.value
    assert row.details["task_id"] == str(task.id)
    assert row.request_id == _AUDIT_CONTEXT.request_id
    assert row.ip_address == _AUDIT_CONTEXT.ip_address


@pytest.mark.integration
async def test_release_repairs_occupancy_with_no_claim_at_all(
    db_session: AsyncSession,
) -> None:
    """OCCUPIED with no backing claim is only producible by out-of-band
    surgery; restoring the invariant (AVAILABLE) is the repair, with
    claim facts absent in details."""
    teacher = await _teacher(db_session)
    task = _task(db_session, teacher)
    await db_session.flush()
    assignment = await _assignment(
        db_session, task, availability=AssignmentAvailability.OCCUPIED
    )

    await _service().release_occupied_assignment(
        db_session, _actor(), assignment.id, reason=_REASON
    )

    assert (
        await _availability(db_session, assignment)
        == AssignmentAvailability.AVAILABLE.value
    )
    row = (await _repair_audit_rows(db_session, str(assignment.id)))[0]
    assert row.details["claim_id"] is None
    assert row.details["claim_status"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("availability", "why"),
    [
        (AssignmentAvailability.AVAILABLE, "not_occupied"),
        (AssignmentAvailability.COMPLETED, "not_occupied"),
        (AssignmentAvailability.RETIRED, "not_occupied"),
    ],
)
async def test_release_refuses_non_occupied_assignment_with_a_typed_409(
    db_session: AsyncSession,
    availability: AssignmentAvailability,
    why: str,
) -> None:
    teacher = await _teacher(db_session)
    task = _task(db_session, teacher)
    await db_session.flush()
    assignment = await _assignment(db_session, task, availability=availability)

    with pytest.raises(AssignmentOccupancyNotDanglingError) as exc_info:
        await _service().release_occupied_assignment(
            db_session, _actor(), assignment.id, reason=_REASON
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["why"] == why
    assert exc_info.value.details["availability_status"] == availability.value
    assert await _availability(db_session, assignment) == availability.value
    assert await _repair_audit_rows(db_session, str(assignment.id)) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "active",
    [
        ClaimStatus.CLAIMED,
        ClaimStatus.VALIDATING,
        ClaimStatus.UNDER_REVIEW,
        ClaimStatus.REVISION_REQUIRED,
    ],
)
async def test_release_refuses_occupancy_backed_by_an_active_claim(
    db_session: AsyncSession, active: ClaimStatus
) -> None:
    """The legitimate state (a live claim holds the occupancy) is a
    typed 409 — even when older terminal claims exist beneath it."""
    teacher = await _teacher(db_session)
    task = _task(db_session, teacher)
    await db_session.flush()
    assignment = await _assignment(
        db_session, task, availability=AssignmentAvailability.OCCUPIED
    )
    student = await _student(db_session)
    await _claim(
        db_session,
        assignment,
        status=ClaimStatus.ABANDONED,
        user_id=student.id,
        claimed_at=_NOW - timedelta(hours=6),
    )
    live = await _claim(db_session, assignment, status=active, user_id=student.id)

    with pytest.raises(AssignmentOccupancyNotDanglingError) as exc_info:
        await _service().release_occupied_assignment(
            db_session, _actor(), assignment.id, reason=_REASON
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["why"] == "active_claim_exists"
    assert exc_info.value.details["claim_status"] == live.status
    assert (
        await _availability(db_session, assignment)
        == AssignmentAvailability.OCCUPIED.value
    )
    assert await _repair_audit_rows(db_session, str(assignment.id)) == []


@pytest.mark.integration
async def test_release_refuses_the_completed_sticky_family(
    db_session: AsyncSession,
) -> None:
    """A COMPLETED claim must not release the assignment to AVAILABLE
    (§8.2 stickiness); its correct repair is a different command."""
    teacher = await _teacher(db_session)
    task = _task(db_session, teacher)
    await db_session.flush()
    assignment = await _assignment(
        db_session, task, availability=AssignmentAvailability.OCCUPIED
    )
    student = await _student(db_session)
    await _claim(
        db_session, assignment, status=ClaimStatus.COMPLETED, user_id=student.id
    )

    with pytest.raises(AssignmentOccupancyNotDanglingError) as exc_info:
        await _service().release_occupied_assignment(
            db_session, _actor(), assignment.id, reason=_REASON
        )

    assert exc_info.value.details["why"] == "claim_status_not_release_terminal"
    assert exc_info.value.details["claim_status"] == ClaimStatus.COMPLETED.value
    assert (
        await _availability(db_session, assignment)
        == AssignmentAvailability.OCCUPIED.value
    )
    assert await _repair_audit_rows(db_session, str(assignment.id)) == []


@pytest.mark.integration
async def test_release_unknown_assignment_is_a_typed_404(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(RepairTargetNotFoundError) as exc_info:
        await _service().release_occupied_assignment(
            db_session, _actor(), uuid.uuid4(), reason=_REASON
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == ErrorCode.NOT_FOUND


# --- allowed repair: force_fail_delivery ---------------------------------------------


@pytest.mark.integration
async def test_force_fail_flips_sending_to_failed_and_audits(
    db_session: AsyncSession,
) -> None:
    delivery = await _delivery(db_session, status=DeliveryStatus.SENDING)
    actor = _actor()

    result = await _service().force_fail_delivery(
        db_session, actor, delivery.id, reason=_REASON, audit_context=_AUDIT_CONTEXT
    )

    assert DeliveryStatus(result.status) is DeliveryStatus.FAILED
    assert result.last_error == f"{FORCED_LAST_ERROR_PREFIX}{_REASON}"
    # The lease column takes the service clock (the injected frozen
    # instant), never the database default.
    assert result.updated_at == _NOW
    assert result.sent_at is None
    assert result.attempts == 1

    status, last_error = await _delivery_state(db_session, delivery)
    assert status == DeliveryStatus.FAILED.value
    assert last_error == f"{FORCED_LAST_ERROR_PREFIX}{_REASON}"

    audits = await _repair_audit_rows(db_session, str(delivery.id))
    assert len(audits) == 1
    row = audits[0]
    assert row.action == AUDIT_STATE_REPAIR_FAILED_DELIVERY
    assert row.target_type == "notification_delivery"
    assert row.actor_user_id == actor.user_id
    assert row.reason == _REASON
    assert row.before_snapshot == {"status": DeliveryStatus.SENDING.value}
    assert row.after_snapshot == {"status": DeliveryStatus.FAILED.value}
    assert row.details["channel"] == NotificationChannel.IN_APP.value
    assert row.details["last_error"] == f"{FORCED_LAST_ERROR_PREFIX}{_REASON}"
    assert row.request_id == _AUDIT_CONTEXT.request_id
    assert row.ip_address == _AUDIT_CONTEXT.ip_address


@pytest.mark.integration
@pytest.mark.parametrize(
    "status",
    [
        DeliveryStatus.PENDING,
        DeliveryStatus.RETRYABLE,
        DeliveryStatus.SENT,
        DeliveryStatus.FAILED,
    ],
)
async def test_force_fail_refuses_every_non_sending_state(
    db_session: AsyncSession, status: DeliveryStatus
) -> None:
    delivery = await _delivery(db_session, status=status)

    with pytest.raises(DeliveryNotStuckSendingError) as exc_info:
        await _service().force_fail_delivery(
            db_session, _actor(), delivery.id, reason=_REASON
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["current_status"] == status.value
    observed, _ = await _delivery_state(db_session, delivery)
    assert observed == status.value
    assert await _repair_audit_rows(db_session, str(delivery.id)) == []


@pytest.mark.integration
async def test_force_fail_unknown_delivery_is_a_typed_404(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(RepairTargetNotFoundError) as exc_info:
        await _service().force_fail_delivery(
            db_session, _actor(), uuid.uuid4(), reason=_REASON
        )
    assert exc_info.value.status_code == 404


# --- shared gates: role, reason, forged parameters ------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TEACHER])
@pytest.mark.parametrize("command", ["release", "force_fail"])
async def test_non_admin_actor_is_refused_for_both_commands(
    db_session: AsyncSession, role: Role, command: str
) -> None:
    delivery = await _delivery(db_session, status=DeliveryStatus.SENDING)
    with pytest.raises(BusinessError) as exc_info:
        if command == "release":
            await _service().release_occupied_assignment(
                db_session, _actor(role), uuid.uuid4(), reason=_REASON
            )
        else:
            await _service().force_fail_delivery(
                db_session, _actor(role), delivery.id, reason=_REASON
            )
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED
    assert await _repair_audit_rows(db_session, str(delivery.id)) == []


@pytest.mark.integration
@pytest.mark.parametrize("reason", ["", "   "])
@pytest.mark.parametrize("command", ["release", "force_fail"])
async def test_blank_reason_is_refused_before_any_read(
    db_session: AsyncSession, reason: str, command: str
) -> None:
    with pytest.raises(BusinessError) as exc_info:
        if command == "release":
            await _service().release_occupied_assignment(
                db_session, _actor(), uuid.uuid4(), reason=reason
            )
        else:
            await _service().force_fail_delivery(
                db_session, _actor(), uuid.uuid4(), reason=reason
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.integration
@pytest.mark.parametrize(
    "forged",
    [
        "assignments",
        "notification_deliveries; DROP TABLE audit_logs",
        "UPDATE points_ledger SET amount = 0",
        "*",
    ],
)
@pytest.mark.parametrize("command", ["release", "force_fail"])
async def test_forged_non_uuid_parameters_are_refused_not_executed(
    db_session: AsyncSession, forged: str, command: str
) -> None:
    """A table name, SQL fragment, or wildcard where the typed id
    belongs is a 400 before any read — the runtime half of "no command
    accepts table/column/raw SQL"."""
    with pytest.raises(BusinessError) as exc_info:
        if command == "release":
            await _service().release_occupied_assignment(
                db_session,
                _actor(),
                forged,
                reason=_REASON,  # type: ignore[arg-type]
            )
        else:
            await _service().force_fail_delivery(
                db_session,
                _actor(),
                forged,
                reason=_REASON,  # type: ignore[arg-type]
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


# --- forbidden destructive repairs (plan step 2) --------------------------------------


def test_service_public_surface_is_exactly_the_two_named_commands() -> None:
    """Structurally unreachable destruction (plan step 2): the service
    exposes exactly the two named repair commands — there is no third
    method to call, destructive or otherwise."""
    public = {
        name
        for name in dir(RepairService)
        if not name.startswith("_") and callable(getattr(RepairService, name, None))
    }
    assert public == {"release_occupied_assignment", "force_fail_delivery"}


def test_no_destructive_repair_method_exists() -> None:
    """The plan's named forbidden families — deleting completed Claim
    history, rewriting an existing Ledger amount, deleting Submission
    review history — have no method on the service to permit them."""
    service = RepairService()
    forbidden = (
        "delete_claim",
        "delete_claim_history",
        "delete_completed_claim",
        "rewrite_ledger",
        "update_ledger_amount",
        "set_ledger_amount",
        "delete_review",
        "delete_review_history",
        "delete_submission",
        "delete_audit_log",
        "truncate",
        "execute",
        "execute_sql",
        "raw_sql",
    )
    for name in forbidden:
        assert not hasattr(service, name), f"destructive surface: {name}"


def test_command_signatures_accept_ids_reason_and_context_only() -> None:
    """Typed parameters are the no-SQL proof (plan step 3): every
    parameter of both commands is a typed session/actor/UUID/string —
    no parameter can carry a table name, column name, or SQL fragment
    into a query."""
    for method in (
        RepairService.release_occupied_assignment,
        RepairService.force_fail_delivery,
    ):
        parameters = inspect.signature(method).parameters
        id_name = "assignment_id" if "release" in method.__name__ else "delivery_id"
        assert set(parameters) == {
            "self",
            "db",
            "actor",
            id_name,
            "reason",
            "audit_context",
        }
        annotation = parameters[id_name].annotation
        annotation_name = (
            annotation
            if isinstance(annotation, str)
            else getattr(annotation, "__name__", str(annotation))
        )
        assert "UUID" in annotation_name
        assert "sql" not in " ".join(parameters).lower()
        assert "table" not in " ".join(parameters).lower()


@pytest.mark.integration
async def test_both_repairs_leave_immutable_history_untouched(
    db_session: AsyncSession,
) -> None:
    """Review Focus 5, behaviorally: a seeded ledger row, completed
    claim history, and an audit probe ride through BOTH repairs
    byte-identical; no claim or ledger row is deleted or rewritten."""
    teacher = await _teacher(db_session)
    task = _task(db_session, teacher)
    await db_session.flush()
    frozen_assignment = await _assignment(
        db_session, task, availability=AssignmentAvailability.OCCUPIED
    )
    student = await _student(db_session)
    frozen_claim = await _claim(
        db_session, frozen_assignment, status=ClaimStatus.COMPLETED, user_id=student.id
    )
    operator = User(
        username=uuid.uuid4().hex,
        password_hash=_PASSWORD_HASH,
        nickname="历史操作管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db_session.add(operator)
    await db_session.flush()
    ledger = PointsLedger(
        user_id=student.id,
        ledger_type="ADMIN_ADJUSTMENT",
        amount=500,
        source_type="MANUAL_TEST",
        source_id=uuid.uuid4(),
        affects_balance=True,
        affects_ranking=False,
        operator_id=operator.id,
        reason="历史调分，不可被修复命令改写",
    )
    db_session.add(ledger)
    probe = AuditLog(
        actor_user_id=uuid.uuid4(),
        actor_role=Role.ADMIN.value,
        action="HISTORICAL_PROBE",
        target_type="comment",
        target_id=str(uuid.uuid4()),
        details={"kept": True},
    )
    db_session.add(probe)
    await db_session.flush()

    dangling = await _assignment(
        db_session, task, availability=AssignmentAvailability.OCCUPIED
    )
    other_student = await _student(db_session)
    await _claim(
        db_session, dangling, status=ClaimStatus.ABANDONED, user_id=other_student.id
    )
    delivery = await _delivery(db_session, status=DeliveryStatus.SENDING)
    before = {
        "claim": (
            frozen_claim.status,
            frozen_claim.claimed_at,
            frozen_claim.terminal_at,
            frozen_claim.base_reward_points_snapshot,
        ),
        "ledger": (ledger.amount, ledger.reason, ledger.source_id),
        "probe": (probe.action, probe.target_id, probe.details),
        "claim_count": await db_session.scalar(
            select(func.count()).select_from(AssignmentClaim)
        ),
        "ledger_count": await db_session.scalar(
            select(func.count()).select_from(PointsLedger)
        ),
    }

    service = _service()
    await service.release_occupied_assignment(
        db_session, _actor(), dangling.id, reason=_REASON
    )
    await service.force_fail_delivery(db_session, _actor(), delivery.id, reason=_REASON)

    assert before["claim"] == (
        frozen_claim.status,
        frozen_claim.claimed_at,
        frozen_claim.terminal_at,
        frozen_claim.base_reward_points_snapshot,
    )
    assert before["ledger"] == (ledger.amount, ledger.reason, ledger.source_id)
    assert before["probe"] == (probe.action, probe.target_id, probe.details)
    assert (
        await db_session.scalar(select(func.count()).select_from(AssignmentClaim))
        == before["claim_count"]
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(PointsLedger))
        == before["ledger_count"]
    )
