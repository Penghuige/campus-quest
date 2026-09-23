# backend/app/modules/notifications/template_admin.py
"""Admin-only NotificationTemplate administration (Plan 08 T5; spec
§25.5).

Design decisions:

- **Admin-only at the SERVICE layer** (``rbac.is_admin``): global
  templates are platform copy, so a Teacher is 403 here even before any
  route guard mounts (the whitelist-admin / account-admin precedent;
  routes will compose ``require_admin_actor`` on top in T9 — defense in
  depth). The service stays callable from workers/tests without HTTP.
- **A template text edit bumps the row's ``version``** (spec §25.5:
  Admin edits bump version; models.py). ``enabled`` toggles do NOT:
  version tracks the CONTENT notifications render from, and a toggle
  changes no text — the audit row records the current version either
  way. (Registering this seam decision in the wave report.)
- **UNIQUE(event_type, channel) conflicts are a typed 409** —
  ``VALIDATION_ERROR`` in the frozen §29 registry (the
  whitelist-confirm / staff-service 409 precedent; a dedicated code is
  a controller registration decision, not this wave's). The friendly
  pre-check covers the sequential case; the constraint closes the
  concurrent race (the loser's ``IntegrityError`` maps to the SAME
  typed conflict — no partial landing, nothing audited for a refusal).
- **Unsafe template markup is rejected at write time** (plan 08 step
  1): V1 templates are pure ``{name}`` placeholder substitution, so
  ``{{``/``}}``/``{%``/``${`` markers and any placeholder outside the
  event type's frozen whitelist are the typed §29 422 BEFORE a row
  persists — the renderer's own grammar
  (``templates.validate_admin_template``), reused not rewritten, so
  write-time and render-time can never disagree about what a template
  may contain.
- **Every committed change writes its audit row in the same
  transaction** (G12; the flush-only ``AuditLogWriter`` discipline):
  ``NOTIFICATION_TEMPLATE_UPSERTED`` for create/text-edit,
  ``NOTIFICATION_TEMPLATE_TOGGLED`` for enable/disable. The §30
  snapshot pair carries a TRUNCATED title/body summary plus
  ``version``/``enabled`` — template copy is product text, not PII
  (G11), but full bodies do not belong on an append-only audit row
  when a summary identifies the change. An idempotent toggle (already
  in the requested state) writes nothing (the staff-invitation replay
  ruling); a refused write writes nothing (nothing happened).
- **Template rows ARE the dispatch/registration render source (PR #5
  gfix C closed the seam):** registration consumes the enabled
  (event_type, IN_APP) row through ``render_template(template=...)``,
  dispatch consumes enabled (event_type, channel) rows through
  ``render_snapshot_template`` (disabled = channel off). This service
  remains the write surface only — it changes no dispatch behavior
  beyond what the rows themselves now say; the consumption semantics
  live in the port and the delivery service.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.notifications.enums import (
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import NotificationTemplate
from app.modules.notifications.templates import (
    InvalidTemplateError,
    UnsafeTemplateMarkupError,
    validate_admin_template,
)

__all__ = [
    "AUDIT_NOTIFICATION_TEMPLATE_TOGGLED",
    "AUDIT_NOTIFICATION_TEMPLATE_UPSERTED",
    "NotificationTemplateAdminService",
    "NotificationTemplateConflictError",
]

# Durable audit action names (G12; Plan 08 T5), the audit-stream
# vocabulary for this surface. Defined here, beside the operations that
# emit them (the staff-service precedent); registration in
# interfaces.md is the controller's step.
AUDIT_NOTIFICATION_TEMPLATE_UPSERTED = "NOTIFICATION_TEMPLATE_UPSERTED"
AUDIT_NOTIFICATION_TEMPLATE_TOGGLED = "NOTIFICATION_TEMPLATE_TOGGLED"

# The polymorphic audit target type for both actions.
_AUDIT_TARGET_TYPE = "notification_template"

_PERMISSION_DENIED_MESSAGE = "仅管理员可以管理通知模板"
_VALIDATION_MESSAGE = "通知模板数据校验失败"
_CONFLICT_MESSAGE = "该事件类型与渠道已存在通知模板"
_NOT_FOUND_MESSAGE = "通知模板不存在"

# Transport bounds: title is the models.py column width; the audit
# snapshot summaries are truncated well below it (full bodies do not
# belong on the append-only audit row — see the module docstring).
_TITLE_MAX_LENGTH = 255
_SUMMARY_MAX_LENGTH = 120


class NotificationTemplateConflictError(BusinessError):
    """A template already exists for the (event_type, channel) pair —
    the UNIQUE constraint's typed 409 (the whitelist-confirm
    precedent: ``VALIDATION_ERROR`` code, conflict status)."""

    def __init__(
        self,
        event_type: NotificationEventType,
        channel: NotificationChannel,
    ) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _CONFLICT_MESSAGE,
            status_code=409,
            details={"event_type": event_type.value, "channel": channel.value},
        )


def _summary(text: str) -> str:
    """The audit-snapshot rendering of a template text: truncated to
    ``_SUMMARY_MAX_LENGTH`` with an ellipsis marker, never the full
    body."""
    if len(text) <= _SUMMARY_MAX_LENGTH:
        return text
    return text[: _SUMMARY_MAX_LENGTH - 1] + "…"


def _snapshot(template: NotificationTemplate) -> dict[str, object]:
    """The §30 snapshot pair entry for one template state: truncated
    title/body summary + version + enabled (business facts, no PII —
    G11)."""
    return {
        "title": _summary(template.title),
        "template_body": _summary(template.template_body),
        "version": template.version,
        "enabled": template.enabled,
    }


class NotificationTemplateAdminService:
    """Create, edit, and toggle NotificationTemplate rows — every
    committed change audited in the same transaction (see the module
    docstring for the full contract).

    ``audit`` defaults to a fresh ``AuditLogWriter`` — stateless and
    flush-only; the default means the default writer, never "no
    auditing" (the RedemptionService wiring ruling)."""

    def __init__(self, audit: AuditLogWriter | None = None) -> None:
        self._audit = audit if audit is not None else AuditLogWriter()

    # --- shared gates -----------------------------------------------------------------

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if not rbac.is_admin(actor.role):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _PERMISSION_DENIED_MESSAGE,
                status_code=403,
            )

    @staticmethod
    def _typed_event_type(
        event_type: NotificationEventType | str,
    ) -> NotificationEventType:
        if isinstance(event_type, NotificationEventType):
            return event_type
        try:
            return NotificationEventType(event_type)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={"field": "event_type", "value": str(event_type)},
            ) from exc

    @staticmethod
    def _typed_channel(channel: NotificationChannel | str) -> NotificationChannel:
        if isinstance(channel, NotificationChannel):
            return channel
        try:
            return NotificationChannel(channel)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={"field": "channel", "value": str(channel)},
            ) from exc

    @staticmethod
    def _validated_texts(
        event_type: NotificationEventType,
        channel: NotificationChannel,
        title: object,
        template_body: object,
    ) -> tuple[str, str]:
        """The stored (stripped, bounded) title/body pair, or the typed
        422 — including the write-time markup gate (see the module
        docstring)."""
        if not isinstance(title, str) or not isinstance(template_body, str):
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={"reason": "title 与 template_body 必须是字符串"},
            )
        stored_title = title.strip()
        stored_body = template_body.strip()
        if not stored_title:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={"field": "title", "reason": "不能为空白"},
            )
        if len(stored_title) > _TITLE_MAX_LENGTH:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={
                    "field": "title",
                    "reason": f"长度不能超过 {_TITLE_MAX_LENGTH} 个字符",
                },
            )
        if not stored_body:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={"field": "template_body", "reason": "不能为空白"},
            )
        try:
            validate_admin_template(
                event_type,
                channel,
                title=stored_title,
                body=stored_body,
            )
        except UnsafeTemplateMarkupError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={
                    "field": exc.field,
                    "marker": exc.marker,
                    "event_type": event_type.value,
                    "channel": channel.value,
                },
            ) from exc
        except InvalidTemplateError as exc:
            # Name the field the offending placeholder actually sits in
            # (the render-time error is field-agnostic).
            field = (
                "title" if f"{{{exc.placeholder}}}" in stored_title else "template_body"
            )
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={
                    "field": field,
                    "placeholder": exc.placeholder,
                    "known_variables": sorted(exc.known),
                    "event_type": event_type.value,
                    "channel": channel.value,
                },
            ) from exc
        return stored_title, stored_body

    async def _locked_template(
        self, db: AsyncSession, template_id: UUID
    ) -> NotificationTemplate:
        """The template row locked FOR UPDATE — version bumps and
        toggles serialize on the row (the system-settings write-path
        discipline)."""
        row = await db.scalar(
            select(NotificationTemplate)
            .where(NotificationTemplate.id == template_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise BusinessError(
                ErrorCode.NOT_FOUND,
                _NOT_FOUND_MESSAGE,
                status_code=404,
                details={"template_id": str(template_id)},
            )
        return row

    async def _append_audit(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        action: str,
        template: NotificationTemplate,
        before: dict[str, object] | None,
        after: dict[str, object],
        audit_context: AuditContext | None,
    ) -> None:
        await self._audit.append(
            db,
            actor=actor,
            action=action,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(template.id),
            before_snapshot=before,
            after_snapshot=after,
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )

    # --- the administration surface -------------------------------------------------

    async def create(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        event_type: NotificationEventType | str,
        channel: NotificationChannel | str,
        title: object,
        template_body: object,
        audit_context: AuditContext | None = None,
    ) -> NotificationTemplate:
        """Create the (event_type, channel) template at version 1 — or
        the typed 409 when one already exists (nothing written, nothing
        audited for a refusal)."""
        self._require_admin(actor)
        typed_event_type = self._typed_event_type(event_type)
        typed_channel = self._typed_channel(channel)
        stored_title, stored_body = self._validated_texts(
            typed_event_type, typed_channel, title, template_body
        )
        existing = await db.scalar(
            select(NotificationTemplate.id).where(
                NotificationTemplate.event_type == typed_event_type.value,
                NotificationTemplate.channel == typed_channel.value,
            )
        )
        if existing is not None:
            raise NotificationTemplateConflictError(typed_event_type, typed_channel)
        template = NotificationTemplate(
            event_type=typed_event_type.value,
            channel=typed_channel.value,
            title=stored_title,
            template_body=stored_body,
            enabled=True,
            version=1,
        )
        db.add(template)
        try:
            await db.flush()
        except IntegrityError as exc:
            # The UNIQUE(event_type, channel) constraint arbitrated a
            # concurrent create: the same typed conflict, never a
            # partial landing.
            raise NotificationTemplateConflictError(
                typed_event_type, typed_channel
            ) from exc
        await self._append_audit(
            db,
            actor=actor,
            action=AUDIT_NOTIFICATION_TEMPLATE_UPSERTED,
            template=template,
            before=None,
            after=_snapshot(template),
            audit_context=audit_context,
        )
        await db.commit()  # row + audit row: one unit (§5)
        return template

    async def update(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        template_id: UUID,
        title: object,
        template_body: object,
        audit_context: AuditContext | None = None,
    ) -> NotificationTemplate:
        """Edit the template's title/body; ``version`` bumps by one and
        both §30 snapshots (truncated summaries + version) ride the
        audit row."""
        self._require_admin(actor)
        template = await self._locked_template(db, template_id)
        typed_event_type = NotificationEventType(template.event_type)
        typed_channel = NotificationChannel(template.channel)
        stored_title, stored_body = self._validated_texts(
            typed_event_type, typed_channel, title, template_body
        )
        before = _snapshot(template)
        template.title = stored_title
        template.template_body = stored_body
        template.version += 1
        await db.flush()
        await self._append_audit(
            db,
            actor=actor,
            action=AUDIT_NOTIFICATION_TEMPLATE_UPSERTED,
            template=template,
            before=before,
            after=_snapshot(template),
            audit_context=audit_context,
        )
        await db.commit()  # edit + audit row: one unit (§5)
        return template

    async def set_enabled(
        self,
        db: AsyncSession,
        *,
        actor: Actor,
        template_id: UUID,
        enabled: bool,
        audit_context: AuditContext | None = None,
    ) -> NotificationTemplate:
        """Enable/disable the template. ``version`` does NOT move (it
        tracks the rendered content; see the module docstring) and an
        idempotent toggle is a silent no-op — no audit row for a
        non-change."""
        self._require_admin(actor)
        if not isinstance(enabled, bool):
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=422,
                details={"field": "enabled", "reason": "必须是布尔值（bool）"},
            )
        template = await self._locked_template(db, template_id)
        if template.enabled is enabled:
            return template  # idempotent replay: nothing happened
        before = _snapshot(template)
        template.enabled = enabled
        await db.flush()
        await self._append_audit(
            db,
            actor=actor,
            action=AUDIT_NOTIFICATION_TEMPLATE_TOGGLED,
            template=template,
            before=before,
            after=_snapshot(template),
            audit_context=audit_context,
        )
        await db.commit()  # toggle + audit row: one unit (§5)
        return template
