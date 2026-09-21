# backend/app/workers/jobs/cleanup_files.py
"""File-retention cleanup worker home (plan 07 T7; spec §13, §27;
MERGE_CARRIES item 3 — completed at the merge; hardening pass 4b).

- ``SubmissionCleanupRepository`` is the real ``CleanupRepository`` over
  the submissions table: due retention snapshots joined to their claims,
  excluding by construction (spec §13/§27) permanent rows, legal holds,
  rows already marked deleted, not-yet-due snapshots, and claims still
  inside the review pipeline (status VALIDATING / UNDER_REVIEW — §27
  不得误删尚在审核中的文件). The service re-validates every guard per
  record, and — pass 4b — deletion AUTHORITY is the conditional claim:
  ``claim_for_cleanup`` is ONE UPDATE whose WHERE re-evaluates every
  guard against current committed state and whose RETURNING hands the
  deletion right to exactly one caller (the TOCTOU closure: a
  protection landing between scan and claim makes the claim fail).
- The same repository is the real ``OrphanIntentRepository`` (pass 4b):
  expired never-finalized upload intents — open-expired and burned
  alike — claimed by the same conditional-UPDATE primitive over
  ``upload_intents.cleanup_deleted_at`` and deleted; finalized intents
  are never touched. Cleanup only ever claims intents PAST
  ``expires_at``, safe because the presigned URL TTL deploys shorter
  than the intent TTL (no legal PUT can land after expiry).
- ``workers.cleanup_expired_files`` is the Celery scan shell: sample the
  SystemClock, build the repository over the shared per-job session
  source plus the production S3 adapter, run the TWO phases (files,
  then orphan intents — same job, same beat entry), return the JSON
  summary.

Session lifecycle: the whole scan runs through
``app.workers.session_source.run_with_session_maker`` — one fresh engine
created and disposed inside this task's ``asyncio.run``, every session
the repository opens rides that engine, and no pooled connection ever
crosses the loop boundary the next task invocation closes. Every
repository method is its own short transaction: the claim takes and
releases its row lock in one statement, and NO lock is ever held across
the S3 delete (the declarative-claim ruling).

Retry policy: transient database failures (``OperationalError`` /
``DBAPIError``) retry with bounded backoff — the scan's per-record work
is idempotent by construction (already-deleted rows leave the candidate
set; provider-side retryability is the cleanup service's own taxonomy),
so a re-run after a connection blip only re-touches what is still due.
Storage failures do NOT retry here: the service maps them into the
summary's failure buckets (releasing the claim) and the NEXT scheduled
scan retries (spec §13 删除失败可重试).

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
    IntentRecord,
    MarkOutcome,
    OrphanIntentSummary,
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
    """The real ``CleanupRepository`` and ``OrphanIntentRepository``
    (spec §13/§27; pass 4b): due submissions and orphan intents from
    PostgreSQL, the conditional deletion claim, idempotent
    ``deleted_at`` compare-and-set.

    Every session opens through the injected maker — in production the
    per-job maker from ``run_with_session_maker``, so the repository
    shares the task's one engine; tests may hand any maker (or the class
    may be driven through the service with a fake, as
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
        first so a bounded batch drains the most overdue rows. This is
        only the CANDIDATE listing — the deletion right is claimed
        separately by ``claim_for_cleanup``.
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
                        Submission.cleanup_claimed_at.is_(None),
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

    async def claim_for_cleanup(self, record: FileRecord, *, now: datetime) -> bool:
        """Claim the deletion right over one record (pass 4b ruling).

        ONE conditional UPDATE: the WHERE clause re-evaluates EVERY
        §13/§27 guard against CURRENT committed state — not permanent,
        no legal hold, retention snapshot still due, not already
        deleted, the claim's status still outside the review pipeline,
        and no claim already held — and the same statement sets
        ``cleanup_claimed_at = now``. ``RETURNING`` decides: True only
        for the winner. Single statement = guards and claim are atomic,
        so a legal_hold or review-pipeline entry committed since the
        scan makes the claim fail and the object survive. The row lock
        this UPDATE takes is released at the commit right here — never
        carried across the provider call.
        """
        from sqlalchemy import select, update

        from app.modules.submissions.models import Submission
        from app.modules.tasks.models import AssignmentClaim

        claim_status_outside_review = (
            select(AssignmentClaim.id)
            .where(
                AssignmentClaim.id == Submission.claim_id,
                AssignmentClaim.status.not_in(_UNDER_REVIEW_CLAIM_STATUSES),
            )
            .exists()
        )
        async with self._session_maker() as session:
            result = await session.execute(
                update(Submission)
                .where(
                    Submission.id == record.submission_id,
                    Submission.deleted_at.is_(None),
                    Submission.retention_permanent.is_(False),
                    Submission.legal_hold.is_(False),
                    Submission.retention_until.is_not(None),
                    Submission.retention_until <= now,
                    Submission.cleanup_claimed_at.is_(None),
                    claim_status_outside_review,
                )
                .values(cleanup_claimed_at=now)
                .returning(Submission.id)
            )
            claimed = result.scalar_one_or_none() is not None
            await session.commit()
            return claimed

    async def release_cleanup_claim(self, record: FileRecord) -> None:
        """Release the claim after a provider failure: clear
        ``cleanup_claimed_at`` (only while the row is still unmarked) so
        the next scan re-claims and the protection writers are not
        blocked on a dead claim. The object is NOT marked deleted."""
        from sqlalchemy import update

        from app.modules.submissions.models import Submission

        async with self._session_maker() as session:
            await session.execute(
                update(Submission)
                .where(
                    Submission.id == record.submission_id,
                    Submission.cleanup_claimed_at.is_not(None),
                    Submission.deleted_at.is_(None),
                )
                .values(cleanup_claimed_at=None)
            )
            await session.commit()

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

    # --- the orphan-intent seam (pass 4b; spec §10/§13/§27) --------------

    async def collect_due_intents(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[IntentRecord]:
        """Expired never-finalized intents, oldest expiry first: the
        objects no finalize will ever claim again (finalize refuses at/
        after ``expires_at``; a BURNED intent's object failed
        verification and only a fresh intent — a fresh key — can
        replace it). Finalized intents are excluded: their object
        belongs to the Submission row and the retention pipeline."""
        from sqlalchemy import select

        from app.modules.submissions.models import UploadIntent

        async with self._session_maker() as session:
            rows = (
                await session.execute(
                    select(UploadIntent.id, UploadIntent.object_key)
                    .where(
                        UploadIntent.expires_at <= now,
                        UploadIntent.finalized_submission_id.is_(None),
                        UploadIntent.cleanup_deleted_at.is_(None),
                    )
                    .order_by(UploadIntent.expires_at, UploadIntent.id)
                    .limit(limit)
                )
            ).all()
        return [
            IntentRecord(intent_id=intent_id, object_key=object_key)
            for intent_id, object_key in rows
        ]

    async def claim_intent(self, intent: IntentRecord, *, now: datetime) -> bool:
        """Claim one intent for deletion: ONE conditional UPDATE
        re-evaluating every guard (``expires_at <= now``, never
        finalized, unclaimed) and setting ``cleanup_deleted_at = now``
        — the claim column doubles as the done marker. Finalize cannot
        lose to this claim: it takes the intent-row FOR UPDATE before
        its own expiry check, so a finalize and a claim serialize on
        the row and the loser's predicate fails."""
        from sqlalchemy import update

        from app.modules.submissions.models import UploadIntent

        async with self._session_maker() as session:
            result = await session.execute(
                update(UploadIntent)
                .where(
                    UploadIntent.id == intent.intent_id,
                    UploadIntent.expires_at <= now,
                    UploadIntent.finalized_submission_id.is_(None),
                    UploadIntent.cleanup_deleted_at.is_(None),
                )
                .values(cleanup_deleted_at=now)
                .returning(UploadIntent.id)
            )
            claimed = result.scalar_one_or_none() is not None
            await session.commit()
            return claimed

    async def release_intent(self, intent: IntentRecord) -> None:
        """Release the intent claim after a provider failure: clear
        ``cleanup_deleted_at`` so the next scan retries."""
        from sqlalchemy import update

        from app.modules.submissions.models import UploadIntent

        async with self._session_maker() as session:
            await session.execute(
                update(UploadIntent)
                .where(
                    UploadIntent.id == intent.intent_id,
                    UploadIntent.cleanup_deleted_at.is_not(None),
                )
                .values(cleanup_deleted_at=None)
            )
            await session.commit()


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.cleanup_expired_files",
    autoretry_for=_RETRYABLE_DB_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def cleanup_files_scan(self: Any, request_id: str) -> dict[str, Any]:
    """One retention-cleanup scan, TWO phases (pass 4b): expired files,
    then orphan intents — discover candidates, claim each through the
    conditional UPDATE, delete object-first, mark the row, reduce to
    the JSON summary (§12).

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

    async def _scan(session_maker: Any) -> tuple[CleanupSummary, OrphanIntentSummary]:
        from app.core.config import get_settings
        from app.integrations.object_storage_s3 import S3ObjectStorage
        from app.modules.files.cleanup_service import (
            cleanup_expired_files,
            cleanup_orphaned_intents,
        )

        repository = SubmissionCleanupRepository(session_maker)
        storage = S3ObjectStorage(get_settings())
        files = await cleanup_expired_files(now, repository, storage)
        intents = await cleanup_orphaned_intents(now, repository, storage)
        return files, intents

    files_summary, intents_summary = asyncio.run(run_with_session_maker(_scan))
    payload: dict[str, Any] = {
        "request_id": request_id,
        "scanned": files_summary.scanned,
        "deleted": files_summary.deleted,
        "reconciled": files_summary.reconciled,
        "already_deleted": files_summary.already_deleted,
        "skipped": dict(files_summary.skipped),
        "failed": dict(files_summary.failed),
        "intents_scanned": intents_summary.scanned,
        "intents_deleted": intents_summary.deleted,
        "intents_missing": intents_summary.missing,
        "intents_skipped": dict(intents_summary.skipped),
        "intents_failed": dict(intents_summary.failed),
    }
    logger.info(
        "cleanup_files_scan.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
