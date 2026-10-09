"""Short locked transactions around bounded external file checking.

Attempt tokens fence late results. Tombstones invalidate in-flight reads;
revisions keep their evidence references independently of the editor list.
"""

import asyncio
import hashlib
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.errors import ProviderError
from app.integrations.evidence_scanner import (
    MAX_EVIDENCE_BYTES,
    EvidenceCheckUnavailableError,
    EvidenceScanner,
    EvidenceThreatError,
    InvalidEvidenceError,
    validate_evidence_type,
)
from app.integrations.object_storage import (
    ObjectStorage,
    StoredObject,
    StoredObjectTooLargeError,
)
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.innovation.evidence_models import AchievementEvidence
from app.modules.innovation.evidence_schemas import (
    EvidenceIntentCreate,
    EvidenceIntentResponse,
    EvidenceListResponse,
    EvidenceResponse,
)
from app.modules.innovation.models import (
    AchievementDraft,
    OwnerQualification,
    ProjectDraft,
)
from app.modules.innovation.owner_service import _require_student
from app.modules.innovation.workflow_guards import require_editable


def _conflict(message: str) -> BusinessError:
    return BusinessError(ErrorCode.CONFLICT, message, status_code=409)


def _response(row: AchievementEvidence) -> EvidenceResponse:
    return EvidenceResponse(
        id=row.id,
        content_type=row.content_type,
        size=row.size,
        state=row.state,
        sha256=row.sha256,
        failure_code=row.failure_code,
        version=row.version,
        created_at=row.created_at,
    )


