# backend/app/workers/jobs/cleanup_files.py
"""File-retention cleanup worker home (plan 07 T7; spec §13, §27;
MERGE_CARRIES item 3 — completed at the merge).

- ``SubmissionCleanupRepository`` is the real ``CleanupRepository`` over
  the submissions table: due retention snapshots joined to their claims,
  excluding by construction (spec §13/§27) permanent rows, legal holds,
  rows already marked deleted, not-yet-due snapshots, and claims still
  inside the review pipeline (status VALIDATING / UNDER_REVIEW — §27
  不得误删尚在审核中的文件). The service (``app.modules.files.
  cleanup_service``) re-validates every guard per record anyway, so a
  stale listing can never reach the provider delete.
- ``workers.cleanup_expired_files`` is the Celery scan shell: sample the
  SystemClock, build the repository over the shared per-job session
  source plus the production S3 adapter, call
  ``cleanup_expired_files``, return the JSON summary.

Session lifecycle: the whole scan runs through
``app.workers.session_source.run_with_session_maker`` — one fresh engine
created and disposed inside this task's ``asyncio.run``, every session
the repository opens rides that engine, and no pooled connection ever
crosses the loop boundary the next task invocation closes.

Retry policy: transient database failures (``OperationalError`` /
``DBAPIError``) retry with bounded backoff — the scan's per-record work
is idempotent by construction (already-deleted rows leave the candidate
set; provider-side retryability is the cleanup service's own taxonomy),
so a re-run after a connection blip only re-touches what is still due.
Storage failures do NOT retry here: the service maps them into the
summary's failure buckets and the NEXT scheduled scan retries (spec §13
删除失败可重试).

Importing this module stays environment-free (the celery_app
lazy-construction contract): no app.db, no settings, no boto3 — the
module-level imports are the cleanup service's frozen data shapes, the
per-job session source, and ``sqlalchemy.exc`` (exception classes for
the autoretry tuple, the project_ranking_update ``redis.exceptions``
precedent).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy.exc import (  # env-free: see module docstring
    DBAPIError,
    OperationalError,
)

from app.modules.files.cleanup_service import (
    CLEANUP_BATCH_LIMIT,
    CleanupSummary,
    FileRecord,
    MarkOutcome,
)
from app.workers.session_source import run_with_session_maker

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

#: Claim statuses whose files the review pipeline still owns (spec §27
#: 不得误删尚在审核中的文件): the machine stage (VALIDATING) and the
#: human stage (UNDER_REVIEW). Actionable claims (CLAIMED /
#: REVISION_REQUIRED) are the STUDENT's to finish, not under review —
#: their overdue rows are the expiry worker's business, and the student
#: action on them is a fresh upload, never the old object.
_UNDER_REVIEW_CLAIM_STATUSES: tuple[str, ...] = ("VALIDATING", "UNDER_REVIEW")

#: Transient database failures the scan's bounded autoretry covers
#: (the autoretry audit's shared gap; see the module docstring).
_RETRYABLE_DB_TRANSIENTS = (OperationalError, DBAPIError)

_MAX_RETRIES = 5


class SubmissionCleanupRepository:
    """The real ``CleanupRepository`` (spec §13/§27): due submissions
    from PostgreSQL, idempotent ``deleted_at`` compare-and-set.

    Every session opens through the injected maker — in production the
    per-job maker from ``run_with_session_maker``, so the repository
    shares the task's one engine; tests may hand any maker (or the
    class may be driven through the service with a fake, as
    ``tests/workers/test_file_cleanup.py`` does).
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    async def collect_due_files(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[FileRecord]:
        """Retention snapshots eligible for deletion at ``now``.

        The exclusion set is the §13/§27 candidate filter: not permanent
        (the XOR-checked snapshot columns make the predicate redundant
        with ``retention_until IS NOT NULL``, kept for the fail-safe
        reading), no legal hold, not already marked, retention due, and
        the claim not inside the review pipeline. Ordered oldest-due
        first so a bounded batch drains the most overdue rows.
        """
        from sqlalchemy import select

        from app.modules.submissions.models import Submission
        from app.modules.tasks.models import AssignmentClaim

        async with self._session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        Submission.id,
                        Submission.object_key,
                        Submission.retention_until,
                        Submission.retention_permanent,
                        Submission.legal_hold,
                        Submission.deleted_at,
                    )
                    .join(AssignmentClaim, AssignmentClaim.id == Submission.claim_id)
                    .where(
                        Submission.deleted_at.is_(None),
                        Submission.retention_permanent.is_(False),
                        Submission.legal_hold.is_(False),
                        Submission.retention_until.is_not(None),
                        Submission.retention_until <= now,
                        AssignmentClaim.status.not_in(_UNDER_REVIEW_CLAIM_STATUSES),
                    )
                    .order_by(Submission.retention_until, Submission.id)
                    .limit(limit)
                )
            ).all()
        return [
            FileRecord(
                submission_id=submission_id,
                object_key=object_key,
                retention_until=retention_until,
                permanent=False,
                legal_hold=False,
                protected=False,
                deleted_at=None,
            )
            for (
                submission_id,
                object_key,
                retention_until,
                _permanent,
                _legal_hold,
                _deleted_at,
            ) in rows
        ]

    async def mark_deleted(
        self, record: FileRecord, *, deleted_at: datetime
    ) -> MarkOutcome:
        """Idempotent ``deleted_at`` compare-and-set on the submission
        row: a live row records the instant (MARKED); a row another
        delivery already marked answers ALREADY_DELETED without
        overwriting the earlier instant. Business metadata, validation
        reports, and audit rows are never touched (§13)."""
        from sqlalchemy import select, update

        from app.modules.submissions.models import Submission

        async with self._session_maker() as session:
            result = await session.execute(
                update(Submission)
                .where(
                    Submission.id == record.submission_id,
                    Submission.deleted_at.is_(None),
                )
                .values(deleted_at=deleted_at)
            )
            # cast: execute() is typed as the generic Result, but an
            # UPDATE statement always hands back a CursorResult whose
            # rowcount is the matched-row count.
            matched = cast("CursorResult[Any]", result).rowcount
            if matched == 1:
                await session.commit()
                return MarkOutcome.MARKED
            observed = await session.scalar(
                select(Submission.deleted_at).where(
                    Submission.id == record.submission_id
                )
            )
            await session.commit()
            if observed is not None:
                return MarkOutcome.ALREADY_DELETED
            # The candidate read saw a row the update cannot find: rows
            # are never deleted from this table. Unreachable by the
            # service's flows — fail loudly instead of fabricating a
            # success (the validation-service unreachable precedent).
            raise RuntimeError(
                f"submission {record.submission_id} vanished between the "
                "cleanup candidate read and mark_deleted"
            )


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.cleanup_expired_files",
    autoretry_for=_RETRYABLE_DB_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def cleanup_files_scan(self: Any, request_id: str) -> dict[str, Any]:
    """One retention-cleanup scan: discover due files, delete each
    object-first, mark the row, reduce to the JSON summary (§12).

    The task body only composes: clock, repository, storage adapter,
    service. Every delete/retain decision lives in
    ``app.modules.files.cleanup_service``; the S3 adapter is the
    production binding (``S3ObjectStorage`` from settings — the same
    fail-closed composition ``validate_submission`` uses).
    """
    from app.core.clock import SystemClock

    job_id = self.request.id
    now = SystemClock().now()
    logger.info(
        "cleanup_files_scan.start",
        extra={"request_id": request_id, "job_id": job_id},
    )

    async def _scan(session_maker: Any) -> CleanupSummary:
        from app.core.config import get_settings
        from app.integrations.object_storage_s3 import S3ObjectStorage
        from app.modules.files.cleanup_service import cleanup_expired_files

        repository = SubmissionCleanupRepository(session_maker)
        storage = S3ObjectStorage(get_settings())
        return await cleanup_expired_files(now, repository, storage)

    summary = asyncio.run(run_with_session_maker(_scan))
    payload: dict[str, Any] = {
        "request_id": request_id,
        "scanned": summary.scanned,
        "deleted": summary.deleted,
        "reconciled": summary.reconciled,
        "already_deleted": summary.already_deleted,
        "skipped": dict(summary.skipped),
        "failed": dict(summary.failed),
    }
    logger.info(
        "cleanup_files_scan.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
