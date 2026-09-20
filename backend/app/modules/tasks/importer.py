# backend/app/modules/tasks/importer.py
"""Assignment batch import facade: preview then confirm (spec §7.1).

Flow (spec §7.1 MUST): upload -> parse -> pre-check -> present valid
count / error count / per-row errors -> explicit user confirmation ->
single-transaction write.

Responsibility split (one phase per module, this file the facade):

- ``import_parsing`` — untrusted bytes -> canonicalized valid rows +
  per-row errors, or one file-level rejection (pure code);
- ``import_preview`` — the single-use server-side preview token
  (mint / store / atomic ``GETDEL`` consume);
- ``import_confirm`` — the confirm insert: canonical order, one flush,
  UNIQUE-race translation into the typed 409;
- this module — ``AssignmentImportService``, the use case that owns the
  orchestration and the transaction: task lookup, authorization, the
  friendly duplicate pre-check against the database, and the one
  ``commit`` at the end of confirm (backend-engineering §5).

Cross-phase design decisions:

- **The token is bound to the task, not the previewing account.** Both
  preview and confirm re-run the same authorization (owner,
  MANAGE_ASSIGNMENTS collaborator, or Admin): two authorized collaborators
  may share a preview/confirm hand-off, and an unauthorized caller can
  neither mint nor consume a token. Authorization runs before the
  consume, so a denied confirm never burns a legitimate token.
- **Transaction shape** (§5): one ``flush`` to evaluate constraints, one
  ``commit`` at the end of the use case; helpers never commit.
- The scalars (caps, TTL) are injected — the composition root maps
  them from the ``assignment_import_*`` settings — so the service is
  testable without a deployment environment. ``redis`` follows the OTP
  module's pattern: a shared ``decode_responses=True`` client.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.events import Actor
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.import_confirm import (
    AssignmentImportResult,
    DuplicateAssignmentsError,
    insert_previewed_rows,
)
from app.modules.tasks.import_parsing import (
    _DUPLICATE_IN_DB_MESSAGE,
    SUPPORTED_IMPORT_PLATFORMS,
    AssignmentImportError,
    AssignmentPreviewRow,
    ImportErrorCode,
    ensure_field_size_limit,
    parse_upload,
)
from app.modules.tasks.import_preview import (
    AssignmentImportPreview,
    InvalidPreviewTokenError,
    consume_preview,
    store_preview,
)
from app.modules.tasks.models import Assignment, Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "ImportErrorCode",
    "SUPPORTED_IMPORT_PLATFORMS",
    "AssignmentImportError",
    "AssignmentImportPreview",
    "AssignmentImportResult",
    "AssignmentImportService",
    "AssignmentPreviewRow",
    "DuplicateAssignmentsError",
    "InvalidPreviewTokenError",
]

logger = logging.getLogger(__name__)

_DENIED_MESSAGE = (
    "只有任务所有者、拥有 MANAGE_ASSIGNMENTS 权限的协作者或管理员可以导入 Assignment"
)


# --- authorization -----------------------------------------------------------------


async def _require_import_access(db: AsyncSession, task: Task, actor: Actor) -> None:
    """Owner, MANAGE_ASSIGNMENTS collaborator, or Admin (spec §4.2)."""
    if actor.user_id == task.owner_teacher_id or is_admin(actor.role):
        return
    permissions = await db.scalar(
        select(TaskCollaborator.permissions).where(
            TaskCollaborator.task_id == task.id,
            TaskCollaborator.teacher_id == actor.user_id,
        )
    )
    if (
        permissions is None
        or CollaboratorPermission.MANAGE_ASSIGNMENTS not in permissions
    ):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            _DENIED_MESSAGE,
            status_code=403,
        )


async def _existing_pairs(
    db: AsyncSession, task_id: UUID, candidates: Sequence[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Which candidate pairs already exist in the task (one query)."""
    result = await db.execute(
        select(Assignment.platform, Assignment.keyword).where(
            Assignment.task_id == task_id,
            tuple_(Assignment.platform, Assignment.keyword).in_(candidates),
        )
    )
    return {(platform, keyword) for platform, keyword in result.all()}


# --- the service -------------------------------------------------------------------