class EvidenceService:
    def __init__(
        self,
        *,
        clock: Clock,
        storage: ObjectStorage,
        scanner: EvidenceScanner,
        audit: AuditLogWriter | None = None,
    ) -> None:
        self._clock = clock
        self._storage = storage
        self._scanner = scanner
        self._audit = audit if audit is not None else AuditLogWriter()

    async def _owned(
        self, db: AsyncSession, *, actor: Actor, project_id: UUID, achievement_id: UUID
    ) -> None:
        await _require_student(db, actor)
        qualification = await db.scalar(
            select(OwnerQualification.status)
            .where(OwnerQualification.user_id == actor.user_id)
            .with_for_update()
        )
        parent = await db.scalar(
            select(ProjectDraft.id)
            .where(
                ProjectDraft.id == project_id,
                ProjectDraft.owner_user_id == actor.user_id,
            )
            .with_for_update()
        )
        child = (
            await db.scalar(
                select(AchievementDraft.id)
                .where(
                    AchievementDraft.id == achievement_id,
                    AchievementDraft.project_id == project_id,
                )
                .with_for_update()
            )
            if parent
            else None
        )
        if child is None:
            raise BusinessError(
                ErrorCode.NOT_FOUND, "项目或成果不存在", status_code=404
            )
        if qualification != "APPROVED":
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED, "请先开通项目负责人资格", status_code=403
            )

    async def _row(
        self, db: AsyncSession, achievement_id: UUID, evidence_id: UUID
    ) -> AchievementEvidence:
        row = await db.scalar(
            select(AchievementEvidence)
            .where(
                AchievementEvidence.id == evidence_id,
                AchievementEvidence.achievement_id == achievement_id,
                AchievementEvidence.removed_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise BusinessError(ErrorCode.NOT_FOUND, "证明材料不存在", status_code=404)
        return row

    async def _record(
        self,
        db: AsyncSession,
        actor: Actor,
        target_id: UUID,
        action: str,
        context: AuditContext | None,
        row: AchievementEvidence | None = None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type="ie_achievement_evidence",
            target_id=str(target_id),
            after_snapshot={"state": row.state, "version": row.version}
            if row
            else None,
            request_id=context.request_id if context else None,
            ip_address=context.ip_address if context else None,
        )

    async def create_intent(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        payload: EvidenceIntentCreate,
        context: AuditContext | None = None,
    ) -> tuple[EvidenceIntentResponse, bool]:
        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        await require_editable(db, achievement_id)
        fingerprint = hashlib.sha256(
            f"{payload.content_type}:{payload.size}".encode()
        ).hexdigest()
        row = await db.scalar(
            select(AchievementEvidence)
            .where(
                AchievementEvidence.achievement_id == achievement_id,
                AchievementEvidence.creation_request_id == payload.request_id,
            )
            .execution_options(populate_existing=True)
        )
        created = row is None
        now = self._clock.now()
        if row is not None:
            if row.creation_payload_fingerprint != fingerprint:
                raise _conflict("这次上传请求已用于其他文件信息")
            if row.removed_at is not None:
                raise _conflict("这次上传已移除，请新建上传请求")
        else:
            count = await db.scalar(
                select(func.count())
                .select_from(AchievementEvidence)
                .where(
                    AchievementEvidence.achievement_id == achievement_id,
                    AchievementEvidence.removed_at.is_(None),
                    or_(
                        AchievementEvidence.state != "PENDING",
                        AchievementEvidence.expires_at > now,
                    ),
                )
            )
            if count is not None and count >= 5:
                raise _conflict("每项成果最多保留五份证明，请先移除多余材料")
            signed = self._storage.create_evidence_upload_url(
                achievement_id=achievement_id,
                content_type=payload.content_type,
                content_length=payload.size,
                expires_in=timedelta(minutes=10),
            )
            if signed.pinned_content_length != payload.size:
                raise RuntimeError(
                    "evidence signing adapter did not pin the requested size"
                )
            row = AchievementEvidence(
                achievement_id=achievement_id,
                creation_request_id=payload.request_id,
                creation_payload_fingerprint=fingerprint,
                object_key=signed.object_key,
                content_type=payload.content_type,
                size=payload.size,
                state="PENDING",
                upload_url=signed.url,
                upload_expires_at=signed.expires_at,
                client_headers=signed.client_headers,
                expires_at=now + timedelta(minutes=15),
                created_at=now,
                updated_at=now,
                version=1,
            )
            db.add(row)
            await db.flush()
        result = EvidenceIntentResponse(
            evidence=_response(row),
            upload_url=row.upload_url,
            expires_at=row.upload_expires_at,
            client_headers=row.client_headers,
            pinned_content_length=row.size,
        )
        await self._record(db, actor, row.id, "IE_EVIDENCE_INTENT", context, row)
        await db.commit()
        return result, created

    async def list_owned(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        context: AuditContext | None = None,
    ) -> EvidenceListResponse:
        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        rows = await db.scalars(
            select(AchievementEvidence)
            .where(
                AchievementEvidence.achievement_id == achievement_id,
                AchievementEvidence.removed_at.is_(None),
            )
            .order_by(AchievementEvidence.created_at, AchievementEvidence.id)
            .execution_options(populate_existing=True)
        )
        result = EvidenceListResponse(items=[_response(row) for row in rows])
        await self._record(db, actor, achievement_id, "IE_EVIDENCE_LIST", context)
        await db.commit()
        return result

    def _check(self, key: str, size: int, content_type: str) -> str:
        head = self._storage.head_object(object_key=key)
        if head is None:
            raise FileNotFoundError("evidence missing")
        if head.size != size or head.content_type != content_type:
            raise InvalidEvidenceError("stored metadata differs from declaration")
        obj = self._storage.read_bounded_object(
            object_key=key, max_bytes=MAX_EVIDENCE_BYTES
        )
        if len(obj.content) != size or obj.content_type != content_type:
            raise InvalidEvidenceError("stored content differs from declaration")
        validate_evidence_type(obj.content, content_type)
        self._scanner.scan(obj.content)
        return hashlib.sha256(obj.content).hexdigest()

    async def complete(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        evidence_id: UUID,
        context: AuditContext | None = None,
    ) -> EvidenceResponse:
        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        row = await self._row(db, achievement_id, evidence_id)
        now = self._clock.now()
        if row.state in ("READY", "REJECTED"):
            result = _response(row)
            await self._record(db, actor, row.id, "IE_EVIDENCE_READ", context)
            await db.commit()
            return result
        await require_editable(db, achievement_id)
        if row.expires_at <= now:
            raise _conflict("上传意向已过期，请移除后重新上传")
        if (
            row.state == "CHECKING"
            and row.checking_until is not None
            and row.checking_until > now
        ):
            raise _conflict("材料正在检查，请稍后刷新")
        token = uuid4()
        row.state, row.check_token = "CHECKING", token
        # Storage HEAD/GET have the adapter's bounded retry/timeout policy;
        # 150s lease also covers them plus the scanner's absolute 30s deadline.
        row.checking_until = now + timedelta(seconds=150)
        row.failure_code = None
        row.version += 1
        row.updated_at = now
        key, size, content_type = row.object_key, row.size, row.content_type
        await self._record(db, actor, row.id, "IE_EVIDENCE_CHECK_START", context, row)
        await db.commit()

        state: Literal["PENDING", "READY", "REJECTED"] = "READY"
        digest, failure = None, None
        missing = False
        try:
            digest = await asyncio.wait_for(
                asyncio.to_thread(self._check, key, size, content_type), timeout=140
            )
        except FileNotFoundError:
            state, failure, missing = "PENDING", "MISSING_OBJECT", True
        except EvidenceThreatError:
            state, failure = "REJECTED", "UNSAFE_CONTENT"
        except (InvalidEvidenceError, StoredObjectTooLargeError):
            state, failure = "REJECTED", "INVALID_CONTENT"
        except (EvidenceCheckUnavailableError, ProviderError, OSError, TimeoutError):
            state, failure = "PENDING", "CHECK_UNAVAILABLE"

        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        row = await self._row(db, achievement_id, evidence_id)
        if row.state != "CHECKING" or row.check_token != token:
            raise _conflict("检查请求已被更新，请刷新材料状态")
        await require_editable(db, achievement_id)
        row.state, row.sha256, row.failure_code = state, digest, failure
        row.check_token, row.checking_until = None, None
        row.version += 1
        row.updated_at = self._clock.now()
        result = _response(row)
        await self._record(db, actor, row.id, "IE_EVIDENCE_CHECK_FINISH", context, row)
        await db.commit()
        if missing:
            raise _conflict("尚未收到上传文件，请完成上传后重试")
        return result

    async def remove(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        evidence_id: UUID,
        context: AuditContext | None = None,
    ) -> None:
        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        await require_editable(db, achievement_id)
        row = await self._row(db, achievement_id, evidence_id)
        if row.referenced:
            raise _conflict("材料已用于审核记录，不能移除")
        row.removed_at = self._clock.now()
        row.version += 1
        row.updated_at = self._clock.now()
        await self._record(db, actor, row.id, "IE_EVIDENCE_REMOVE", context, row)
        await db.commit()

    async def read_content(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        project_id: UUID,
        achievement_id: UUID,
        evidence_id: UUID,
        context: AuditContext | None = None,
    ) -> StoredObject:
        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        row = await self._row(db, achievement_id, evidence_id)
        if row.state != "READY":
            raise _conflict("材料尚未通过检查")
        key, digest, size, content_type = (
            row.object_key,
            row.sha256,
            row.size,
            row.content_type,
        )
        await self._record(db, actor, row.id, "IE_EVIDENCE_CONTENT_READ", context)
        await db.commit()
        try:
            obj = await asyncio.to_thread(
                self._storage.read_bounded_object,
                object_key=key,
                max_bytes=MAX_EVIDENCE_BYTES,
            )
        except (OSError, ProviderError, ValueError) as exc:
            raise _conflict("材料暂不可读，请稍后重试") from exc
        if (
            len(obj.content) != size
            or obj.content_type != content_type
            or hashlib.sha256(obj.content).hexdigest() != digest
        ):
            raise _conflict("材料与已检查的版本不一致，禁止下载")
        await self._owned(
            db, actor=actor, project_id=project_id, achievement_id=achievement_id
        )
        row = await self._row(db, achievement_id, evidence_id)
        if row.state != "READY" or row.sha256 != digest:
            raise _conflict("材料状态已变更，请刷新")
        await db.commit()
        return obj
