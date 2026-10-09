"""Owner workflow transitions with saved-version CAS and immutable references."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.innovation.achievement_service import _not_found
from app.modules.innovation.evidence_models import AchievementEvidence
from app.modules.innovation.evidence_service import _conflict
from app.modules.innovation.models import (
    AchievementDraft,
    OwnerProfile,
    OwnerQualification,
    ProjectDraft,
)
from app.modules.innovation.owner_service import _require_student
from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementRevision,
    AchievementWorkflow,
    RevisionEvidence,
)
from app.modules.innovation.review_schemas import (
    AchievementWorkflowResponse,
    ReviewCaseSummary,
    SavedRevisionCommand,
    WithdrawCommand,
)

PROJECT_FIELDS = ("title", "summary", "direction", "stage", "team_status")
ACHIEVEMENT_FIELDS = ("title", "description", "work_url", "award_text")
PROFILE_FIELDS = ("name", "student_no", "major", "grade")


def case_summary(row: AchievementReviewCase) -> ReviewCaseSummary:
    return ReviewCaseSummary(
        id=row.id,
        revision_id=row.revision_id,
        status=row.status,
        version=row.version,
        reason=row.reason,
    )


class AchievementReviewService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit if audit is not None else AuditLogWriter()

    async def _owned(
        self, db: AsyncSession, actor: Actor, project_id: UUID, achievement_id: UUID
    ) -> tuple[
        ProjectDraft,
        AchievementDraft,
        OwnerQualification | None,
        OwnerProfile | None,
        AchievementWorkflow,
    ]:
        await _require_student(db, actor)
        qualification = await db.scalar(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == actor.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        profile = await db.scalar(
            select(OwnerProfile)
            .where(OwnerProfile.user_id == actor.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        project = await db.scalar(
            select(ProjectDraft)
            .where(
                ProjectDraft.id == project_id,
                ProjectDraft.owner_user_id == actor.user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        achievement = (
            await db.scalar(
                select(AchievementDraft)
                .where(
                    AchievementDraft.id == achievement_id,
                    AchievementDraft.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if project
            else None
        )
        if project is None or achievement is None:
            raise _not_found()
        workflow = await db.scalar(
            select(AchievementWorkflow)
            .where(AchievementWorkflow.achievement_id == achievement_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if workflow is None:
            workflow = AchievementWorkflow(
                achievement_id=achievement_id,
                first_review_state="DRAFT",
                moderation_state="NORMAL",
                version=1,
            )
            db.add(workflow)
            await db.flush()
        return project, achievement, qualification, profile, workflow

    async def _response(
        self,
        db: AsyncSession,
        workflow: AchievementWorkflow,
        project: ProjectDraft,
        achievement: AchievementDraft,
    ) -> AchievementWorkflowResponse:
        case = await db.scalar(
            select(AchievementReviewCase)
            .join(
                AchievementRevision,
                AchievementReviewCase.revision_id == AchievementRevision.id,
            )
            .where(AchievementReviewCase.achievement_id == achievement.id)
            .order_by(AchievementRevision.number.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        revision = (
            await db.get(AchievementRevision, workflow.public_revision_id)
            if workflow.public_revision_id
            else None
        )
        return AchievementWorkflowResponse(
            achievement_id=achievement.id,
            first_review_state=workflow.first_review_state,
            moderation_state=workflow.moderation_state,
            version=workflow.version,
            review_case=case_summary(case) if case else None,
            public_revision_id=workflow.public_revision_id,
            first_approved_at=workflow.first_approved_at,
            latest_update_at=workflow.latest_update_at,
            has_unpublished_changes=revision is not None
            and (
                revision.project_version != project.version
                or revision.achievement_version != achievement.version
            ),
        )

    async def _record(
        self,
        db: AsyncSession,
        actor: Actor,
        workflow: AchievementWorkflow,
        action: str,
        context: AuditContext | None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type="ie_achievement_workflow",
            target_id=str(workflow.achievement_id),
            after_snapshot={
                "state": workflow.first_review_state,
                "version": workflow.version,
            },
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )

    async def workflow(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        context: AuditContext | None = None,
    ) -> AchievementWorkflowResponse:
        project, achievement, _, _, workflow = await self._owned(
            db, actor, project_id, achievement_id
        )
        result = await self._response(db, workflow, project, achievement)
        await self._record(db, actor, workflow, "IE_REVIEW_WORKFLOW_READ", context)
        await db.commit()
        return result

    async def _saved_revision(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        payload: SavedRevisionCommand,
        operation: str,
        context: AuditContext | None,
    ) -> AchievementWorkflowResponse:
        project, achievement, qualification, profile, workflow = await self._owned(
            db, actor, project_id, achievement_id
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": operation, **payload.model_dump(mode="json")},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing = await db.scalar(
            select(AchievementRevision).where(
                AchievementRevision.achievement_id == achievement_id,
                AchievementRevision.creation_request_id == payload.request_id,
            )
        )
        if existing:
            if existing.creation_payload_fingerprint != fingerprint:
                raise _conflict("这次请求已用于其他提交内容")
            result = await self._response(db, workflow, project, achievement)
            await self._record(db, actor, workflow, "IE_REVIEW_REQUEST_REPLAY", context)
            await db.commit()
            return result
        if qualification is None or qualification.status != "APPROVED":
            raise _conflict("请先申请并开通负责人资格")
        if profile is None or any(
            not getattr(profile, field).strip() for field in PROFILE_FIELDS
        ):
            raise _conflict("请先保存姓名、学号、专业、年级")
        if any(not getattr(project, field).strip() for field in PROJECT_FIELDS):
            raise _conflict("请先完善并保存项目五项概况")
        if not achievement.title.strip() or not achievement.description.strip():
            raise _conflict("请先保存成果名称和说明")
        if (
            workflow.version != payload.workflow_version
            or project.version != payload.project_version
            or achievement.version != payload.achievement_version
        ):
            raise _conflict("已保存版本发生变化，请刷新状态后重试")
        if operation == "SUBMIT" and workflow.first_review_state not in (
            "DRAFT",
            "RETURNED",
        ):
            raise _conflict("当前成果不能重复首次提交")
        if operation == "UPDATE" and workflow.first_review_state != "APPROVED":
            raise _conflict("首次核实通过后才能发布免复审更新")
        materials = list(
            await db.scalars(
                select(AchievementEvidence)
                .where(
                    AchievementEvidence.id.in_(payload.evidence_ids),
                    AchievementEvidence.achievement_id == achievement_id,
                    AchievementEvidence.state == "READY",
                    AchievementEvidence.removed_at.is_(None),
                )
                .order_by(AchievementEvidence.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(materials) != len(payload.evidence_ids):
            raise _conflict("所选证明不属于该成果或尚未通过检查")
        number = await db.scalar(
            select(func.max(AchievementRevision.number)).where(
                AchievementRevision.achievement_id == achievement_id
            )
        )
        now = self._clock.now()
        revision = AchievementRevision(
            achievement_id=achievement_id,
            number=(number or 0) + 1,
            creation_request_id=payload.request_id,
            creation_payload_fingerprint=fingerprint,
            operation=operation,
            project_version=project.version,
            achievement_version=achievement.version,
            project_content={
                field: getattr(project, field) for field in PROJECT_FIELDS
            },
            achievement_content={
                field: getattr(achievement, field) for field in ACHIEVEMENT_FIELDS
            },
            owner_profile={field: getattr(profile, field) for field in PROFILE_FIELDS},
            created_at=now,
        )
        db.add(revision)
        await db.flush()
        for material in materials:
            if material.sha256 is None:
                raise RuntimeError("READY material lost its digest")
            material.referenced = True
            db.add(
                RevisionEvidence(
                    revision_id=revision.id,
                    achievement_id=achievement_id,
                    evidence_id=material.id,
                    sha256=material.sha256,
                )
            )
        if operation == "SUBMIT":
            workflow.first_review_state = "SUBMITTED"
            db.add(
                AchievementReviewCase(
                    achievement_id=achievement_id,
                    revision_id=revision.id,
                    status="SUBMITTED",
                    version=1,
                    submitted_at=now,
                )
            )
        else:
            workflow.public_revision_id = revision.id
            workflow.latest_update_at = now
        workflow.version += 1
        await db.flush()
        result = await self._response(db, workflow, project, achievement)
        await self._record(
            db,
            actor,
            workflow,
            "IE_REVIEW_SUBMIT"
            if operation == "SUBMIT"
            else "IE_ACHIEVEMENT_PUBLISH_UPDATE",
            context,
        )
        await db.commit()
        return result

    async def submit(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        payload: SavedRevisionCommand,
        context: AuditContext | None = None,
    ) -> AchievementWorkflowResponse:
        return await self._saved_revision(
            db,
            actor=actor,
            project_id=project_id,
            achievement_id=achievement_id,
            payload=payload,
            operation="SUBMIT",
            context=context,
        )

    async def publish_update(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        payload: SavedRevisionCommand,
        context: AuditContext | None = None,
    ) -> AchievementWorkflowResponse:
        return await self._saved_revision(
            db,
            actor=actor,
            project_id=project_id,
            achievement_id=achievement_id,
            payload=payload,
            operation="UPDATE",
            context=context,
        )

    async def withdraw(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        payload: WithdrawCommand,
        context: AuditContext | None = None,
    ) -> AchievementWorkflowResponse:
        project, achievement, _, _, workflow = await self._owned(
            db, actor, project_id, achievement_id
        )
        case = await db.scalar(
            select(AchievementReviewCase)
            .where(
                AchievementReviewCase.id == payload.case_id,
                AchievementReviewCase.achievement_id == achievement_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if case is None:
            raise _not_found()
        if (
            workflow.first_review_state != "SUBMITTED"
            or workflow.version != payload.workflow_version
            or case.status != "SUBMITTED"
            or case.version != payload.case_version
        ):
            raise _conflict("核实状态已变更，请刷新后再操作")
        case.status = "WITHDRAWN"
        case.version += 1
        workflow.first_review_state = "DRAFT"
        workflow.version += 1
        await db.flush()
        result = await self._response(db, workflow, project, achievement)
        await self._record(db, actor, workflow, "IE_REVIEW_WITHDRAW", context)
        await db.commit()
        return result
