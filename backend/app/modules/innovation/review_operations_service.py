"""Assigned operator review: current authority, frozen content, one transaction."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.errors import ProviderError
from app.integrations.evidence_scanner import MAX_EVIDENCE_BYTES
from app.integrations.object_storage import ObjectStorage, StoredObject
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.evidence_models import AchievementEvidence
from app.modules.innovation.evidence_service import _conflict
from app.modules.innovation.models import (
    AchievementDraft,
    OperationsGrant,
    OwnerQualification,
    ProjectDraft,
)
from app.modules.innovation.operations_service import _USERS
from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementRevision,
    AchievementWorkflow,
    ReviewConflict,
    RevisionEvidence,
)
from app.modules.innovation.review_schemas import (
    ReviewCaseSummary,
    ReviewDecisionCommand,
    ReviewPrivateDetail,
    ReviewQueueItem,
    ReviewQueueResponse,
    ReviewVersionCommand,
)
from app.modules.innovation.review_service import case_summary
from app.modules.notifications.enums import NotificationEventType
from app.modules.notifications.port import NotificationPort


def _forbidden() -> BusinessError:
    return BusinessError(
        ErrorCode.PERMISSION_DENIED, "当前账号无权审核或读取该条目", status_code=403
    )


def _missing() -> BusinessError:
    return BusinessError(ErrorCode.NOT_FOUND, "核实条目不存在", status_code=404)


@dataclass(frozen=True)
class ReviewProjectScope:
    id: UUID
    owner_user_id: UUID


class ReviewOperationsService:
    def __init__(
        self,
        *,
        clock: Clock,
        storage: ObjectStorage,
        audit: AuditLogWriter | None = None,
        notifications: NotificationPort | None = None,
    ) -> None:
        self._clock, self._storage = clock, storage
        self._audit = audit if audit is not None else AuditLogWriter()
        self._notifications = (
            notifications
            if notifications is not None
            else NotificationPort(clock=clock)
        )

    async def _authority(
        self,
        db: AsyncSession,
        actor: Actor,
        owner_id: UUID | None = None,
        *,
        assignee_id: UUID | None = None,
    ) -> None:
        ids = {actor.user_id} if owner_id is None else {actor.user_id, owner_id}
        if assignee_id is not None:
            ids.add(assignee_id)
        accounts = {
            row.id: row
            for row in (
                await db.execute(
                    select(_USERS)
                    .where(_USERS.c.id.in_(ids))
                    .order_by(_USERS.c.id)
                    .with_for_update()
                )
            ).all()
        }
        account = accounts.get(actor.user_id)
        if (
            actor.role != Role.STUDENT
            or account is None
            or account.role != "STUDENT"
            or account.status != "ACTIVE"
        ):
            raise _forbidden()
        grant = await db.scalar(
            select(OperationsGrant.enabled)
            .where(OperationsGrant.user_id == actor.user_id)
            .with_for_update()
        )
        if grant is not True:
            raise _forbidden()
        if owner_id is not None:
            if owner_id == actor.user_id:
                raise _forbidden()
            owner = accounts.get(owner_id)
            if owner is None or owner.role != "STUDENT" or owner.status != "ACTIVE":
                raise _conflict("负责人账号当前不可用，暂停核实")
            status = await db.scalar(
                select(OwnerQualification.status)
                .where(OwnerQualification.user_id == owner_id)
                .with_for_update()
            )
            if status != "APPROVED":
                raise _conflict("负责人资格当前不可用，暂停核实")

    async def _scope(
        self,
        db: AsyncSession,
        actor: Actor,
        case_id: UUID,
        *,
        allow_conflict: bool = False,
        lock_assignee: bool = False,
    ) -> tuple[AchievementReviewCase, AchievementWorkflow, ReviewProjectScope]:
        scope = (
            await db.execute(
                select(
                    ProjectDraft.id.label("project_id"),
                    ProjectDraft.owner_user_id,
                    AchievementDraft.id.label("achievement_id"),
                    AchievementReviewCase.assigned_user_id,
                )
                .join(AchievementDraft, AchievementDraft.project_id == ProjectDraft.id)
                .join(
                    AchievementReviewCase,
                    AchievementReviewCase.achievement_id == AchievementDraft.id,
                )
                .where(AchievementReviewCase.id == case_id)
            )
        ).one_or_none()
        if scope is None:
            # Validate operator authority even when the target doesn't exist.
            await self._authority(db, actor)
            raise _missing()
        await self._authority(
            db,
            actor,
            scope.owner_user_id,
            assignee_id=scope.assigned_user_id if lock_assignee else None,
        )
        project = (
            await db.execute(
                select(ProjectDraft.id, ProjectDraft.owner_user_id)
                .where(ProjectDraft.id == scope.project_id)
                .with_for_update()
            )
        ).one_or_none()
        if project is None or project.owner_user_id != scope.owner_user_id:
            raise _conflict("项目关系已变更，请刷新")
        achievement = await db.scalar(
            select(AchievementDraft.id)
            .where(
                AchievementDraft.id == scope.achievement_id,
                AchievementDraft.project_id == project.id,
            )
            .with_for_update()
        )
        if achievement is None:
            raise _missing()
        workflow = await db.scalar(
            select(AchievementWorkflow)
            .where(AchievementWorkflow.achievement_id == achievement)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        case = await db.scalar(
            select(AchievementReviewCase)
            .where(
                AchievementReviewCase.id == case_id,
                AchievementReviewCase.achievement_id == achievement,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if workflow is None or case is None:
            raise _missing()
        if lock_assignee and case.assigned_user_id != scope.assigned_user_id:
            raise _conflict("领取关系已改变，请刷新队列")
        conflict = await db.scalar(
            select(ReviewConflict.user_id).where(
                ReviewConflict.project_id == project.id,
                ReviewConflict.user_id == actor.user_id,
            )
        )
        if conflict is not None and not allow_conflict:
            raise _forbidden()
        return case, workflow, ReviewProjectScope(project.id, project.owner_user_id)

    @staticmethod
    def _valid_assignee() -> Exists:
        """One eligibility predicate for queue visibility and locked reclaim.

        The enclosing SELECT supplies the case and project. Reclaim locks the
        previous assignee's account before reading this predicate, serializing
        it with account/grant changes; project locks serialize conflict changes.
        """
        assignee = _USERS.alias("assigned_review_operator")
        conflict = (
            select(ReviewConflict.user_id)
            .where(
                ReviewConflict.user_id == assignee.c.id,
                ReviewConflict.project_id == ProjectDraft.id,
            )
            .correlate(assignee, ProjectDraft)
            .exists()
        )
        return (
            select(assignee.c.id)
            .join(OperationsGrant, OperationsGrant.user_id == assignee.c.id)
            .where(
                assignee.c.id == AchievementReviewCase.assigned_user_id,
                assignee.c.role == "STUDENT",
                assignee.c.status == "ACTIVE",
                OperationsGrant.enabled.is_(True),
                ~conflict,
            )
            .correlate(AchievementReviewCase, ProjectDraft)
            .exists()
        )

    async def _record(
        self,
        db: AsyncSession,
        actor: Actor,
        target_id: UUID,
        action: str,
        context: AuditContext | None,
        version: int | None = None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type="ie_review_case",
            target_id=str(target_id),
            after_snapshot={"version": version} if version is not None else None,
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )

    async def list_queue(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        limit: int,
        offset: int,
        context: AuditContext | None = None,
    ) -> ReviewQueueResponse:
        await self._authority(db, actor)
        owner = _USERS.alias("review_owner")
        query = select(
            AchievementReviewCase.id,
            AchievementReviewCase.revision_id,
            AchievementReviewCase.status,
            AchievementReviewCase.version,
            AchievementReviewCase.submitted_at,
            AchievementReviewCase.assigned_user_id,
            AchievementRevision.project_content["title"].astext.label("project_title"),
            AchievementRevision.achievement_content["title"].astext.label(
                "achievement_title"
            ),
        )
        query = query.join(
            AchievementRevision,
            AchievementReviewCase.revision_id == AchievementRevision.id,
        )
        query = query.join(
            AchievementDraft,
            AchievementReviewCase.achievement_id == AchievementDraft.id,
        )
        query = query.join(
            ProjectDraft, AchievementDraft.project_id == ProjectDraft.id
        ).join(owner, owner.c.id == ProjectDraft.owner_user_id)
        query = query.where(
            AchievementReviewCase.status == "SUBMITTED",
            ProjectDraft.owner_user_id != actor.user_id,
            owner.c.status == "ACTIVE",
            owner.c.role == "STUDENT",
            or_(
                AchievementReviewCase.assigned_user_id.is_(None),
                AchievementReviewCase.assigned_user_id == actor.user_id,
                ~self._valid_assignee(),
            ),
            ~select(ReviewConflict.user_id)
            .where(
                ReviewConflict.project_id == ProjectDraft.id,
                ReviewConflict.user_id == actor.user_id,
            )
            .exists(),
        )
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await db.execute(
                query.order_by(
                    AchievementReviewCase.submitted_at, AchievementReviewCase.id
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
        result = ReviewQueueResponse(
            items=[
                ReviewQueueItem(
                    id=row.id,
                    revision_id=row.revision_id,
                    status=row.status,
                    version=row.version,
                    reason=None,
                    project_title=row.project_title,
                    achievement_title=row.achievement_title,
                    submitted_at=row.submitted_at,
                    claimed_by_me=row.assigned_user_id == actor.user_id,
                )
                for row in rows
            ],
            total=total or 0,
            limit=limit,
            offset=offset,
        )
        await self._record(db, actor, actor.user_id, "IE_REVIEW_QUEUE_READ", context)
        await db.commit()
        return result

    @staticmethod
    def _assigned(case: AchievementReviewCase, actor: Actor) -> None:
        if case.status != "SUBMITTED":
            raise _conflict("该核实条目已关闭，请刷新队列")
        if case.assigned_user_id != actor.user_id:
            raise _forbidden()

    async def claim(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        case_id: UUID,
        payload: ReviewVersionCommand,
        context: AuditContext | None = None,
    ) -> ReviewCaseSummary:
        case, _, _ = await self._scope(db, actor, case_id, lock_assignee=True)
        action = "IE_REVIEW_CLAIM"
        if case.status != "SUBMITTED":
            raise _conflict("该核实条目已关闭")
        if case.assigned_user_id == actor.user_id and payload.version in (
            case.version,
            case.version - 1,
        ):
            pass
        elif case.version != payload.version:
            raise _conflict("该条目已被领取或更新，请刷新队列")
        else:
            if case.assigned_user_id is not None:
                eligible = await db.scalar(
                    select(self._valid_assignee())
                    .select_from(AchievementReviewCase)
                    .join(
                        AchievementDraft,
                        AchievementDraft.id == AchievementReviewCase.achievement_id,
                    )
                    .join(ProjectDraft, ProjectDraft.id == AchievementDraft.project_id)
                    .where(AchievementReviewCase.id == case.id)
                )
                if eligible:
                    raise _conflict("该条目已被领取或更新，请刷新队列")
                action = "IE_REVIEW_CLAIM_RECOVER"
            case.assigned_user_id = actor.user_id
            case.version += 1
        result = case_summary(case)
        await self._record(db, actor, case.id, action, context, case.version)
        await db.commit()
        return result

    async def declare_conflict(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        case_id: UUID,
        payload: ReviewVersionCommand,
        context: AuditContext | None = None,
    ) -> None:
        case, _, project = await self._scope(db, actor, case_id, allow_conflict=True)
        if case.status != "SUBMITTED" or case.version != payload.version:
            raise _conflict("核实条目已变更，请刷新后声明回避")
        await db.execute(
            pg_insert(ReviewConflict)
            .values(
                project_id=project.id,
                user_id=actor.user_id,
                created_at=self._clock.now(),
            )
            .on_conflict_do_nothing()
        )
        # _scope already holds the project lock, shared by all case mutations.
        # Project-level conflict must release every pending claim in that scope,
        # while preserving decided history and assignments in other projects.
        assigned = (
            await db.scalars(
                select(AchievementReviewCase)
                .join(
                    AchievementDraft,
                    AchievementDraft.id == AchievementReviewCase.achievement_id,
                )
                .where(
                    AchievementDraft.project_id == project.id,
                    AchievementReviewCase.status == "SUBMITTED",
                    AchievementReviewCase.assigned_user_id == actor.user_id,
                )
                .order_by(AchievementReviewCase.id)
                .with_for_update(of=AchievementReviewCase)
                .execution_options(populate_existing=True)
            )
        ).all()
        for assigned_case in assigned:
            assigned_case.assigned_user_id = None
            assigned_case.version += 1
            await self._record(
                db,
                actor,
                assigned_case.id,
                "IE_REVIEW_CONFLICT_RELEASE",
                context,
                assigned_case.version,
            )
        await self._record(
            db, actor, case.id, "IE_REVIEW_CONFLICT_DECLARE", context, case.version
        )
        await db.commit()

    async def detail(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        case_id: UUID,
        context: AuditContext | None = None,
    ) -> ReviewPrivateDetail:
        case, _, _ = await self._scope(db, actor, case_id)
        self._assigned(case, actor)
        await self._record(db, actor, case.id, "IE_REVIEW_SNAPSHOT_READ", context)
        await db.commit()  # durable access audit precedes even selecting PII
        case, _, _ = await self._scope(db, actor, case_id)
        self._assigned(case, actor)
        revision = await db.get(AchievementRevision, case.revision_id)
        if revision is None:
            raise RuntimeError("review revision missing")
        evidence = list(
            await db.scalars(
                select(RevisionEvidence.evidence_id)
                .where(RevisionEvidence.revision_id == revision.id)
                .order_by(RevisionEvidence.evidence_id)
            )
        )
        result = ReviewPrivateDetail(
            case=case_summary(case),
            project_content=revision.project_content,
            achievement_content=revision.achievement_content,
            owner_profile=revision.owner_profile,
            evidence=evidence,
        )
        await db.commit()
        return result

    async def decision(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        case_id: UUID,
        payload: ReviewDecisionCommand,
        context: AuditContext | None = None,
    ) -> ReviewCaseSummary:
        case, workflow, project = await self._scope(db, actor, case_id)
        fingerprint = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if case.assigned_user_id != actor.user_id:
            raise _forbidden()
        if case.decision_request_id == payload.request_id:
            if case.decision_payload_fingerprint != fingerprint:
                raise _conflict("这次决定请求已用于其他内容")
            result = case_summary(case)
            await self._record(db, actor, case.id, "IE_REVIEW_DECISION_REPLAY", context)
            await db.commit()
            return result
        if (
            case.status != "SUBMITTED"
            or workflow.first_review_state != "SUBMITTED"
            or case.version != payload.version
            or case.revision_id != payload.revision_id
        ):
            raise _conflict("核实条目或版本已变更，请刷新后决定")
        now = self._clock.now()
        case.status = payload.decision
        case.reason = payload.reason or None
        case.decided_at = now
        case.decision_request_id = payload.request_id
        case.decision_payload_fingerprint = fingerprint
        case.version += 1
        workflow.first_review_state = payload.decision
        workflow.version += 1
        if payload.decision == "APPROVED":
            workflow.public_revision_id = case.revision_id
            workflow.first_approved_at = now
            workflow.latest_update_at = now
        title = await db.scalar(
            select(AchievementRevision.achievement_content["title"].astext).where(
                AchievementRevision.id == case.revision_id
            )
        )
        if title is None:
            raise RuntimeError("review revision missing its title")
        event = (
            NotificationEventType.IE_ACHIEVEMENT_APPROVED
            if payload.decision == "APPROVED"
            else NotificationEventType.IE_ACHIEVEMENT_RETURNED
        )
        await self._notifications.record_event(
            db,
            event_key=f"ie_review:{case.id}:decision",
            event_type=event,
            user_id=project.owner_user_id,
            payload={"achievement_title": title, "review_reason": payload.reason},
        )
        await self._record(
            db, actor, case.id, "IE_REVIEW_DECISION", context, case.version
        )
        result = case_summary(case)
        await db.commit()
        return result

    async def read_content(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        case_id: UUID,
        evidence_id: UUID,
        context: AuditContext | None = None,
    ) -> StoredObject:
        case, _, _ = await self._scope(db, actor, case_id)
        self._assigned(case, actor)
        reference = await db.get(RevisionEvidence, (case.revision_id, evidence_id))
        material = await db.get(AchievementEvidence, evidence_id) if reference else None
        if (
            material is None
            or reference is None
            or material.state != "READY"
            or material.sha256 != reference.sha256
        ):
            raise _missing()
        key, digest, size, content_type, version = (
            material.object_key,
            reference.sha256,
            material.size,
            material.content_type,
            case.version,
        )
        await self._record(db, actor, case.id, "IE_REVIEW_EVIDENCE_READ", context)
        await db.commit()
        try:
            obj = await asyncio.to_thread(
                self._storage.read_bounded_object,
                object_key=key,
                max_bytes=MAX_EVIDENCE_BYTES,
            )
        except (ProviderError, OSError, ValueError) as exc:
            raise _conflict("材料暂不可读，请稍后重试") from exc
        if (
            len(obj.content) != size
            or obj.content_type != content_type
            or hashlib.sha256(obj.content).hexdigest() != digest
        ):
            raise _conflict("材料与审核版本不一致，禁止下载")
        case, _, _ = await self._scope(db, actor, case_id)
        self._assigned(case, actor)
        if case.version != version:
            raise _conflict("核实条目已变更，请刷新")
        await db.commit()
        return obj
