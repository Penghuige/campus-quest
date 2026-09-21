# backend/app/modules/files/cleanup_service.py
"""File-retention cleanup service (plan 07 T7; spec §13, §27;
backend-engineering §12).

Worker owns orchestration, service owns judgement: the Celery shell
(``app/workers/jobs/cleanup_files.py``) samples the clock, constructs
the repository and the object-storage adapter, and calls
``cleanup_expired_files``. Every delete / retain decision lives here.

The worker consumes file state through two seams:

- ``FileRecord`` — the retention snapshot of ONE stored object: the
  ``retention_until`` snapshot XOR the explicit ``permanent`` flag (§13:
  permanent retention is a real marker, never a "very large date"), the
  ``legal_hold`` and under-review ``protected`` flags, and
  ``deleted_at`` (None while the database believes the object present).
- ``CleanupRepository`` — due-candidate discovery plus the idempotent
  ``mark_deleted`` compare-and-set. The real query excludes permanent,
  legal-hold, protected (claims in review states, §27 不得误删尚在审核
  中的文件), not-yet-due, and already-deleted rows BY CONSTRUCTION; the
  service re-validates every guard per record anyway, so a stale or
  buggy listing can never reach the provider delete.

Ordering (§13): the object is deleted FIRST, then the database state is
updated. Business metadata, validation reports, and audit records are
never removed — ``mark_deleted`` only records the deletion instant on
the same row.

Idempotency (§27), absorbed at three layers:

1. By construction — already-deleted rows leave the candidate set, so a
   re-run over the same state issues zero provider delete calls.
2. Compare-and-set — a redelivered job holding a STALE snapshot that
   finds the object already gone and the row already marked answers
   ALREADY_DELETED: a plain success, no warning.
3. Reconcile — the object is gone while the database believed it
   present: the state is fixed and a warning is recorded (§27).

Provider failures follow the adapter taxonomy
(``app/integrations/errors.py``): temporary / unknown outcomes are
retryable by the NEXT run (object-first ordering makes the retry safe
either way — a delete that actually happened surfaces as reconcile or
ALREADY_DELETED), permanent rejections are terminal for the record.
None of them retry in-process; each counts exactly one failure with its
reason, and the record stays untouched.

Unexpected exceptions (a repository/DB failure or a storage bug outside
the taxonomy) are NOT swallowed: they propagate so redelivery and
operators see them, mirroring the claim-expiry jobs' loud-bug rule.
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


class MarkOutcome(StrEnum):
    """Result of the idempotent ``mark_deleted`` compare-and-set."""

    MARKED = "MARKED"
    ALREADY_DELETED = "ALREADY_DELETED"


class CleanupRepository(Protocol):
    """Port for due-file discovery and deletion-state updates.

    The production implementation is ``SubmissionCleanupRepository`` in
    ``app/workers/jobs/cleanup_files.py`` (MERGE_CARRIES item 3, wired
    at the merge); tests drive the service with in-memory fakes.
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
        service re-validates every guard per record regardless.
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


def _guard(record: FileRecord, now: datetime) -> FileCleanupOutcome | None:
    """The §13/§27 retain guards, re-checked per record.

    Order is fail-safe: ``permanent`` first (an XOR-violating snapshot
    with both markers set is still retained), then legal hold, then the
    under-review protection, then the retention snapshot itself — a
    missing snapshot (``None``) is NEVER deletable.
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

    Guards first (a stale listing deletes nothing), then the provider
    delete, then the database state. A missing object splits on the
    repository's compare-and-set: already marked = idempotent success
    (§27 branch 1); believed present = reconcile with a warning (§27
    branch 2).
    """
    skipped = _guard(record, now)
    if skipped is not None:
        return skipped

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
        return FileCleanupOutcome.FAILED_STORAGE_TEMPORARY
    except PermanentProviderError:
        return FileCleanupOutcome.FAILED_STORAGE_PERMANENT
    except UnknownOutcomeError:
        # The delete may or may not have happened; object-first ordering
        # makes the next run converge (present -> delete again, gone ->
        # reconcile / already-deleted). No in-process retry.
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
