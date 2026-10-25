"""Private talent files: guarded inventory and short leased scan transactions."""

import asyncio
import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
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
from app.modules.innovation.talent_common import (
    conflict,
    fingerprint,
    mutation,
    not_found,
)
from app.modules.innovation.talent_file_schemas import (
    TalentFileIntentCreate,
    TalentFileIntentResponse,
    TalentFileListResponse,
    TalentFileResponse,
)
from app.modules.innovation.talent_models import TalentFile, TalentRevision
from app.modules.innovation.talent_schemas import (
    TalentActionResult,
    TalentFields,
    TalentFileCommand,
)
from app.modules.innovation.talent_service import TalentService


def file_response(row: TalentFile) -> TalentFileResponse:
    return TalentFileResponse.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "content_type": row.content_type,
            "size": row.size,
            "state": row.state,
            "failure_code": row.failure_code,
            "version": row.version,
            "removed_at": row.removed_at,
            "revoked_at": row.revoked_at,
            "created_at": row.created_at,
        }
    )


class TalentFileService:
    def __init__(
        self,
        *,
        clock: Clock,
        storage: ObjectStorage,
        scanner: EvidenceScanner,
        audit: AuditLogWriter | None = None,
    ) -> None:
        self.clock, self.storage, self.scanner = clock, storage, scanner
        self.talents = TalentService(clock=clock, audit=audit)

    async def lock_file(
        self, db: AsyncSession, profile_id: UUID, file_id: UUID
    ) -> TalentFile:
        row = await db.scalar(
            select(TalentFile)
            .where(
                TalentFile.id == file_id,
                TalentFile.profile_id == profile_id,
                TalentFile.removed_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise not_found()
        return row

    async def create_intent(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentFileIntentCreate,
        context: AuditContext,
    ) -> tuple[TalentFileIntentResponse, bool]:
        digest = fingerprint(
            {"action": "create-file", **payload.model_dump(mode="json")}
        )
        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            previous = await self.talents.receipt(
                db, actor=actor, request_id=payload.request_id, digest=digest
            )
            row = await db.scalar(
                select(TalentFile)
                .where(
                    TalentFile.profile_id == profile.id,
                    TalentFile.creation_request_id == payload.request_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            created = row is None
            if row is not None:
                if row.creation_fingerprint != digest:
                    raise conflict("该上传请求已用于其他文件", kind="REQUEST_REUSE")
                if row.removed_at is not None or row.revoked_at is not None:
                    raise conflict(
                        "该上传已移除或撤回，请重新选择文件", kind="FILE_UNAVAILABLE"
                    )
                if previous is None:
                    raise RuntimeError("Talent upload receipt missing")
            else:
                if previous is not None:
                    raise RuntimeError("Talent upload record missing")
                count = await db.scalar(
                    select(func.count())
                    .select_from(TalentFile)
                    .where(
                        TalentFile.profile_id == profile.id,
                        TalentFile.removed_at.is_(None),
                        TalentFile.revoked_at.is_(None),
                    )
                )
                if count is not None and count >= 10:
                    raise conflict(
                        "最多保留 10 个上传记录，请先移除不需要的文件",
                        kind="FILE_LIMIT",
                    )
                # Presigning is local cryptography; external object reads/scans
                # below run outside database transactions and locks.
                signed = self.storage.create_talent_file_upload_url(
                    profile_id=profile.id,
                    content_type=payload.content_type,
                    content_length=payload.size,
                    expires_in=timedelta(minutes=15),
                )
                if signed.pinned_content_length != payload.size:
                    raise RuntimeError("Talent storage adapter must pin upload size")
                row = TalentFile(
                    id=uuid4(),
                    profile_id=profile.id,
                    creation_request_id=payload.request_id,
                    creation_fingerprint=digest,
                    name=payload.name,
                    object_key=signed.object_key,
                    content_type=payload.content_type,
                    size=payload.size,
                    upload_url=signed.url,
                    upload_expires_at=signed.expires_at,
                    client_headers=signed.client_headers,
                    state="PENDING",
                    version=1,
                    created_at=self.clock.now(),
                )
                db.add(row)
                await db.flush()
                self.talents.add_receipt(
                    db,
                    actor=actor,
                    row=profile,
                    request_id=payload.request_id,
                    digest=digest,
                    action="create-file",
                    resource_id=row.id,
                )
            await self.talents.record_audit(
                db, actor=actor, row=profile, action="file-intent", context=context
            )
            return TalentFileIntentResponse(
                file=file_response(row),
                upload_url=row.upload_url,
                expires_at=row.upload_expires_at,
                client_headers=row.client_headers,
                pinned_content_length=row.size,
            ), created

    def check_bytes(self, key: str, size: int, content_type: str) -> str:
        head = self.storage.head_object(object_key=key)
        if head is None:
            raise FileNotFoundError("talent file missing")
        if head.size != size or head.content_type != content_type:
            raise InvalidEvidenceError("stored metadata differs")
        obj = self.storage.read_bounded_object(
            object_key=key, max_bytes=MAX_EVIDENCE_BYTES
        )
        if len(obj.content) != size or obj.content_type != content_type:
            raise InvalidEvidenceError("stored content differs")
        validate_evidence_type(obj.content, content_type)
        self.scanner.scan(obj.content)
        return hashlib.sha256(obj.content).hexdigest()

    async def complete(
        self, db: AsyncSession, *, actor: Actor, file_id: UUID, context: AuditContext
    ) -> TalentFileResponse:
        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            row = await self.lock_file(db, profile.id, file_id)
            if row.revoked_at is not None:
                raise conflict("文件已撤回，无法再次检查", kind="FILE_UNAVAILABLE")
            if row.state in {"READY", "REJECTED"}:
                await self.talents.record_audit(
                    db, actor=actor, row=profile, action="file-state", context=context
                )
                return file_response(row)
            now = self.clock.now()
            if (
                row.state == "CHECKING"
                and row.attempt_started_at is not None
                and (row.attempt_started_at + timedelta(seconds=150) > now)
            ):
                raise conflict("文件正在检查，请稍后刷新", kind="CHECK_IN_PROGRESS")
            token = uuid4()
            row.state, row.attempt_token, row.attempt_started_at = (
                "CHECKING",
                token,
                now,
            )
            row.failure_code = None
            row.version += 1
            key, size, content_type = row.object_key, row.size, row.content_type
            await self.talents.record_audit(
                db, actor=actor, row=profile, action="file-check-start", context=context
            )

        state, digest, failure = "READY", None, None
        try:
            digest = await asyncio.wait_for(
                asyncio.to_thread(self.check_bytes, key, size, content_type),
                timeout=140,
            )
        except FileNotFoundError:
            state, failure = "PENDING", "MISSING_OBJECT"
        except EvidenceThreatError:
            state, failure = "REJECTED", "UNSAFE_CONTENT"
        except (InvalidEvidenceError, StoredObjectTooLargeError):
            state, failure = "REJECTED", "INVALID_CONTENT"
        except (EvidenceCheckUnavailableError, ProviderError, OSError, TimeoutError):
            state, failure = "PENDING", "CHECK_UNAVAILABLE"

        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            row = await self.lock_file(db, profile.id, file_id)
            if row.revoked_at is not None:
                raise not_found()
            if row.state != "CHECKING" or row.attempt_token != token:
                raise conflict("检查状态已变化，请刷新", kind="CHECK_REPLACED")
            row.state, row.sha256, row.failure_code = state, digest, failure
            row.attempt_token, row.attempt_started_at = None, None
            row.version += 1
            await self.talents.record_audit(
                db,
                actor=actor,
                row=profile,
                action="file-check-finish",
                context=context,
            )
            return file_response(row)

    async def list_owned(
        self, db: AsyncSession, *, actor: Actor, context: AuditContext
    ) -> TalentFileListResponse:
        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            rows = await db.scalars(
                select(TalentFile)
                .where(
                    TalentFile.profile_id == profile.id,
                    TalentFile.removed_at.is_(None),
                )
                .order_by(TalentFile.created_at, TalentFile.id)
            )
            result = TalentFileListResponse(items=[file_response(row) for row in rows])
            await self.talents.record_audit(
                db, actor=actor, row=profile, action="file-list", context=context
            )
            return result

    async def _change(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        file_id: UUID,
        payload: TalentFileCommand,
        action: str,
        context: AuditContext,
    ) -> TalentActionResult:
        digest = fingerprint(
            {
                "action": action,
                "file_id": str(file_id),
                **payload.model_dump(mode="json"),
            }
        )
        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            previous = await self.talents.receipt(
                db, actor=actor, request_id=payload.request_id, digest=digest
            )
            if previous is not None:
                return await self.talents.result(db, profile, previous, replayed=True)
            self.talents.check_version(profile, payload.version)
            file = await self.lock_file(db, profile.id, file_id)
            revision = (
                await db.get(TalentRevision, profile.public_revision_id)
                if profile.public_revision_id
                else None
            )
            referenced = (
                revision is not None and str(file_id) in revision.content["file_ids"]
            )
            if action == "remove-file":
                if referenced and profile.visibility == "VISIBLE":
                    raise conflict(
                        "文件当前公开展示，请先撤回公开文件", kind="FILE_PUBLIC"
                    )
                file.removed_at = self.clock.now()
            elif action == "revoke-file":
                if not referenced or file.revoked_at is not None:
                    raise conflict("文件当前未获公开授权", kind="FILE_UNAVAILABLE")
                file.revoked_at = self.clock.now()
                assert revision is not None
                content = dict(revision.content)
                content["file_ids"] = [
                    f for f in content["file_ids"] if f != str(file_id)
                ]
                await self.talents.write_revision(
                    db, profile, content=content, contacts=list(revision.contacts)
                )
            else:
                raise ValueError("Unsupported talent file command")
            if file.state == "CHECKING":
                file.state = "PENDING"
                file.attempt_token, file.attempt_started_at = None, None
            file.version += 1
            fields = TalentFields.model_validate(profile.fields)
            fields.file_ids = [f for f in fields.file_ids if f != file_id]
            profile.fields = fields.model_dump(mode="json")
            profile.version += 1
            profile.updated_at = self.clock.now()
            receipt = self.talents.add_receipt(
                db,
                actor=actor,
                row=profile,
                request_id=payload.request_id,
                digest=digest,
                action=action,
                resource_id=file.id,
            )
            await self.talents.record_audit(
                db, actor=actor, row=profile, action=action, context=context
            )
            return await self.talents.result(db, profile, receipt, replayed=False)

    async def remove(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        file_id: UUID,
        payload: TalentFileCommand,
        context: AuditContext,
    ) -> TalentActionResult:
        return await self._change(
            db,
            actor=actor,
            file_id=file_id,
            payload=payload,
            action="remove-file",
            context=context,
        )

    async def revoke(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        file_id: UUID,
        payload: TalentFileCommand,
        context: AuditContext,
    ) -> TalentActionResult:
        return await self._change(
            db,
            actor=actor,
            file_id=file_id,
            payload=payload,
            action="revoke-file",
            context=context,
        )

    async def read_owned_content(
        self, db: AsyncSession, *, actor: Actor, file_id: UUID, context: AuditContext
    ) -> StoredObject:
        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            file = await self.lock_file(db, profile.id, file_id)
            if file.state != "READY":
                raise conflict("文件尚未通过检查，暂不可下载", kind="FILE_NOT_READY")
            key, size, content_type, digest = (
                file.object_key,
                file.size,
                file.content_type,
                file.sha256,
            )
        try:
            obj = await asyncio.wait_for(
                asyncio.to_thread(
                    self.storage.read_bounded_object,
                    object_key=key,
                    max_bytes=MAX_EVIDENCE_BYTES,
                ),
                timeout=140,
            )
        except (OSError, ProviderError, ValueError, TimeoutError) as exc:
            raise conflict("文件暂不可读，请稍后重试", kind="FILE_UNAVAILABLE") from exc
        if (
            len(obj.content) != size
            or obj.content_type != content_type
            or hashlib.sha256(obj.content).hexdigest() != digest
        ):
            raise conflict("文件与已检查的内容不一致", kind="FILE_CHANGED")
        async with mutation(db):
            profile = await self.talents.lock_owned(db, actor=actor)
            file = await self.lock_file(db, profile.id, file_id)
            if file.state != "READY" or file.sha256 != digest:
                raise conflict("文件状态已变化，请刷新", kind="FILE_CHANGED")
            await self.talents.record_audit(
                db, actor=actor, row=profile, action="file-read-own", context=context
            )
        return obj
