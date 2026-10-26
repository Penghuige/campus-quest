"""Owner lifecycle commands; user -> qualification -> project -> recruitment locks."""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.innovation.contact_schemas import ContactChannel
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from app.modules.innovation.owner_service import _require_student
from app.modules.innovation.recruitment_models import (
    RecruitmentActionReceipt,
    RecruitmentContactAuthorization,
    RecruitmentDraft,
    RecruitmentRevision,
)
from app.modules.innovation.recruitment_schemas import (
    RecruitmentActionResult,
    RecruitmentDraftCreate,
    RecruitmentDraftListResponse,
    RecruitmentDraftResponse,
    RecruitmentDraftUpdate,
    RecruitmentFields,
    RecruitmentStatus,
    RecruitmentVersionCommand,
    RevokeRecruitmentContactCommand,
)

REMINDER_PERIOD = timedelta(days=30)


def fingerprint(content: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def conflict(message: str, *, kind: str) -> BusinessError:
    return BusinessError(
        ErrorCode.CONFLICT, message, status_code=409, details={"kind": kind}
    )


def not_found() -> BusinessError:
    return BusinessError(ErrorCode.NOT_FOUND, "招募岗位不存在", status_code=404)


@asynccontextmanager
async def mutation(db: AsyncSession) -> AsyncIterator[None]:
    """Own commit/rollback, including audit failures, without swallowing errors."""
    try:
        yield
        await db.commit()
    except Exception:
        await db.rollback()
        raise


class RecruitmentService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self._clock = clock
        self._audit = audit or AuditLogWriter()

    async def lock_project(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        require_qualification: bool = False,
    ) -> ProjectDraft:
        await _require_student(db, actor)
        qualification = await db.scalar(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == actor.user_id)
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
        if project is None:
            raise not_found()
        if require_qualification and (
            qualification is None or qualification.status != "APPROVED"
        ):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                "发布招募须先开通负责人资格",
                status_code=403,
            )
        return project

    async def lock_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        require_qualification: bool = False,
    ) -> RecruitmentDraft:
        await self.lock_project(
            db,
            actor=actor,
            project_id=project_id,
            require_qualification=require_qualification,
        )
        row = await db.scalar(
            select(RecruitmentDraft)
            .where(
                RecruitmentDraft.id == recruitment_id,
                RecruitmentDraft.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise not_found()
        return row

    def logical_status(self, row: RecruitmentDraft) -> RecruitmentStatus:
        if (
            row.status == "OPEN"
            and row.deadline_at is not None
            and self._clock.now() >= row.deadline_at
        ):
            return "EXPIRED"
        return cast(RecruitmentStatus, row.status)

    async def response(
        self, db: AsyncSession, row: RecruitmentDraft
    ) -> RecruitmentDraftResponse:
        contacts = (
            list(
                await db.scalars(
                    select(RecruitmentContactAuthorization)
                    .where(
                        RecruitmentContactAuthorization.revision_id
                        == row.public_revision_id,
                        RecruitmentContactAuthorization.revoked_at.is_(None),
                    )
                    .order_by(RecruitmentContactAuthorization.channel)
                )
            )
            if row.public_revision_id is not None
            else []
        )
        revision = (
            await db.get(RecruitmentRevision, row.public_revision_id)
            if row.public_revision_id is not None
            else None
        )
        fields = RecruitmentFields.model_validate(row.fields)
        changed = (
            revision is None
            or revision.content != fields.model_dump(mode="json", exclude={"contacts"})
            or {c.channel: c.value for c in contacts}
            != {c.channel: c.value for c in fields.contacts}
        )
        return RecruitmentDraftResponse(
            id=row.id,
            project_id=row.project_id,
            fields=fields,
            version=row.version,
            status=self.logical_status(row),
            moderation_state=cast(Any, row.moderation_state),
            public_contact_channels=[cast(ContactChannel, c.channel) for c in contacts],
            has_unpublished_changes=changed,
            cycle_id=row.cycle_id,
            deadline_at=row.deadline_at,
            last_confirmed_at=row.last_confirmed_at,
            next_remind_at=row.next_remind_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def audit(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        row: RecruitmentDraft,
        action: str,
        context: AuditContext,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=f"ie.recruitment.{action}",
            target_type="ie_recruitment",
            target_id=str(row.id),
            details={
                "version": row.version,
                "status": row.status,
                "cycle_id": str(row.cycle_id) if row.cycle_id else None,
            },
            request_id=context.request_id,
            ip_address=context.ip_address,
        )

    async def create(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        payload: RecruitmentDraftCreate,
        context: AuditContext,
    ) -> tuple[RecruitmentDraftResponse, bool]:
        async with mutation(db):
            await self.lock_project(db, actor=actor, project_id=project_id)
            content = payload.fields.model_dump(mode="json")
            digest = fingerprint(content)
            row = await db.scalar(
                select(RecruitmentDraft).where(
                    RecruitmentDraft.project_id == project_id,
                    RecruitmentDraft.creation_request_id == payload.request_id,
                )
            )
            created = row is None
            if row is None:
                now = self._clock.now()
                row = RecruitmentDraft(
                    project_id=project_id,
                    creation_request_id=payload.request_id,
                    creation_fingerprint=digest,
                    fields=content,
                    version=1,
                    status="DRAFT",
                    moderation_state="NORMAL",
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                await db.flush()
                await self.audit(
                    db, actor=actor, row=row, action="create", context=context
                )
            elif row.creation_fingerprint != digest:
                raise conflict("此请求已用于不同内容", kind="REQUEST_CONFLICT")
            result = await self.response(db, row)
        return result, created

    async def get_owned(
        self, db: AsyncSession, *, actor: Actor, project_id: UUID, recruitment_id: UUID
    ) -> RecruitmentDraftResponse:
        async with mutation(db):
            row = await self.lock_owned(
                db, actor=actor, project_id=project_id, recruitment_id=recruitment_id
            )
            return await self.response(db, row)

    async def list_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        limit: int,
        offset: int,
    ) -> RecruitmentDraftListResponse:
        async with mutation(db):
            await self.lock_project(db, actor=actor, project_id=project_id)
            total = await db.scalar(
                select(func.count())
                .select_from(RecruitmentDraft)
                .where(RecruitmentDraft.project_id == project_id)
            )
            rows = list(
                await db.scalars(
                    select(RecruitmentDraft)
                    .where(RecruitmentDraft.project_id == project_id)
                    .order_by(
                        RecruitmentDraft.updated_at.desc(), RecruitmentDraft.id.desc()
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            items = [await self.response(db, row) for row in rows]
            return RecruitmentDraftListResponse(
                items=items, total=total or 0, limit=limit, offset=offset
            )

    async def _receipt(
        self,
        db: AsyncSession,
        *,
        row: RecruitmentDraft,
        action: str,
        payload: RecruitmentVersionCommand,
    ) -> tuple[RecruitmentActionReceipt | None, str]:
        digest = fingerprint({"action": action, **payload.model_dump(mode="json")})
        prior = await db.get(RecruitmentActionReceipt, (row.id, payload.request_id))
        if prior is not None:
            if prior.fingerprint != digest:
                raise conflict(
                    "同一请求不能改用其他动作或内容", kind="REQUEST_CONFLICT"
                )
            return prior, digest
        if payload.version != row.version:
            raise conflict("内容已变化，请重新读取后再操作", kind="VERSION_CONFLICT")
        return None, digest

    async def update(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RecruitmentDraftUpdate,
        context: AuditContext,
    ) -> RecruitmentDraftResponse:
        async with mutation(db):
            row = await self.lock_owned(
                db, actor=actor, project_id=project_id, recruitment_id=recruitment_id
            )
            receipt, digest = await self._receipt(
                db, row=row, action="update", payload=payload
            )
            if receipt is None:
                row.fields = payload.fields.model_dump(mode="json")
                await self._finish(
                    db,
                    row=row,
                    actor=actor,
                    action="update",
                    payload=payload,
                    digest=digest,
                    context=context,
                )
            return await self.response(db, row)

    async def _finish(
        self,
        db: AsyncSession,
        *,
        row: RecruitmentDraft,
        actor: Actor,
        action: str,
        payload: RecruitmentVersionCommand,
        digest: str,
        context: AuditContext,
    ) -> RecruitmentActionReceipt:
        row.version += 1
        row.updated_at = self._clock.now()
        receipt = RecruitmentActionReceipt(
            recruitment_id=row.id,
            request_id=payload.request_id,
            action=action,
            fingerprint=digest,
            applied_version=row.version,
            created_at=self._clock.now(),
        )
        db.add(receipt)
        await self.audit(db, actor=actor, row=row, action=action, context=context)
        return receipt

    async def _publish_revision(
        self, db: AsyncSession, row: RecruitmentDraft, *, new_cycle: bool
    ) -> None:
        fields = RecruitmentFields.model_validate(row.fields)
        now = self._clock.now()
        try:
            fields.validate_publication(now)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, str(exc), status_code=422
            ) from exc
        number = (
            await db.scalar(
                select(func.max(RecruitmentRevision.number)).where(
                    RecruitmentRevision.recruitment_id == row.id
                )
            )
            or 0
        ) + 1
        revision = RecruitmentRevision(
            recruitment_id=row.id,
            number=number,
            content=fields.model_dump(mode="json", exclude={"contacts"}),
            created_at=now,
        )
        db.add(revision)
        await db.flush()
        for contact in fields.contacts:
            db.add(
                RecruitmentContactAuthorization(
                    recruitment_id=row.id,
                    revision_id=revision.id,
                    channel=contact.channel,
                    value=contact.value,
                    authorized_at=now,
                )
            )
        start_period = new_cycle or (
            row.deadline_at is not None and fields.deadline_at is None
        )
        if new_cycle or (row.deadline_at is None) != (fields.deadline_at is None):
            row.cycle_id = uuid4()
        row.deadline_at = fields.deadline_at
        if row.deadline_at is not None:
            row.last_confirmed_at = row.next_remind_at = None
        elif start_period:
            row.last_confirmed_at, row.next_remind_at = now, now + REMINDER_PERIOD
        row.public_revision_id = revision.id
        row.status = "OPEN"

    async def _act(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RecruitmentVersionCommand,
        context: AuditContext,
        action: str,
    ) -> RecruitmentActionResult:
        async with mutation(db):
            # Replays require current access; qualification is checked only if a
            # new publishing action will actually take effect below.
            row = await self.lock_owned(
                db, actor=actor, project_id=project_id, recruitment_id=recruitment_id
            )
            receipt, digest = await self._receipt(
                db, row=row, action=action, payload=payload
            )
            replayed = receipt is not None
            if receipt is None:
                status = self.logical_status(row)
                if (
                    action in ("publish", "reopen", "confirm_active")
                    and row.moderation_state != "NORMAL"
                ):
                    raise conflict("岗位当前不可恢复公开", kind="MODERATION_CONFLICT")
                if action in ("publish", "reopen"):
                    qualification = await db.get(OwnerQualification, actor.user_id)
                    if qualification is None or qualification.status != "APPROVED":
                        raise BusinessError(
                            ErrorCode.PERMISSION_DENIED,
                            "发布招募须先开通负责人资格",
                            status_code=403,
                        )
                    allowed = (
                        ("DRAFT", "OPEN")
                        if action == "publish"
                        else ("CLOSED", "EXPIRED")
                    )
                    if status not in allowed:
                        raise conflict(
                            "请使用符合当前状态的发布或重新开放动作",
                            kind="STATE_CONFLICT",
                        )
                    await self._publish_revision(db, row, new_cycle=status != "OPEN")
                elif action == "close":
                    if status != "OPEN":
                        raise conflict("当前岗位未开放", kind="STATE_CONFLICT")
                    row.status = "CLOSED"
                elif action == "confirm_active":
                    if status != "OPEN" or row.deadline_at is not None:
                        raise conflict(
                            "仅无日期的有效岗位可确认继续", kind="STATE_CONFLICT"
                        )
                    row.cycle_id = uuid4()
                    row.last_confirmed_at = self._clock.now()
                    row.next_remind_at = self._clock.now() + REMINDER_PERIOD
                elif action == "revoke_contact":
                    await self._revoke(
                        db, row, cast(RevokeRecruitmentContactCommand, payload)
                    )
                else:
                    raise ValueError("Unsupported recruitment action")
                receipt = await self._finish(
                    db,
                    row=row,
                    actor=actor,
                    action=action,
                    payload=payload,
                    digest=digest,
                    context=context,
                )
            return RecruitmentActionResult(
                action=action,
                replayed=replayed,
                applied_version=receipt.applied_version,
                current=await self.response(db, row),
            )

    async def _revoke(
        self,
        db: AsyncSession,
        row: RecruitmentDraft,
        payload: RevokeRecruitmentContactCommand,
    ) -> None:
        contacts = list(
            await db.scalars(
                select(RecruitmentContactAuthorization)
                .where(
                    RecruitmentContactAuthorization.revision_id
                    == row.public_revision_id,
                    RecruitmentContactAuthorization.revoked_at.is_(None),
                )
                .with_for_update()
            )
        )
        target = next((c for c in contacts if c.channel == payload.channel), None)
        if target is None:
            raise conflict("该渠道当前没有公开授权", kind="CONTACT_CONFLICT")
        if len(contacts) == 1 and not payload.close_if_last:
            raise conflict(
                "撤销最后渠道须同时确认关闭招募", kind="LAST_CONTACT_REQUIRES_CLOSE"
            )
        target.revoked_at = self._clock.now()
        row.fields = {
            **row.fields,
            "contacts": [
                c for c in row.fields["contacts"] if c["channel"] != payload.channel
            ],
        }
        if len(contacts) == 1:
            row.status = "CLOSED"

    async def publish(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RecruitmentVersionCommand,
        context: AuditContext,
    ) -> RecruitmentActionResult:
        return await self._act(
            db,
            actor=actor,
            project_id=project_id,
            recruitment_id=recruitment_id,
            payload=payload,
            context=context,
            action="publish",
        )

    async def close(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RecruitmentVersionCommand,
        context: AuditContext,
    ) -> RecruitmentActionResult:
        return await self._act(
            db,
            actor=actor,
            project_id=project_id,
            recruitment_id=recruitment_id,
            payload=payload,
            context=context,
            action="close",
        )

    async def reopen(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RecruitmentVersionCommand,
        context: AuditContext,
    ) -> RecruitmentActionResult:
        return await self._act(
            db,
            actor=actor,
            project_id=project_id,
            recruitment_id=recruitment_id,
            payload=payload,
            context=context,
            action="reopen",
        )

    async def confirm_active(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RecruitmentVersionCommand,
        context: AuditContext,
    ) -> RecruitmentActionResult:
        return await self._act(
            db,
            actor=actor,
            project_id=project_id,
            recruitment_id=recruitment_id,
            payload=payload,
            context=context,
            action="confirm_active",
        )

    async def revoke_contact(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        recruitment_id: UUID,
        payload: RevokeRecruitmentContactCommand,
        context: AuditContext,
    ) -> RecruitmentActionResult:
        return await self._act(
            db,
            actor=actor,
            project_id=project_id,
            recruitment_id=recruitment_id,
            payload=payload,
            context=context,
            action="revoke_contact",
        )
