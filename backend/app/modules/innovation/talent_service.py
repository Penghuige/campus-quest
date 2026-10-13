"""Talent commands own transactions and immutable consent snapshots."""

import unicodedata
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.innovation.recruitment_schemas import ContactChannel
from app.modules.innovation.talent_common import (
    conflict,
    fingerprint,
    lock_accounts,
    mutation,
    not_found,
)
from app.modules.innovation.talent_models import (
    TalentActionReceipt,
    TalentFile,
    TalentProfile,
    TalentRevision,
    TalentRevisionFile,
    TalentTag,
    TalentTagSuggestion,
)
from app.modules.innovation.talent_schemas import (
    TalentActionResult,
    TalentFields,
    TalentProfileReadResponse,
    TalentProfileResponse,
    TalentRevokeContactCommand,
    TalentSaveCommand,
    TalentSuggestionCommand,
    TalentSuggestionResponse,
    TalentTagListResponse,
    TalentTagResponse,
    TalentVersionCommand,
)


class TalentService:
    def __init__(self, *, clock: Clock, audit: AuditLogWriter | None = None) -> None:
        self.clock = clock
        self.audit = audit if audit is not None else AuditLogWriter()

    async def lock_owned(
        self, db: AsyncSession, *, actor: Actor, viewer: Actor | None = None
    ) -> TalentProfile:
        await lock_accounts(
            db, actor=viewer or actor, owner_id=actor.user_id, student=viewer is None
        )
        row = await db.scalar(
            select(TalentProfile)
            .where(TalentProfile.owner_user_id == actor.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise not_found()
        return row

    async def response(
        self, db: AsyncSession, row: TalentProfile
    ) -> TalentProfileResponse:
        revision = (
            await db.get(TalentRevision, row.public_revision_id)
            if row.public_revision_id
            else None
        )
        fields = TalentFields.model_validate(row.fields)
        public_content = (
            {k: v for k, v in revision.content.items() if k != "tags"}
            if revision
            else None
        )
        return TalentProfileResponse(
            id=row.id,
            fields=fields,
            version=row.version,
            visibility=cast(Literal["HIDDEN", "VISIBLE"], row.visibility),
            moderation_state=cast(
                Literal["NORMAL", "TAKEN_DOWN"], row.moderation_state
            ),
            public_version=row.public_version,
            public_contact_channels=[
                cast(ContactChannel, value["channel"]) for value in revision.contacts
            ]
            if revision
            else [],
            public_file_ids=[UUID(value) for value in revision.content["file_ids"]]
            if revision
            else [],
            has_unpublished_changes=revision is None
            or public_content != fields.model_dump(mode="json", exclude={"contacts"})
            or revision.contacts
            != [c.model_dump(mode="json") for c in fields.contacts],
            updated_at=row.updated_at,
        )

    async def record_audit(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        row: TalentProfile,
        action: str,
        context: AuditContext,
    ) -> None:
        await self.audit.append(
            db,
            actor=actor,
            action=f"ie.talent.{action}",
            target_type="ie_talent",
            target_id=str(row.id),
            details={
                "version": row.version,
                "public_version": row.public_version,
                "visibility": row.visibility,
            },
            request_id=context.request_id,
            ip_address=context.ip_address,
        )

    async def receipt(
        self, db: AsyncSession, *, actor: Actor, request_id: UUID, digest: str
    ) -> TalentActionReceipt | None:
        existing = await db.scalar(
            select(TalentActionReceipt).where(
                TalentActionReceipt.actor_user_id == actor.user_id,
                TalentActionReceipt.request_id == request_id,
            )
        )
        if existing is not None and existing.fingerprint != digest:
            raise conflict("该请求已用于不同操作，请刷新后确认", kind="REQUEST_REUSE")
        return existing

    def add_receipt(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        row: TalentProfile | None,
        request_id: UUID,
        digest: str,
        action: str,
        resource_id: UUID,
    ) -> TalentActionReceipt:
        receipt = TalentActionReceipt(
            actor_user_id=actor.user_id,
            profile_id=row.id if row else None,
            request_id=request_id,
            fingerprint=digest,
            action=action,
            applied_version=row.version if row else 0,
            resource_id=resource_id,
            created_at=self.clock.now(),
        )
        db.add(receipt)
        return receipt

    async def result(
        self,
        db: AsyncSession,
        row: TalentProfile,
        receipt: TalentActionReceipt,
        *,
        replayed: bool,
    ) -> TalentActionResult:
        return TalentActionResult(
            action=receipt.action,
            replayed=replayed,
            applied_version=receipt.applied_version,
            current=await self.response(db, row),
        )

    @staticmethod
    def check_version(row: TalentProfile, version: int) -> None:
        if row.version != version:
            raise conflict("档案已变化，请保留输入并刷新当前状态")

    async def check_tags(
        self, db: AsyncSession, fields: TalentFields
    ) -> list[TalentTag]:
        tags = list(
            await db.scalars(
                select(TalentTag)
                .where(TalentTag.id.in_(fields.tag_ids), TalentTag.enabled.is_(True))
                .order_by(TalentTag.id)
                .with_for_update()
            )
        )
        if len(tags) != len(fields.tag_ids):
            raise conflict("所选技能标签已不可用，请重新选择", kind="TAG_UNAVAILABLE")
        return tags

    async def write_revision(
        self,
        db: AsyncSession,
        row: TalentProfile,
        *,
        content: dict[str, Any],
        contacts: list[dict[str, str]],
    ) -> None:
        row.public_version += 1
        revision = TalentRevision(
            id=uuid4(),
            profile_id=row.id,
            number=row.public_version,
            content=content,
            contacts=contacts,
            created_at=self.clock.now(),
        )
        db.add(revision)
        await db.flush()
        for file_id in content["file_ids"]:
            db.add(
                TalentRevisionFile(
                    revision_id=revision.id, profile_id=row.id, file_id=UUID(file_id)
                )
            )
        row.public_revision_id = revision.id
        row.public_updated_at = self.clock.now()

    async def replace_public(
        self, db: AsyncSession, row: TalentProfile, *, fields: TalentFields
    ) -> None:
        try:
            fields.validate_publication()
        except ValueError as exc:
            raise conflict(str(exc), kind="INCOMPLETE_PROFILE") from exc
        tags = await self.check_tags(db, fields)
        files = list(
            await db.scalars(
                select(TalentFile)
                .where(
                    TalentFile.profile_id == row.id, TalentFile.id.in_(fields.file_ids)
                )
                .order_by(TalentFile.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(files) != len(fields.file_ids) or any(
            f.state != "READY" or f.removed_at is not None or f.revoked_at is not None
            for f in files
        ):
            raise conflict(
                "所选作品尚未通过检查或已撤回，请重新选择", kind="FILE_UNAVAILABLE"
            )
        content = fields.model_dump(mode="json", exclude={"contacts"})
        by_id = {tag.id: tag.name for tag in tags}
        content["tags"] = [
            {"id": str(tag_id), "name": by_id[tag_id]} for tag_id in fields.tag_ids
        ]
        await self.write_revision(
            db,
            row,
            content=content,
            contacts=[c.model_dump(mode="json") for c in fields.contacts],
        )

    async def get_owned(
        self, db: AsyncSession, *, actor: Actor, context: AuditContext
    ) -> TalentProfileReadResponse:
        async with mutation(db):
            await lock_accounts(db, actor=actor, owner_id=actor.user_id, student=True)
            row = await db.scalar(
                select(TalentProfile)
                .where(TalentProfile.owner_user_id == actor.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                return TalentProfileReadResponse(profile=None)
            await self.record_audit(
                db, actor=actor, row=row, action="read-own", context=context
            )
            return TalentProfileReadResponse(profile=await self.response(db, row))

    async def save(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentSaveCommand,
        context: AuditContext,
    ) -> TalentActionResult:
        digest = fingerprint({"action": "save", **payload.model_dump(mode="json")})
        async with mutation(db):
            await lock_accounts(db, actor=actor, owner_id=actor.user_id, student=True)
            row = await db.scalar(
                select(TalentProfile)
                .where(TalentProfile.owner_user_id == actor.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            previous = await self.receipt(
                db, actor=actor, request_id=payload.request_id, digest=digest
            )
            if previous is not None and row is not None:
                return await self.result(db, row, previous, replayed=True)
            if (row is None and payload.version != 0) or (
                row is not None and row.version != payload.version
            ):
                raise conflict("档案已变化，请保留输入并刷新当前状态")
            await self.check_tags(db, payload.fields)
            now = self.clock.now()
            if row is None:
                row = TalentProfile(
                    id=uuid4(),
                    owner_user_id=actor.user_id,
                    fields=payload.fields.model_dump(mode="json"),
                    version=1,
                    public_version=0,
                    visibility="HIDDEN",
                    moderation_state="NORMAL",
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.fields = payload.fields.model_dump(mode="json")
                row.version += 1
                row.updated_at = now
            await db.flush()
            receipt = self.add_receipt(
                db,
                actor=actor,
                row=row,
                request_id=payload.request_id,
                digest=digest,
                action="save",
                resource_id=row.id,
            )
            await self.record_audit(
                db, actor=actor, row=row, action="save", context=context
            )
            return await self.result(db, row, receipt, replayed=False)

    async def _action(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentVersionCommand,
        action: str,
        context: AuditContext,
    ) -> TalentActionResult:
        digest = fingerprint({"action": action, **payload.model_dump(mode="json")})
        async with mutation(db):
            row = await self.lock_owned(db, actor=actor)
            previous = await self.receipt(
                db, actor=actor, request_id=payload.request_id, digest=digest
            )
            if previous:
                return await self.result(db, row, previous, replayed=True)
            self.check_version(row, payload.version)
            if action == "publish":
                if row.moderation_state != "NORMAL":
                    raise conflict("档案暂时无法开启展示", kind="MODERATED")
                await self.replace_public(
                    db, row, fields=TalentFields.model_validate(row.fields)
                )
                row.visibility = "VISIBLE"
            elif action == "hide":
                if row.visibility != "VISIBLE":
                    raise conflict("档案当前未公开", kind="NOT_VISIBLE")
                row.visibility = "HIDDEN"
                row.public_version += 1
            elif action == "revoke-contact":
                if not isinstance(payload, TalentRevokeContactCommand):
                    raise TypeError("Contact revocation requires typed channel")
                revision = (
                    await db.get(TalentRevision, row.public_revision_id)
                    if row.public_revision_id
                    else None
                )
                if revision is None or not any(
                    c["channel"] == payload.channel for c in revision.contacts
                ):
                    raise conflict("该渠道当前未公开授权", kind="CONTACT_UNAVAILABLE")
                private = TalentFields.model_validate(row.fields)
                private.contacts = [
                    c for c in private.contacts if c.channel != payload.channel
                ]
                row.fields = private.model_dump(mode="json")
                await self.write_revision(
                    db,
                    row,
                    content=dict(revision.content),
                    contacts=[
                        c for c in revision.contacts if c["channel"] != payload.channel
                    ],
                )
            else:
                raise ValueError("Unsupported talent command")
            row.version += 1
            row.updated_at = self.clock.now()
            receipt = self.add_receipt(
                db,
                actor=actor,
                row=row,
                request_id=payload.request_id,
                digest=digest,
                action=action,
                resource_id=row.id,
            )
            await self.record_audit(
                db, actor=actor, row=row, action=action, context=context
            )
            return await self.result(db, row, receipt, replayed=False)

    async def publish(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentVersionCommand,
        context: AuditContext,
    ) -> TalentActionResult:
        return await self._action(
            db, actor=actor, payload=payload, action="publish", context=context
        )

    async def hide(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentVersionCommand,
        context: AuditContext,
    ) -> TalentActionResult:
        return await self._action(
            db, actor=actor, payload=payload, action="hide", context=context
        )

    async def revoke_contact(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentRevokeContactCommand,
        context: AuditContext,
    ) -> TalentActionResult:
        return await self._action(
            db, actor=actor, payload=payload, action="revoke-contact", context=context
        )

    async def list_tags(self, db: AsyncSession) -> TalentTagListResponse:
        tags = await db.scalars(
            select(TalentTag)
            .where(TalentTag.enabled.is_(True))
            .order_by(TalentTag.position, TalentTag.id)
        )
        return TalentTagListResponse(
            items=[TalentTagResponse(id=t.id, name=t.name) for t in tags]
        )

    async def suggest(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        payload: TalentSuggestionCommand,
        context: AuditContext,
    ) -> TalentSuggestionResponse:
        digest = fingerprint({"action": "suggest", **payload.model_dump(mode="json")})
        async with mutation(db):
            await lock_accounts(db, actor=actor, owner_id=actor.user_id, student=True)
            previous = await self.receipt(
                db, actor=actor, request_id=payload.request_id, digest=digest
            )
            if previous:
                return TalentSuggestionResponse(
                    id=previous.resource_id, status="PENDING", replayed=True
                )
            # Compatibility normalization may expand one accepted code point
            # into many. Keep the deduplication key bounded, not the user text.
            normalized = fingerprint(
                {"name": unicodedata.normalize("NFKC", payload.name).casefold()}
            )
            suggestion = await db.scalar(
                select(TalentTagSuggestion).where(
                    TalentTagSuggestion.owner_user_id == actor.user_id,
                    TalentTagSuggestion.normalized_name == normalized,
                    TalentTagSuggestion.status == "PENDING",
                )
            )
            if suggestion is None:
                suggestion = TalentTagSuggestion(
                    id=uuid4(),
                    owner_user_id=actor.user_id,
                    name=payload.name,
                    normalized_name=normalized,
                    reason=payload.reason,
                    status="PENDING",
                    created_at=self.clock.now(),
                )
                db.add(suggestion)
                await db.flush()
            self.add_receipt(
                db,
                actor=actor,
                row=None,
                request_id=payload.request_id,
                digest=digest,
                action="suggest",
                resource_id=suggestion.id,
            )
            await self.audit.append(
                db,
                actor=actor,
                action="ie.talent.suggest",
                target_type="ie_talent_tag_suggestion",
                target_id=str(suggestion.id),
                request_id=context.request_id,
                ip_address=context.ip_address,
            )
            return TalentSuggestionResponse(
                id=suggestion.id, status="PENDING", replayed=False
            )
