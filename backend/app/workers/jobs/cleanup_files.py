# backend/app/workers/jobs/cleanup_files.py
"""File-retention cleanup worker home (plan 07 T7; spec §13, §27;
MERGE_CARRIES item 3 — completed at the merge; hardening pass 4b; pass
5a leases + unified serialization boundary; final pass A fencing).

- ``SubmissionCleanupRepository`` is the real ``CleanupRepository`` over
  the submissions table: due retention snapshots joined to their claims,
  excluding by construction (spec §13/§27) permanent rows, legal holds,
  rows already marked deleted, not-yet-due snapshots, and claims still
  inside the review pipeline (status VALIDATING / UNDER_REVIEW — §27
  不得误删尚在审核中的文件). The service re-validates every guard per
  record, and — pass 4b — deletion AUTHORITY is the claimed lease
  (``cleanup_claimed_at`` + ``cleanup_lease_expires_at``, pass 5a):
  ``claim_for_cleanup`` is a SHORT transaction that locks the submission
  row and then the claim row — the SAME submission -> claim order the
  protection paths (validation tx1/tx2) lock them; the reward-lock
  review entry locks the claim row alone, which the claim's second lock
  serializes with — re-evaluates every §13/§27 guard against CURRENT
  committed state under both locks, writes the lease plus a fresh
  ownership token, and commits (pass 5a's unified serialization
  boundary: the protection writers' claim-row-locked guard and this
  claim can no longer interleave — whichever side takes the locks first
  wins). An expired lease is a crashed worker: the row re-enters the
  candidate set and the takeover resets both timestamps and REWRITES
  the token after the same guarded re-evaluation.
- FENCING (final pass A): the claim hands back the token it wrote, and
  ``release_cleanup_claim`` / ``mark_deleted`` (and the intent-side
  twins) compare-and-set on it. A worker whose lease expired mid-flight
  and lost the row to a takeover therefore matches ZERO rows with its
  stale token — the release/mark is logged and abandoned, never applied
  to the takeover's claim (ABA closure; the owner claim-ownership
  ruling).
- The same repository is the real ``OrphanIntentRepository`` (pass 4b):
  expired never-finalized upload intents — open-expired and burned
  alike — claimed by the same conditional-UPDATE primitive (pass 5a
  splits the claim columns ``cleanup_claimed_at`` /
  ``cleanup_lease_expires_at`` from the ``cleanup_deleted_at`` DONE
  marker; final pass A adds the token to the same statement) and
  deleted; finalized intents are never touched. Intents carry no
  protection semantics, so the single conditional UPDATE remains their
  whole serialization boundary — the lease adds only crash takeover,
  the token only stale-worker fencing. Cleanup only ever claims intents
  PAST ``expires_at``, safe because the presigned URL TTL deploys
  shorter than the intent TTL (no legal PUT can land after expiry).
- ``workers.cleanup_expired_files`` is the Celery scan shell: sample the
  SystemClock, build the repository (lease length from Settings) and the
  object-storage adapter, run the TWO phases (files, then orphan
  intents — same job, same beat entry), return the JSON summary.

Session lifecycle: the whole scan runs through
``app.workers.session_source.run_with_session_maker`` — one fresh engine
created and disposed inside this task's ``asyncio.run``, every session
the repository opens rides that engine, and no pooled connection ever
crosses the loop boundary the next task invocation closes. Every
repository method is its own short transaction: the claim takes and
releases its two row locks in one transaction, and NO lock is ever held
across the S3 delete (the declarative-claim ruling).

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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

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
    (spec §13/§27; pass 4b claim, pass 5a lease, final pass A fencing):
    due submissions and orphan intents from PostgreSQL, the lock-ordered
    leased deletion claim with crash takeover and an ownership token,
    token-CAS release/mark (a stale worker cannot touch a takeover's
    claim), idempotent ``deleted_at`` compare-and-set.

    Every session opens through the injected maker — in production the
    per-job maker from ``run_with_session_maker``, so the repository
    shares the task's one engine; tests may hand any maker (or the class
    may be driven through the service with a fake, as
    ``tests/workers/test_file_cleanup.py`` does).
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: int,
    ) -> None:
        """``lease_seconds`` is the claim lease length (the
        ``cleanup_claim_lease_seconds`` setting in production; explicit
        here so the exclusivity horizon under test is a visible fact,
        not a hidden default)."""
        self._session_maker = session_maker
        self._lease_seconds = lease_seconds

    def _lease_expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self._lease_seconds)

    async def collect_due_files(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[FileRecord]:
        """Retention snapshots eligible for deletion at ``now``.

        The exclusion set is the §13/§27 candidate filter: not permanent
        (the XOR-checked snapshot columns make the predicate redundant
        with ``retention_until IS NOT NULL``, kept for the fail-safe
        reading), no legal hold, not already marked, retention due, the
        claim not inside the review pipeline — and no LIVE cleanup lease
        (unclaimed rows, plus claimed rows whose lease expired: pass 5a
        ends the permanent exclusion of claimed rows — an expired lease
        is a crashed worker whose row the scan must take over). Ordered
        oldest-due first so a bounded batch drains the most overdue
        rows. This is only the CANDIDATE listing — the deletion right is
        claimed separately by ``claim_for_cleanup``.
        """
        from sqlalchemy import or_, select

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
                        # No live lease: unclaimed, or claimed with an
                        # expired lease (crash takeover re-candidates the
                        # row). A claimed row with a NULL lease stays
                        # excluded — unknown lease reads as live.
                        or_(
                            Submission.cleanup_claimed_at.is_(None),
                            Submission.cleanup_lease_expires_at <= now,
                        ),
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

    async def claim_for_cleanup(
        self, record: FileRecord, *, now: datetime
    ) -> UUID | None:
        """Claim the deletion right over one record (pass 4b ruling;
        pass 5a unified serialization boundary + lease; final pass A
        fencing token) — the return value is the OWNERSHIP TOKEN this
        claim wrote, or None when the claim was lost.

        ONE short transaction, locked in the protection paths' own
        order: (1) the submission row FOR UPDATE, (2) the claim row FOR
        UPDATE — the same submission -> claim direction validation tx1 /
        tx2 take (no service locks claim -> submission, so no cycle can
        form). Under BOTH locks every §13/§27 guard is re-evaluated
        against the CURRENT committed rows — not permanent, no legal
        hold, retention still due, not already deleted, the claim's
        status still outside the review pipeline, and no LIVE lease
        held — and the same transaction writes the lease
        (``cleanup_claimed_at = now`` plus
        ``cleanup_lease_expires_at = now + lease_seconds``) AND a fresh
        ``cleanup_claim_token`` (a new uuid4 on every claim and every
        takeover — the fencing discipline), then commits. A takeover
        (the previous lease expired) resets both timestamps and
        REWRITES the token, which is what fences the old worker's late
        release/mark out of the row it no longer owns.

        Mutual exclusion with the protection writers is the row locks
        themselves: a protection transaction holding either lock blocks
        this claim until it commits, and this claim's guard then sees
        the committed protected state and fails — and vice versa, a
        protection arriving mid-claim waits and its guard sees the
        committed claim (the typed 409 — the unfinished claim blocks
        protection even past lease expiry, the claim-ownership ruling).
        The locks are released at the commit right here — never carried
        across the provider call.
        """
        from sqlalchemy import select

        from app.modules.submissions.models import Submission
        from app.modules.tasks.models import AssignmentClaim

        token = uuid4()
        async with self._session_maker() as session:
            # (1) The submission row lock — the protection paths' FIRST
            # lock (validation tx1's `SELECT submissions ... FOR UPDATE`).
            submission = await session.scalar(
                select(Submission)
                .where(Submission.id == record.submission_id)
                .with_for_update()
            )
            if submission is None:
                # Rows are never deleted from this table, so a live
                # candidate cannot vanish; if it somehow does, the
                # fail-safe answer is claim-lost (no deletion right).
                await session.rollback()
                return None
            # (2) The claim row lock — same direction as validation
            # tx1's second lock; the reward-lock review entry takes this
            # lock alone, which is the boundary it serializes on.
            claim = await session.scalar(
                select(AssignmentClaim)
                .where(AssignmentClaim.id == submission.claim_id)
                .with_for_update()
            )
            lease_live = (
                submission.cleanup_claimed_at is not None
                and submission.deleted_at is None
                and (
                    submission.cleanup_lease_expires_at is None
                    or submission.cleanup_lease_expires_at > now
                )
            )
            if (
                submission.deleted_at is not None
                or submission.retention_permanent
                or submission.legal_hold
                or submission.retention_until is None
                or submission.retention_until > now
                or lease_live
                or claim is None  # FK-broken unreachable shape
                or claim.status in _UNDER_REVIEW_CLAIM_STATUSES
            ):
                # A guard failed on the locked current state, or another
                # delivery holds a live lease: no deletion right. Read-
                # only transaction — rollback releases both locks.
                await session.rollback()
                return None
            submission.cleanup_claimed_at = now
            submission.cleanup_lease_expires_at = self._lease_expiry(now)
            submission.cleanup_claim_token = token
            await session.commit()
            return token

    async def release_cleanup_claim(self, record: FileRecord, *, token: UUID) -> None:
        """Release THIS claim after a provider failure: clear all three
        claim columns — but only while the row still carries ``token``
        (the fencing CAS).

        A worker whose lease expired mid-flight and whose row a takeover
        re-claimed arrives here with a STALE token: the WHERE matches
        zero rows, the release is logged and abandoned, and the
        takeover's live claim is untouched (ABA closure — the old shape,
        which matched on ``claimed_at IS NOT NULL`` alone, could clear a
        healthy successor's claim). The object is NOT marked deleted
        either way."""
        from sqlalchemy import update

        from app.modules.submissions.models import Submission

        async with self._session_maker() as session:
            result = await session.execute(
                update(Submission)
                .where(
                    Submission.id == record.submission_id,
                    Submission.cleanup_claim_token == token,
                    Submission.cleanup_claimed_at.is_not(None),
                    Submission.deleted_at.is_(None),
                )
                .values(
                    cleanup_claimed_at=None,
                    cleanup_lease_expires_at=None,
                    cleanup_claim_token=None,
                )
            )
            # cast: UPDATE statements hand back a CursorResult whose
            # rowcount is the matched-row count.
            matched = cast("CursorResult[Any]", result).rowcount
            if matched == 0:
                logger.info(
                    "file_cleanup.fenced_release_abandoned",
                    extra={
                        "submission_id": str(record.submission_id),
                        "object_key": record.object_key,
                    },
                )
            await session.commit()

    async def mark_deleted(
        self, record: FileRecord, *, token: UUID, deleted_at: datetime
    ) -> MarkOutcome:
        """Idempotent ``deleted_at`` compare-and-set, fenced on the
        claim's ownership token: the UPDATE matches only the row this
        delivery's claim still owns (``cleanup_claim_token = token``).

        Outcomes: a live owned row records the instant (MARKED); a row
        already marked deleted answers ALREADY_DELETED without
        overwriting the earlier instant (whoever marked it — a racing
        delivery or the takeover); a row whose token is no longer this
        delivery's answers CLAIM_LOST — the lease expired mid-flight, a
        takeover rewrote the token, and the completion belongs to IT
        (logged here, never written by this stale worker). Business
        metadata, validation reports, and audit rows are never touched
        (§13)."""
        from sqlalchemy import select, update

        from app.modules.submissions.models import Submission

        async with self._session_maker() as session:
            result = await session.execute(
                update(Submission)
                .where(
                    Submission.id == record.submission_id,
                    Submission.cleanup_claim_token == token,
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
            observed_token, observed_deleted = (
                await session.execute(
                    select(
                        Submission.cleanup_claim_token,
                        Submission.deleted_at,
                    ).where(Submission.id == record.submission_id)
                )
            ).one()
            await session.commit()
            if observed_deleted is not None:
                return MarkOutcome.ALREADY_DELETED
            if observed_token != token:
                # Fenced out: a takeover owns the claim now and will
                # resolve the completion on its own claim. Record and
                # abandon — never write over a live successor (ABA).
                logger.info(
                    "file_cleanup.fenced_mark_abandoned",
                    extra={
                        "submission_id": str(record.submission_id),
                        "object_key": record.object_key,
                    },
                )
                return MarkOutcome.CLAIM_LOST
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
        belongs to the Submission row and the retention pipeline. A
        live cleanup lease also excludes the row (pass 5a); an EXPIRED
        lease re-candidates it — the claiming worker died between claim
        and delete, and this scan takes over."""
        from sqlalchemy import or_, select

        from app.modules.submissions.models import UploadIntent

        async with self._session_maker() as session:
            rows = (
                await session.execute(
                    select(UploadIntent.id, UploadIntent.object_key)
                    .where(
                        UploadIntent.expires_at <= now,
                        UploadIntent.finalized_submission_id.is_(None),
                        UploadIntent.cleanup_deleted_at.is_(None),
                        or_(
                            UploadIntent.cleanup_claimed_at.is_(None),
                            UploadIntent.cleanup_lease_expires_at <= now,
                        ),
                    )
                    .order_by(UploadIntent.expires_at, UploadIntent.id)
                    .limit(limit)
                )
            ).all()
        return [
            IntentRecord(intent_id=intent_id, object_key=object_key)
            for intent_id, object_key in rows
        ]

    async def claim_intent(self, intent: IntentRecord, *, now: datetime) -> UUID | None:
        """Claim one intent for deletion — the return value is the
        OWNERSHIP TOKEN this claim wrote, or None when the claim was
        lost. ONE conditional UPDATE re-evaluating every guard
        (``expires_at <= now``, never finalized, not done, no live lease
        — unclaimed, or claimed with an expired lease, which this
        takeover RESETS to a fresh lease) and setting
        ``cleanup_claimed_at``,
        ``cleanup_lease_expires_at``, and a fresh ``cleanup_claim_token``
        (final pass A: every claim and every takeover rewrites the
        token); ``cleanup_deleted_at`` stays NULL until
        ``mark_intent_deleted`` records the completion (pass 5a splits
        the pass-4b combined column). Finalize cannot lose to this
        claim: it takes the intent-row FOR UPDATE before its own expiry
        check, so the two serialize on the row; intents carry no
        protection transition, so the single statement is their whole
        serialization boundary."""
        from sqlalchemy import or_, update

        from app.modules.submissions.models import UploadIntent

        token = uuid4()
        async with self._session_maker() as session:
            result = await session.execute(
                update(UploadIntent)
                .where(
                    UploadIntent.id == intent.intent_id,
                    UploadIntent.expires_at <= now,
                    UploadIntent.finalized_submission_id.is_(None),
                    UploadIntent.cleanup_deleted_at.is_(None),
                    or_(
                        UploadIntent.cleanup_claimed_at.is_(None),
                        UploadIntent.cleanup_lease_expires_at <= now,
                    ),
                )
                .values(
                    cleanup_claimed_at=now,
                    cleanup_lease_expires_at=self._lease_expiry(now),
                    cleanup_claim_token=token,
                )
                .returning(UploadIntent.id)
            )
            claimed = result.scalar_one_or_none() is not None
            await session.commit()
            return token if claimed else None

    async def mark_intent_deleted(
        self, intent: IntentRecord, *, token: UUID, deleted_at: datetime
    ) -> None:
        """Record the deletion's completion (the DONE marker, pass 5a),
        fenced on the claim's ownership token: set ``cleanup_deleted_at``
        only while NULL AND the row still carries ``token`` — idempotent
        for the owner, so a redelivered job or the takeover after a
        delete-that-already-happened marks at most one instant and never
        overwrites one. A stale worker whose token a takeover rewrote
        matches zero rows: the done marker is logged and abandoned, and
        the takeover records the completion on its own claim (ABA
        closure, the same fencing as the submissions side)."""
        from sqlalchemy import select, update

        from app.modules.submissions.models import UploadIntent

        async with self._session_maker() as session:
            result = await session.execute(
                update(UploadIntent)
                .where(
                    UploadIntent.id == intent.intent_id,
                    UploadIntent.cleanup_claim_token == token,
                    UploadIntent.cleanup_deleted_at.is_(None),
                )
                .values(cleanup_deleted_at=deleted_at)
            )
            # cast: UPDATE statements hand back a CursorResult whose
            # rowcount is the matched-row count.
            matched = cast("CursorResult[Any]", result).rowcount
            if matched == 0:
                observed_token, observed_done = (
                    await session.execute(
                        select(
                            UploadIntent.cleanup_claim_token,
                            UploadIntent.cleanup_deleted_at,
                        ).where(UploadIntent.id == intent.intent_id)
                    )
                ).one()
                if observed_done is None and observed_token != token:
                    logger.info(
                        "file_cleanup.fenced_intent_mark_abandoned",
                        extra={"intent_id": str(intent.intent_id)},
                    )
            await session.commit()

    async def release_intent(self, intent: IntentRecord, *, token: UUID) -> None:
        """Release the intent claim after a provider failure: clear all
        three claim columns so the next scan retries (the done marker
        stays NULL — the deletion never settled). Fenced on the token:
        a stale worker whose row a takeover re-claimed matches zero
        rows, logs, and abandons — the takeover's live claim is
        untouched (ABA closure)."""
        from sqlalchemy import update

        from app.modules.submissions.models import UploadIntent

        async with self._session_maker() as session:
            result = await session.execute(
                update(UploadIntent)
                .where(
                    UploadIntent.id == intent.intent_id,
                    UploadIntent.cleanup_claim_token == token,
                    UploadIntent.cleanup_claimed_at.is_not(None),
                )
                .values(
                    cleanup_claimed_at=None,
                    cleanup_lease_expires_at=None,
                    cleanup_claim_token=None,
                )
            )
            # cast: UPDATE statements hand back a CursorResult whose
            # rowcount is the matched-row count.
            matched = cast("CursorResult[Any]", result).rowcount
            if matched == 0:
                logger.info(
                    "file_cleanup.fenced_intent_release_abandoned",
                    extra={"intent_id": str(intent.intent_id)},
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

        settings = get_settings()
        repository = SubmissionCleanupRepository(
            session_maker, lease_seconds=settings.cleanup_claim_lease_seconds
        )
        storage = S3ObjectStorage(settings)
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