class AssignmentImportService:
    """Preview and confirm Assignment batch imports (spec §7.1).

    See the module docstring for the phase split; the method signatures
    are the module's public contract (the router and the tests call
    exactly these two).
    """

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        clock: Clock,
        max_file_bytes: int,
        max_rows: int,
        keyword_max_length: int,
        preview_ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._max_file_bytes = max_file_bytes
        self._max_rows = max_rows
        self._keyword_max_length = keyword_max_length
        self._preview_ttl_seconds = preview_ttl_seconds
        # Bounded cell length (engineering §14): raise csv's field limit
        # above the byte cap BEFORE any parsing can happen (see
        # import_parsing.ensure_field_size_limit).
        ensure_field_size_limit(max_file_bytes)

    # -- preview ----------------------------------------------------------------

    async def preview_assignments(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        data: bytes,
    ) -> AssignmentImportPreview:
        """Parse and pre-check an upload; mint a confirm token iff
        something is importable (spec §7.1 steps 1-4).

        File-level failures return a preview DTO carrying a single
        file-level error and no token — they are validation outcomes, not
        exceptions (backend-engineering §14).
        """
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        await _require_import_access(db, task, actor)

        parsed = parse_upload(
            data,
            max_file_bytes=self._max_file_bytes,
            max_rows=self._max_rows,
            keyword_max_length=self._keyword_max_length,
        )
        if parsed.file_error is not None:
            return self._file_rejected(task_id, parsed.file_error)
        valid = parsed.valid
        errors = parsed.errors

        if valid:
            existing = await _existing_pairs(
                db, task_id, [(row.platform, row.keyword) for row in valid]
            )
            if existing:
                kept: list[AssignmentPreviewRow] = []
                for row in valid:
                    if (row.platform, row.keyword) in existing:
                        errors.append(
                            AssignmentImportError(
                                ImportErrorCode.DUPLICATE_IN_DB,
                                _DUPLICATE_IN_DB_MESSAGE,
                                row_number=row.row_number,
                                platform=row.platform,
                                keyword=row.keyword,
                            )
                        )
                    else:
                        kept.append(row)
                valid = kept

        errors.sort(key=lambda error: error.row_number or 0)

        preview_token: str | None = None
        expires_at = None
        if valid:
            now = self._clock.now()
            expires_at = now + timedelta(seconds=self._preview_ttl_seconds)
            preview_token = await store_preview(
                self._redis,
                task_id=task_id,
                rows=valid,
                expires_at=expires_at,
                ttl_seconds=self._preview_ttl_seconds,
            )

        logger.info(
            "assignment import previewed task_id=%s rows=%d valid=%d errors=%d",
            task_id,
            parsed.total_rows,
            len(valid),
            len(errors),
        )
        return AssignmentImportPreview(
            task_id=task_id,
            total_rows=parsed.total_rows,
            valid=tuple(valid),
            errors=tuple(errors),
            preview_token=preview_token,
            expires_at=expires_at,
        )

    # -- confirm ----------------------------------------------------------------

    async def confirm_assignments(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        preview_token: str,
    ) -> AssignmentImportResult:
        """Insert exactly the previewed rows in one transaction (spec
        §7.1 steps 5-6).

        Authorization runs before the single ``GETDEL`` consume, so a
        denied caller never burns a token; the friendly duplicate
        pre-check runs before the flush; the UNIQUE constraint is the
        race closer, and its IntegrityError becomes the same typed 409.
        """
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        await _require_import_access(db, task, actor)

        rows = await consume_preview(
            self._redis,
            clock=self._clock,
            task_id=task_id,
            preview_token=preview_token,
        )

        conflicts = await _existing_pairs(
            db, task_id, [(row.platform, row.keyword) for row in rows]
        )
        if conflicts:
            raise DuplicateAssignmentsError(sorted(conflicts))

        ordered_rows = await insert_previewed_rows(db, task_id=task_id, rows=rows)
        await db.commit()

        logger.info(
            "assignment import confirmed task_id=%s inserted=%d",
            task_id,
            len(rows),
        )
        return AssignmentImportResult(
            task_id=task_id, inserted=len(rows), rows=ordered_rows
        )

    # -- internals ----------------------------------------------------------------

    def _file_rejected(
        self,
        task_id: UUID,
        error: AssignmentImportError,
    ) -> AssignmentImportPreview:
        logger.info(
            "assignment import rejected task_id=%s code=%s",
            task_id,
            error.code.value,
        )
        return AssignmentImportPreview(
            task_id=task_id,
            total_rows=0,
            valid=(),
            errors=(error,),
            preview_token=None,
            expires_at=None,
        )
