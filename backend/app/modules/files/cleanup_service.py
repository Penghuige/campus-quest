# backend/app/modules/files/cleanup_service.py
"""File-retention cleanup service (plan 07 T7; spec §13, §27;
backend-engineering §12; hardening pass 4b deletion-claim ruling).

Worker owns orchestration, service owns judgement: the Celery shell
(``app/workers/jobs/cleanup_files.py``) samples the clock, constructs
the repository and the object-storage adapter, and calls
``cleanup_expired_files``. Every delete / retain decision lives here.

Deletion is CLAIMED, not snapshot-judged (pass 4b): the §13/§27 retain
guards are re-evaluated by a single conditional UPDATE against CURRENT
committed state, which claims the deletion right
(``cleanup_claimed_at``) in the same statement — guards and claim are
atomic, so a legal_hold or review-pipeline entry that lands between the
scan and the claim makes the claim FAIL and the object survive (the
TOCTOU the boolean-snapshot pipeline had). Only a claimed record
reaches the provider delete; the service-level snapshot guards remain
as the stale-listing defense in front. The protection writers carry
the other half of the ruling (a typed 409 while a claim is in flight —
``app.modules.submissions.cleanup_claim``): protection must win,
deletion is retryable.

Pass 4b also adds the orphan-intent cleanup (``cleanup_orphaned_intents``):
expired intents that never finalized hold stored objects nobody else
will ever remove. Same claim primitive over
``upload_intents.cleanup_deleted_at`` (which doubles as the done
marker), only ever claimed AFTER ``expires_at`` — the presigned URL TTL
is deployed shorter than the intent TTL, so no legal PUT can land after
the intent expired (a deployment invariant this cleanup depends on;
both TTLs are injectable, wired from Settings). Finalized intents are
never touched: their object belongs to the Submission row and the
retention pipeline above.

The worker consumes file state through two seams:

- ``FileRecord`` — the retention snapshot of ONE stored object: the
  ``retention_until`` snapshot XOR the explicit ``permanent`` flag (§13:
  permanent retention is a real marker, never a "very large date"), the
  ``legal_hold`` and under-review ``protected`` flags, and
  ``deleted_at`` (None while the database believes the object present).
- ``CleanupRepository`` — due-candidate discovery, the conditional
  claim/release pair that owns the deletion right, and the idempotent
  ``mark_deleted`` compare-and-set. The real query excludes permanent,
  legal-hold, protected (claims in review states, §27 不得误删尚在审核
  中的文件), not-yet-due, and already-deleted rows BY CONSTRUCTION; the
  service re-validates every guard per record anyway, and the claim
  re-evaluates them atomically, so a stale or buggy listing can never
  reach the provider delete.

Ordering (§13): the object is deleted FIRST, then the database state is
updated. Business metadata, validation reports, and audit records are
never removed — ``mark_deleted`` only records the deletion instant on
the same row. No row lock is ever held across the provider call (the
claim is declarative — that is the whole point of it).

Idempotency (§27), absorbed at three layers:

1. By construction — already-deleted rows leave the candidate set, so a
   re-run over the same state issues zero provider delete calls.
2. Claim exclusivity — a redelivered job holding a STALE snapshot
   cannot claim a row the first delivery already claimed, so it issues
   no provider call at all (SKIPPED_CLAIM_LOST); the compare-and-set
   below stays as the belt-and-braces backstop.
3. Compare-and-set — a claimed record whose object is already gone and
   whose row is already marked answers ALREADY_DELETED: a plain
   success, no warning; believed present, the state is fixed and a
   warning recorded (§27 reconcile).

Provider failures follow the adapter taxonomy
(``app/integrations/errors.py``): temporary / unknown outcomes are
retryable by the NEXT run (object-first ordering makes the retry safe
either way — a delete that actually happened surfaces as reconcile or
ALREADY_DELETED), permanent rejections are terminal for the record.
None of them retry in-process; each counts exactly one failure with its
reason, the CLAIM IS RELEASED (so the next scan can retry and the
protection writers are not blocked on a dead claim), and the record
stays otherwise untouched.

Unexpected exceptions (a repository/DB failure or a storage bug outside
the taxonomy) are NOT swallowed: they propagate so redelivery and
operators see them, mirroring the claim-expiry jobs' loud-bug rule. A
crash between claim and release can strand a claim: the row stays
claimed (never re-claimed, protection 409s) until an operator clears
``cleanup_claimed_at`` — deliberate fail-safe over auto-takeover, which
would re-open exactly the race the claim exists to close.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.object_storage import ObjectStorage

logger = logging.getLogger(__name__)

# One scan batch's ceiling: bounds the per-scan work; the next scheduled
# run picks up whatever a huge backlog leaves behind (same shape as the
# claim-expiry scan).
CLEANUP_BATCH_LIMIT = 500


@dataclass(frozen=True)
class FileRecord:
    """Retention snapshot of one stored file, as the worker consumes it.

    Invariants (spec §13): exactly one of ``retention_until`` /
    ``permanent`` is set on a healthy row. The service's guards are
    fail-safe anyway — ``permanent`` is checked first, so even a
    snapshot violating the XOR (both set) is retained, never deleted.

    ``protected`` is the repo-level under-review predicate (§27): the
    real query excludes claims in review states from the candidate set
    entirely; the flag travels on the record so the service re-checks.
    """

    submission_id: UUID
    object_key: str
    retention_until: datetime | None = None
    permanent: bool = False
    legal_hold: bool = False
    protected: bool = False
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class IntentRecord:
    """One orphan-intent candidate as the worker consumes it: the
    intent's id plus the object key its presigned grant pinned.

    No boolean snapshot travels — every orphan guard (``expires_at``
    past, never finalized, unclaimed) is evaluated only inside the
    conditional claim, which is why there is nothing for a stale
    listing to get wrong here.
    """

    intent_id: UUID
    object_key: str


class MarkOutcome(StrEnum):
    """Result of the idempotent ``mark_deleted`` compare-and-set."""

    MARKED = "MARKED"
    ALREADY_DELETED = "ALREADY_DELETED"


class CleanupRepository(Protocol):
    """Port for due-file discovery, the deletion claim, and
    deletion-state updates.

    The production implementation is ``SubmissionCleanupRepository`` in
    ``app/workers/jobs/cleanup_files.py``; tests drive the service with
    in-memory fakes.
    """

    async def collect_due_files(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[FileRecord]:
        """Snapshots of files due for cleanup at ``now``.

        The real query yields ONLY rows eligible for deletion: retention
        snapshot due (``retention_until <= now``), not ``permanent``,
        not under ``legal_hold``, not protected by an active review
        (claims in review states), and not already marked deleted.
        Exclusion by construction is the first idempotency layer; the
        claim re-evaluates every guard atomically regardless.
        """
        ...

    async def claim_for_cleanup(self, record: FileRecord, *, now: datetime) -> bool:
        """Claim the deletion right over one record.

        ONE conditional UPDATE: the WHERE clause re-evaluates EVERY
        §13/§27 guard against current committed state — not permanent,
        no legal hold, retention still due, not already deleted, the
        claim's status still outside the review pipeline, and no claim
        already held — and the same statement sets
        ``cleanup_claimed_at = now``. ``RETURNING`` decides the answer:
        True only for the winner. Single statement = guards and claim
        are atomic, so a protection that landed since the scan makes
        the claim fail and the object survive.
        """
        ...

    async def release_cleanup_claim(self, record: FileRecord) -> None:
        """Release the claim after a provider failure: clear
        ``cleanup_claimed_at`` so the next scan re-claims (and the
        protection writers are not blocked on a dead claim). The object
        is NOT marked deleted.
        """
        ...

    async def mark_deleted(
        self, record: FileRecord, *, deleted_at: datetime
    ) -> MarkOutcome:
        """Record the deletion instant on the file's own row.

        Idempotent compare-and-set: a row already marked deleted answers
        ``ALREADY_DELETED`` without overwriting the earlier instant; a
        live row records ``deleted_at`` and answers ``MARKED``. Only the
        deletion state changes — business metadata, validation reports,
        and audit records stay untouched (§13).
        """
        ...


class OrphanIntentRepository(Protocol):
    """Port for the orphan-intent cleanup (pass 4b): candidate
    discovery plus the same conditional claim/release primitive over
    ``upload_intents``.

    The production implementation is the same
    ``SubmissionCleanupRepository`` (one repository, two tables), kept
    as a separate seam so the service function states exactly what it
    consumes.
    """

    async def collect_due_intents(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[IntentRecord]:
        """Expired intents that never finalized, oldest expiry first.

        The real query yields ONLY unclaimed rows with
        ``expires_at <= now`` and ``finalized_submission_id IS NULL`` —
        open-expired and burned intents alike; a finalized intent's
        object belongs to its Submission and the retention pipeline.
        """
        ...

    async def claim_intent(self, intent: IntentRecord, *, now: datetime) -> bool:
        """Claim one intent for deletion: a single conditional UPDATE
        re-evaluating every guard (``expires_at <= now``, never
        finalized, unclaimed) and setting ``cleanup_deleted_at = now``
        in the same statement; ``RETURNING`` decides. Finalize can never
        lose to this claim: it holds the intent-row lock before its own
        expiry check, so the two serialize on the row.
        """
        ...

    async def release_intent(self, intent: IntentRecord) -> None:
        """Release the claim after a provider failure (clear
        ``cleanup_deleted_at``); the next scan retries."""
        ...


class FileCleanupOutcome(StrEnum):
    """The per-file decision, every value a normal answer (never an
    exception): skips and provider failures are expected scan results
    under at-least-once delivery."""

    DELETED = "DELETED"
    RECONCILED_MISSING = "RECONCILED_MISSING"
    ALREADY_DELETED = "ALREADY_DELETED"
    SKIPPED_PERMANENT = "SKIPPED_PERMANENT"
    SKIPPED_LEGAL_HOLD = "SKIPPED_LEGAL_HOLD"
    SKIPPED_UNDER_REVIEW = "SKIPPED_UNDER_REVIEW"
    SKIPPED_RETENTION_NOT_DUE = "SKIPPED_RETENTION_NOT_DUE"
    SKIPPED_CLAIM_LOST = "SKIPPED_CLAIM_LOST"
    FAILED_STORAGE_TEMPORARY = "FAILED_STORAGE_TEMPORARY"
    FAILED_STORAGE_PERMANENT = "FAILED_STORAGE_PERMANENT"
    FAILED_STORAGE_UNKNOWN = "FAILED_STORAGE_UNKNOWN"

    @property
    def is_skipped(self) -> bool:
        """True when the file was deliberately retained."""
        return self.value.startswith("SKIPPED_")

    @property
    def is_failure(self) -> bool:
        """True when the file was left untouched by a provider failure."""
        return self.value.startswith("FAILED_")


class OrphanIntentOutcome(StrEnum):
    """The per-intent decision. Deliberately smaller than the file
    side: intents carry no retention policy and no review protection,
    so a candidate is either claimed and deleted (a missing object is
    the idempotent-success shape — most expired intents were never
    uploaded at all), claim-lost, or a provider failure."""

    DELETED = "DELETED"
    MISSING_OBJECT = "MISSING_OBJECT"
    SKIPPED_CLAIM_LOST = "SKIPPED_CLAIM_LOST"
    FAILED_STORAGE_TEMPORARY = "FAILED_STORAGE_TEMPORARY"
    FAILED_STORAGE_PERMANENT = "FAILED_STORAGE_PERMANENT"
    FAILED_STORAGE_UNKNOWN = "FAILED_STORAGE_UNKNOWN"

    @property
    def is_skipped(self) -> bool:
        return self.value.startswith("SKIPPED_")

    @property
    def is_failure(self) -> bool:
        return self.value.startswith("FAILED_")


@dataclass(frozen=True)
class CleanupSummary:
    """One cleanup scan's counts; every scanned file lands in exactly
    one bucket (``deleted + reconciled + already_deleted + skipped +
    failed == scanned``). Skip and failure buckets are keyed by
    ``FileCleanupOutcome`` value."""

    scanned: int = 0
    deleted: int = 0
    reconciled: int = 0
    already_deleted: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict)
    failed: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class OrphanIntentSummary:
    """One orphan-intent scan's counts; every scanned intent lands in
    exactly one bucket (``deleted + missing + skipped + failed ==
    scanned``)."""

    scanned: int = 0
    deleted: int = 0
    missing: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict)
    failed: Mapping[str, int] = field(default_factory=dict)


def _guard(record: FileRecord, now: datetime) -> FileCleanupOutcome | None:
    """The §13/§27 retain guards, re-checked per record.

    Order is fail-safe: ``permanent`` first (an XOR-violating snapshot
    with both markers set is still retained), then legal hold, then the
    under-review protection, then the retention snapshot itself — a
    missing snapshot (``None``) is NEVER deletable. These are the
    stale-listing defense; the deletion authority is the claim.
    """
    if record.permanent:
        return FileCleanupOutcome.SKIPPED_PERMANENT
    if record.legal_hold:
        return FileCleanupOutcome.SKIPPED_LEGAL_HOLD
    if record.protected:
        return FileCleanupOutcome.SKIPPED_UNDER_REVIEW
    if record.retention_until is None or record.retention_until > now:
        return FileCleanupOutcome.SKIPPED_RETENTION_NOT_DUE
    return None


async def cleanup_expired_file(
    record: FileRecord,
    *,
    repo: CleanupRepository,
    storage: ObjectStorage,
    now: datetime,
) -> FileCleanupOutcome:
    """Decide and act on ONE due-file snapshot (object first, §13).

    Snapshot guards first (a stale listing deletes nothing), then the
    CLAIM — the conditional UPDATE that re-evaluates every guard
    atomically against current state; losing the claim (a protection
    landed since the scan, or another delivery holds the row) retains
    the object with SKIPPED_CLAIM_LOST and no provider call. A missing
    object splits on the repository's compare-and-set: already marked
    = idempotent success (§27 branch 1); believed present = reconcile
    with a warning (§27 branch 2). Provider failures release the claim
    so the next scan retries.
    """
    skipped = _guard(record, now)
    if skipped is not None:
        return skipped

    if not await repo.claim_for_cleanup(record, now=now):
        return FileCleanupOutcome.SKIPPED_CLAIM_LOST

    try:
        storage.delete_object(object_key=record.object_key)
    except FileNotFoundError:
        outcome = await repo.mark_deleted(record, deleted_at=now)
        if outcome is MarkOutcome.ALREADY_DELETED:
            return FileCleanupOutcome.ALREADY_DELETED
        logger.warning(
            "file_cleanup.reconcile_missing_object",
            extra={
                "submission_id": str(record.submission_id),
                "object_key": record.object_key,
            },
        )
        return FileCleanupOutcome.RECONCILED_MISSING
    except TemporaryProviderError:
        await repo.release_cleanup_claim(record)
        return FileCleanupOutcome.FAILED_STORAGE_TEMPORARY
    except PermanentProviderError:
        await repo.release_cleanup_claim(record)
        return FileCleanupOutcome.FAILED_STORAGE_PERMANENT
    except UnknownOutcomeError:
        # The delete may or may not have happened; object-first ordering
        # makes the next run converge (present -> delete again, gone ->
        # reconcile / already-deleted). Release the claim so the next
        # run CAN retry; no in-process retry.
        await repo.release_cleanup_claim(record)
        return FileCleanupOutcome.FAILED_STORAGE_UNKNOWN

    outcome = await repo.mark_deleted(record, deleted_at=now)
    if outcome is MarkOutcome.ALREADY_DELETED:
        # A racing delivery completed both steps; this one is the
        # idempotent no-op, not a second deletion.
        return FileCleanupOutcome.ALREADY_DELETED
    return FileCleanupOutcome.DELETED


async def cleanup_expired_files(
    now: datetime,
    repo: CleanupRepository,
    storage: ObjectStorage,
    *,
    limit: int = CLEANUP_BATCH_LIMIT,
) -> CleanupSummary:
    """One retention-cleanup scan: discover due candidates, decide each
    through ``cleanup_expired_file``, and reduce to a ``CleanupSummary``.

    The scan itself writes nothing beyond what each per-file decision
    commits; a record that failed transiently stays a candidate, so the
    next scheduled scan retries it (§13 删除失败可重试) while a re-run
    over already-cleaned state is a no-op by construction.
    """
    scanned = 0
    deleted = 0
    reconciled = 0
    already_deleted = 0
    skipped: dict[str, int] = {}
    failed: dict[str, int] = {}

    for record in await repo.collect_due_files(now, limit=limit):
        scanned += 1
        outcome = await cleanup_expired_file(
            record, repo=repo, storage=storage, now=now
        )
        if outcome is FileCleanupOutcome.DELETED:
            deleted += 1
        elif outcome is FileCleanupOutcome.RECONCILED_MISSING:
            reconciled += 1
        elif outcome is FileCleanupOutcome.ALREADY_DELETED:
            already_deleted += 1
        elif outcome.is_skipped:
            skipped[outcome.value] = skipped.get(outcome.value, 0) + 1
        else:
            failed[outcome.value] = failed.get(outcome.value, 0) + 1

    return CleanupSummary(
        scanned=scanned,
        deleted=deleted,
        reconciled=reconciled,
        already_deleted=already_deleted,
        skipped=skipped,
        failed=failed,
    )


async def cleanup_orphaned_intent(
    intent: IntentRecord,
    *,
    repo: OrphanIntentRepository,
    storage: ObjectStorage,
    now: datetime,
) -> OrphanIntentOutcome:
    """Decide and act on ONE orphan-intent candidate: claim (the guards
    live entirely inside the conditional claim — there is no boolean
    snapshot to trust), then delete, with the claim column doubling as
    the done marker.

    A missing object is the idempotent SUCCESS shape (§27): most
    expired intents were never uploaded, and a re-run over a cleaned
    intent re-claims nothing. Provider failures release the claim for
    the next scan.
    """
    if not await repo.claim_intent(intent, now=now):
        return OrphanIntentOutcome.SKIPPED_CLAIM_LOST

    try:
        storage.delete_object(object_key=intent.object_key)
    except FileNotFoundError:
        return OrphanIntentOutcome.MISSING_OBJECT
    except TemporaryProviderError:
        await repo.release_intent(intent)
        return OrphanIntentOutcome.FAILED_STORAGE_TEMPORARY
    except PermanentProviderError:
        await repo.release_intent(intent)
        return OrphanIntentOutcome.FAILED_STORAGE_PERMANENT
    except UnknownOutcomeError:
        await repo.release_intent(intent)
        return OrphanIntentOutcome.FAILED_STORAGE_UNKNOWN
    return OrphanIntentOutcome.DELETED


async def cleanup_orphaned_intents(
    now: datetime,
    repo: OrphanIntentRepository,
    storage: ObjectStorage,
    *,
    limit: int = CLEANUP_BATCH_LIMIT,
) -> OrphanIntentSummary:
    """One orphan-intent scan: expired never-finalized intents, oldest
    first, each decided through ``cleanup_orphaned_intent``.

    Only intents PAST ``expires_at`` are ever claimed, and finalized
    intents never are; the URL-TTL < intent-TTL deployment invariant
    guarantees no legal PUT can land after that instant (see the module
    docstring).
    """
    scanned = 0
    deleted = 0
    missing = 0
    skipped: dict[str, int] = {}
    failed: dict[str, int] = {}

    for intent in await repo.collect_due_intents(now, limit=limit):
        scanned += 1
        outcome = await cleanup_orphaned_intent(
            intent, repo=repo, storage=storage, now=now
        )
        if outcome is OrphanIntentOutcome.DELETED:
            deleted += 1
        elif outcome is OrphanIntentOutcome.MISSING_OBJECT:
            missing += 1
        elif outcome.is_skipped:
            skipped[outcome.value] = skipped.get(outcome.value, 0) + 1
        else:
            failed[outcome.value] = failed.get(outcome.value, 0) + 1

    return OrphanIntentSummary(
        scanned=scanned,
        deleted=deleted,
        missing=missing,
        skipped=skipped,
        failed=failed,
    )
