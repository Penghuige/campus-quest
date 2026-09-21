# backend/app/workers/jobs/cleanup_files.py
"""File-retention cleanup worker home (plan 07 T7; spec §13, §27).

On this branch the Submission model does not exist (it lives on the
plans stream), so the production ``CleanupRepository`` is a PLACEHOLDER
that reads nothing: composing the future job with it is a safe no-op.
The cleanup logic it will call is final already —
``app.modules.files.cleanup_service.cleanup_expired_files`` (guards,
object-first delete, §27 reconcile/idempotency, failure taxonomy) —
fully covered by ``tests/workers/test_file_cleanup.py`` against an
in-memory repository and ``FakeObjectStorage``.

The plan-07 merge completes this module: replace the placeholder with
the real Submission query (due retention snapshots joined to claims,
excluding in-review claims, legal holds, permanent rows, and rows
already marked deleted), register the Celery shell here (scan task:
sample SystemClock, build repository + object-storage adapter, call the
service, return the JSON summary), append this module to ``JOB_MODULES``
(also updating the pinned task list in
``tests/workers/test_celery_wiring.py``), and add the beat schedule.

Importing this module stays dependency-free (celery_app
lazy-construction contract): no sqlalchemy, no app.db, no settings.
"""

from __future__ import annotations

from datetime import datetime

from app.modules.files.cleanup_service import (
    CLEANUP_BATCH_LIMIT,
    FileRecord,
    MarkOutcome,
)


class PlaceholderCleanupRepository:
    """Default ``CleanupRepository`` on this branch — reads NOTHING.

    ``collect_due_files`` yields no candidates, so any composition
    built over it (now, and until the merge wires the real Submission
    query) is a verifiable no-op instead of a half query over tables
    that do not exist here.
    """

    async def collect_due_files(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[FileRecord]:
        """No due files on this branch (no Submission table to read)."""
        return []

    async def mark_deleted(
        self, record: FileRecord, *, deleted_at: datetime
    ) -> MarkOutcome:
        """Unreachable by construction: no candidate is ever yielded."""
        raise RuntimeError(
            "PlaceholderCleanupRepository reads nothing and cannot mark "
            "records deleted; the plan-07 merge wires the real Submission "
            "query into this slot."
        )
