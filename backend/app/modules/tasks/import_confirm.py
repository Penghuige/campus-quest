# backend/app/modules/tasks/import_confirm.py
"""Confirm-phase write: insert exactly the previewed rows (spec §7.1
steps 5-6).

Single responsibility: the all-or-nothing confirm insert — canonical row
order, one flush for constraint evaluation, and translation of the
UNIQUE-constraint race into the typed duplicate conflict. The commit
itself stays with the use case (``AssignmentImportService``), per
backend-engineering §5 (one flush, one commit at the service boundary;
helpers never commit).

Design decisions:

- **Races between preview and confirm** (spec §7.1: 正式写入时仍依赖
  数据库 UNIQUE 兜底) are closed by UNIQUE(task_id, platform, keyword):
  the friendly pre-check (in ``importer``) produces the typed 409, and
  the IntegrityError a concurrent importer still triggers is translated
  here into the SAME typed conflict — only that constraint name is
  converted, any other database failure propagates
  (backend-engineering §7).
- **Inserts happen in one canonical order** — rows are sorted by
  ``(platform, keyword)`` before ``add_all`` — so concurrent confirms of
  previews that stored the same pair set in different row orders cannot
  interleave unique-index slot acquisition into a PostgreSQL deadlock
  (whose OperationalError is not an IntegrityError and would escape as
  a 500); the loser queues on the first slot and surfaces the typed 409.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.tasks.enums import AssignmentAvailability
from app.modules.tasks.import_parsing import AssignmentPreviewRow
from app.modules.tasks.models import Assignment

_UNIQUE_CONSTRAINT = "uq_assignments_task_id_platform_keyword"

_PREVIOUS_IMPORT_RACE_MESSAGE = "部分组合已被并发导入，请重新预览后确认"
_PREVIOUS_IMPORT_MESSAGE = "部分组合已存在于该任务，请移除后重新预览"


# --- typed exceptions (router-mapped) ---------------------------------------------


class DuplicateAssignmentsError(BusinessError):
    """Some previewed pairs already exist in the task (typed 409).

    ``conflicts`` carries the pre-checked pairs; the race-closed
    IntegrityError path passes none (the constraint violation names no
    row), which is why the message then points at re-previewing. The
    registry has no dedicated import-conflict code (same posture as
    ``DuplicateCollaboratorError``): ``VALIDATION_ERROR`` with 409.
    """

    def __init__(self, conflicts: Sequence[tuple[str, str]]) -> None:
        details: dict[str, Any] | None = None
        if conflicts:
            details = {
                "conflicts": [
                    {"platform": platform, "keyword": keyword}
                    for platform, keyword in conflicts
                ]
            }
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _PREVIOUS_IMPORT_RACE_MESSAGE
            if not conflicts
            else _PREVIOUS_IMPORT_MESSAGE,
            status_code=409,
            details=details,
        )


# --- DTOs (explicit, never serialized ORM objects) ---------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentImportResult:
    """Outcome of a successful confirm: what landed, all-or-nothing."""

    task_id: UUID
    inserted: int
    rows: tuple[AssignmentPreviewRow, ...]


async def insert_previewed_rows(
    db: AsyncSession,
    *,
    task_id: UUID,
    rows: tuple[AssignmentPreviewRow, ...],
) -> tuple[AssignmentPreviewRow, ...]:
    """Queue the previewed rows in canonical order and flush.

    Returns the rows in the inserted (canonical) order. Does NOT commit:
    the use case owns the transaction (§5).
    """
    # Deadlock avoidance: two concurrent confirms
    # whose previews stored the same pair set in DIFFERENT row orders
    # ([A,B] vs [B,A]) would otherwise take the
    # UNIQUE(task_id, platform, keyword) index slots in opposite
    # orders — PostgreSQL detects the cycle and the loser surfaces
    # DeadlockDetectedError as a DBAPIError/OperationalError, which
    # the ``except IntegrityError`` mapping below cannot convert (a
    # 500). Sorting gives every transaction one canonical slot
    # acquisition order, so the loser queues on the first slot and
    # surfaces the typed 409 instead (verified by reproduction: the
    # reversed-order barrier test deadlocks without the sort).
    ordered_rows = tuple(sorted(rows, key=lambda row: (row.platform, row.keyword)))

    db.add_all(
        [
            Assignment(
                task_id=task_id,
                platform=row.platform,
                keyword=row.keyword,
                availability_status=AssignmentAvailability.AVAILABLE.value,
            )
            for row in ordered_rows
        ]
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        if _UNIQUE_CONSTRAINT in str(exc):
            raise DuplicateAssignmentsError([]) from exc
        raise
    return ordered_rows
