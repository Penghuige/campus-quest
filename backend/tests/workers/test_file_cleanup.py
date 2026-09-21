# backend/tests/workers/test_file_cleanup.py
"""File-retention cleanup worker tests (plan 07 T7; spec §13, §27).

Pure in-memory: the cleanup logic runs against an in-memory
``CleanupRepository`` (mirroring the merge target's due predicate) and
``FakeObjectStorage`` — exactly the two seams the merge-wired Celery
job will compose. No PostgreSQL and no broker are touched, so this file
carries no ``integration`` mark (unlike its siblings, which run against
the real database).

The Submission model lives on the plans branch, not here; the branch
contract pinned below: the placeholder production repository reads
NOTHING (composition is a safe no-op until the merge wires the real
Submission query), while the scan / guard / delete / reconcile /
idempotency logic it will call is final.

Coverage per the brief:
- Retention matrix: expired 30/90/180-day objects deleted; future,
  permanent, legal-hold, and under-review (protected) files retained —
  both via repo-level exclusion and via the service-level guards that
  must survive a stale or buggy listing (§27 不得误删).
- Missing object (§27): a row the database believes present reconciles
  with a warning and keeps its metadata; a row already marked deleted is
  an idempotent success with no warning.
- Idempotency: a second run over the same state issues zero additional
  provider delete calls (pinned by the fake's call recording).
- Failure taxonomy: temporary / permanent / unknown provider errors
  leave the record untouched, count exactly one failure, and never
  retry in-process — a single programmed outage staying effective
  proves the one-attempt rule; the next run retries or self-heals via
  the object-first ordering.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.modules.files.cleanup_service import (
    CleanupSummary,
    FileCleanupOutcome,
    FileRecord,
    MarkOutcome,
    cleanup_expired_file,
    cleanup_expired_files,
)
from app.workers.jobs.cleanup_files import PlaceholderCleanupRepository
from tests.fakes.integrations import FakeObjectStorage

NOW = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
CLAIM_ID = UUID("12345678-1234-5678-1234-567812345678")
TTL = timedelta(minutes=5)


# --- in-memory repository (the merge target's predicate, mirrored) -------------------


@dataclass
class Row:
    """Mutable live database row; the service only ever sees snapshots."""

    submission_id: UUID
    object_key: str
    retention_until: datetime | None
    permanent: bool = False
    legal_hold: bool = False
    protected: bool = False
    deleted_at: datetime | None = None


class InMemoryCleanupRepository:
    """Due-candidate discovery + idempotent ``mark_deleted`` in memory.

    ``collect_due_files`` mirrors the real query's exclusion set (spec
    §13/§27): permanent rows, legal-hold rows, protected rows (claims in
    review states), not-yet-due rows, and rows already marked deleted
    never become candidates. With ``stale_listing=True`` it yields every
    row untouched — the stale/buggy-listing shape the service-level
    guards must survive on their own.

    ``mark_deleted`` is the idempotent compare-and-set the racing
    redelivery path needs: an already-marked row answers ALREADY_DELETED
    without overwriting the earlier instant.
    """

    def __init__(self, rows: list[Row], *, stale_listing: bool = False) -> None:
        self.rows = rows
        self.stale_listing = stale_listing
        self.mark_calls: list[str] = []

    def snapshot(self, row: Row) -> FileRecord:
        return FileRecord(
            submission_id=row.submission_id,
            object_key=row.object_key,
            retention_until=row.retention_until,
            permanent=row.permanent,
            legal_hold=row.legal_hold,
            protected=row.protected,
            deleted_at=row.deleted_at,
        )

    def row_for(self, object_key: str) -> Row:
        return next(row for row in self.rows if row.object_key == object_key)

    async def collect_due_files(
        self, now: datetime, *, limit: int = 500
    ) -> list[FileRecord]:
        if self.stale_listing:
            due = self.rows
        else:
            due = [
                row
                for row in self.rows
                if not row.permanent
                and not row.legal_hold
                and not row.protected
                and row.deleted_at is None
                and row.retention_until is not None
                and row.retention_until <= now
            ]
        return [self.snapshot(row) for row in due[:limit]]

    async def mark_deleted(
        self, record: FileRecord, *, deleted_at: datetime
    ) -> MarkOutcome:
        self.mark_calls.append(record.object_key)
        row = self.row_for(record.object_key)
        if row.deleted_at is not None:
            return MarkOutcome.ALREADY_DELETED
        row.deleted_at = deleted_at
        return MarkOutcome.MARKED


def _stored_object(storage: FakeObjectStorage, *, size: int = 1024) -> str:
    """Seed one object the fake really holds (upload URL + completed PUT)."""
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    storage.put_object(object_key=url.object_key, size=size)
    return url.object_key


def _absent_object(storage: FakeObjectStorage) -> str:
    """A key whose upload intent was issued but the object never landed."""
    url = storage.create_upload_url(
        claim_id=CLAIM_ID, content_type="text/csv", expires_in=TTL
    )
    return url.object_key


def _row(
    *,
    object_key: str,
    retention_until: datetime | None,
    **flags: Any,
) -> Row:
    return Row(
        submission_id=uuid.uuid4(),
        object_key=object_key,
        retention_until=retention_until,
        **flags,
    )


# --- signature pins ------------------------------------------------------------------


def test_cleanup_signature_shape_matches_worker_composition() -> None:
    # §12 composition: the scan takes the instant plus its two seams,
    # with the batch size keyword-only; the per-record unit takes the
    # snapshot plus keyword-only seams.
    scan_parameters = inspect.signature(cleanup_expired_files).parameters
    assert list(scan_parameters) == ["now", "repo", "storage", "limit"]
    assert scan_parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    single_parameters = inspect.signature(cleanup_expired_file).parameters
    assert list(single_parameters) == ["record", "repo", "storage", "now"]
    assert all(
        single_parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("repo", "storage", "now")
    )


def test_retention_snapshot_xor_permanent_is_carried_on_the_record() -> None:
    # §13: retention_until snapshot XOR the explicit permanent flag —
    # permanent is a real marker, never a "very large date".
    snapshot = FileRecord(
        submission_id=uuid.uuid4(),
        object_key="submissions/x/y",
        retention_until=NOW - timedelta(days=30),
    )
    assert snapshot.permanent is False
    permanent = FileRecord(
        submission_id=uuid.uuid4(),
        object_key="submissions/x/z",
        retention_until=None,
        permanent=True,
    )
    assert permanent.retention_until is None


# --- retention matrix ----------------------------------------------------------------


async def test_retention_matrix_deletes_only_expired_unprotected_objects() -> None:
    """Expired 30/90/180-day objects are deleted; future, permanent,
    legal-hold, and under-review objects survive untouched (spec §13)."""
    storage = FakeObjectStorage()
    key_30 = _stored_object(storage)
    key_90 = _stored_object(storage)
    key_180 = _stored_object(storage)
    future_key = _stored_object(storage)
    permanent_key = _stored_object(storage)
    legal_hold_key = _stored_object(storage)
    under_review_key = _stored_object(storage)
    repo = InMemoryCleanupRepository(
        [
            _row(object_key=key_30, retention_until=NOW - timedelta(days=30)),
            _row(object_key=key_90, retention_until=NOW - timedelta(days=90)),
            _row(object_key=key_180, retention_until=NOW - timedelta(days=180)),
            _row(object_key=future_key, retention_until=NOW + timedelta(days=1)),
            _row(object_key=permanent_key, retention_until=None, permanent=True),
            _row(
                object_key=legal_hold_key,
                retention_until=NOW - timedelta(days=30),
                legal_hold=True,
            ),
            _row(
                object_key=under_review_key,
                retention_until=NOW - timedelta(days=30),
                protected=True,
            ),
        ]
    )

    summary = await cleanup_expired_files(NOW, repo, storage)

    assert summary == CleanupSummary(scanned=3, deleted=3)
    assert storage.deleted_keys == [key_30, key_90, key_180]
    assert repo.row_for(key_30).deleted_at == NOW
    assert repo.row_for(key_90).deleted_at == NOW
    assert repo.row_for(key_180).deleted_at == NOW
    # Retained: object still exists in storage and the row stays present.
    for retained in (future_key, permanent_key, legal_hold_key, under_review_key):
        assert storage.head_object(object_key=retained) is not None
        assert repo.row_for(retained).deleted_at is None


async def test_service_guards_survive_stale_listing() -> None:
    """A stale or buggy listing that mis-yields protected rows must
    still delete nothing: every §27 guard is re-checked per record in
    the service, not only in the candidate query."""
    storage = FakeObjectStorage()
    permanent_key = _stored_object(storage)
    xor_violation_key = _stored_object(storage)
    legal_hold_key = _stored_object(storage)
    under_review_key = _stored_object(storage)
    future_key = _stored_object(storage)
    repo = InMemoryCleanupRepository(
        [
            _row(object_key=permanent_key, retention_until=None, permanent=True),
            # XOR-violating snapshot: permanent wins — never delete.
            _row(
                object_key=xor_violation_key,
                retention_until=NOW - timedelta(days=30),
                permanent=True,
            ),
            _row(
                object_key=legal_hold_key,
                retention_until=NOW - timedelta(days=30),
                legal_hold=True,
            ),
            _row(
                object_key=under_review_key,
                retention_until=NOW - timedelta(days=30),
                protected=True,
            ),
            _row(object_key=future_key, retention_until=NOW + timedelta(days=1)),
        ],
        stale_listing=True,
    )

    summary = await cleanup_expired_files(NOW, repo, storage)

    assert summary == CleanupSummary(
        scanned=5,
        skipped={
            FileCleanupOutcome.SKIPPED_PERMANENT.value: 2,
            FileCleanupOutcome.SKIPPED_LEGAL_HOLD.value: 1,
            FileCleanupOutcome.SKIPPED_UNDER_REVIEW.value: 1,
            FileCleanupOutcome.SKIPPED_RETENTION_NOT_DUE.value: 1,
        },
    )
    assert storage.deleted_keys == []
    assert repo.mark_calls == []
    for row in repo.rows:
        assert row.deleted_at is None
        assert storage.head_object(object_key=row.object_key) is not None


# --- §27 missing-object branches ------------------------------------------------------


async def test_missing_object_with_present_state_reconciles_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Storage 404s a row the database believes present: the state is
    fixed (deleted_at set), a reconcile warning is recorded, and the
    Submission metadata row itself is preserved — only deleted_at
    moves."""
    storage = FakeObjectStorage()
    missing_key = _absent_object(storage)
    row = _row(object_key=missing_key, retention_until=NOW - timedelta(days=30))
    repo = InMemoryCleanupRepository([row])

    with caplog.at_level(logging.WARNING, logger="app.modules.files.cleanup_service"):
        summary = await cleanup_expired_files(NOW, repo, storage)

    assert summary == CleanupSummary(scanned=1, reconciled=1)
    warnings = [
        record
        for record in caplog.records
        if record.message == "file_cleanup.reconcile_missing_object"
    ]
    assert len(warnings) == 1
    assert warnings[0].object_key == missing_key
    assert repo.mark_calls == [missing_key]
    # State fixed, metadata preserved: the row survives with only
    # deleted_at changed.
    assert row.deleted_at == NOW
    assert row.retention_until == NOW - timedelta(days=30)
    assert row.legal_hold is False


async def test_missing_object_already_marked_deleted_is_idempotent_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§27 branch 1: a redelivered job replays a STALE snapshot (the
    racing worker already deleted the object and marked the row). The
    missing object is a success, not a reconcile — no warning, and the
    earlier deleted_at instant is not overwritten."""
    storage = FakeObjectStorage()
    gone_key = _absent_object(storage)
    earlier_instant = NOW - timedelta(minutes=5)
    row = _row(object_key=gone_key, retention_until=NOW - timedelta(days=30))
    row.deleted_at = earlier_instant
    repo = InMemoryCleanupRepository([row])
    stale_snapshot = FileRecord(
        submission_id=row.submission_id,
        object_key=gone_key,
        retention_until=row.retention_until,
    )

    with caplog.at_level(logging.WARNING, logger="app.modules.files.cleanup_service"):
        outcome = await cleanup_expired_file(
            stale_snapshot, repo=repo, storage=storage, now=NOW
        )

    assert outcome is FileCleanupOutcome.ALREADY_DELETED
    assert caplog.records == []
    assert row.deleted_at == earlier_instant


async def test_deleted_object_with_already_marked_row_counts_once() -> None:
    """A successful provider delete whose mark answers ALREADY_DELETED
    (the racing delivery completed both steps) reports idempotent
    success — the file is never counted as deleted twice."""
    storage = FakeObjectStorage()
    present_key = _stored_object(storage)
    earlier_instant = NOW - timedelta(minutes=5)
    row = _row(object_key=present_key, retention_until=NOW - timedelta(days=30))
    row.deleted_at = earlier_instant
    repo = InMemoryCleanupRepository([row])
    stale_snapshot = FileRecord(
        submission_id=row.submission_id,
        object_key=present_key,
        retention_until=row.retention_until,
    )

    outcome = await cleanup_expired_file(
        stale_snapshot, repo=repo, storage=storage, now=NOW
    )

    assert outcome is FileCleanupOutcome.ALREADY_DELETED
    assert storage.deleted_keys == [present_key]
    assert row.deleted_at == earlier_instant


# --- idempotency ----------------------------------------------------------------------


async def test_second_run_makes_no_additional_delete_calls() -> None:
    """A second pass over the same state issues ZERO additional provider
    delete calls: already-deleted rows left the candidate set, so the
    fake's call count stays at the first run's total."""
    storage = FakeObjectStorage()
    keys = [_stored_object(storage) for _ in range(3)]
    repo = InMemoryCleanupRepository(
        [_row(object_key=key, retention_until=NOW - timedelta(days=30)) for key in keys]
    )

    first = await cleanup_expired_files(NOW, repo, storage)
    assert first == CleanupSummary(scanned=3, deleted=3)
    assert len(storage.deleted_keys) == 3

    second = await cleanup_expired_files(NOW, repo, storage)

    assert second == CleanupSummary()
    assert storage.deleted_keys == keys
    assert repo.mark_calls == keys  # no second mark either


# --- failure taxonomy -----------------------------------------------------------------


async def test_transient_provider_failure_is_counted_and_retryable() -> None:
    """A temporary outage leaves the record untouched and counts one
    failure; the next run retries and completes (§13 删除失败可重试)."""
    storage = FakeObjectStorage()
    key = _stored_object(storage)
    repo = InMemoryCleanupRepository(
        [_row(object_key=key, retention_until=NOW - timedelta(days=30))]
    )
    storage.fail_with(TemporaryProviderError("s3 throttled"))

    failed_run = await cleanup_expired_files(NOW, repo, storage)

    assert failed_run == CleanupSummary(
        scanned=1,
        failed={FileCleanupOutcome.FAILED_STORAGE_TEMPORARY.value: 1},
    )
    assert repo.row_for(key).deleted_at is None
    assert storage.head_object(object_key=key) is not None
    # The single programmed outage stayed effective evidence: had the
    # service retried in-process, that attempt would have deleted.
    assert storage.deleted_keys == []

    retried_run = await cleanup_expired_files(NOW, repo, storage)

    assert retried_run == CleanupSummary(scanned=1, deleted=1)
    assert storage.deleted_keys == [key]
    assert repo.row_for(key).deleted_at == NOW


async def test_permanent_provider_failure_is_counted_without_retry_loops() -> None:
    """A permanent provider rejection counts one failure with its
    reason and stops — exactly one provider attempt, no in-process
    retry loop; the record stays present for operator attention."""
    storage = FakeObjectStorage()
    key = _stored_object(storage)
    repo = InMemoryCleanupRepository(
        [_row(object_key=key, retention_until=NOW - timedelta(days=30))]
    )
    storage.fail_with(PermanentProviderError("access denied"))

    summary = await cleanup_expired_files(NOW, repo, storage)

    assert summary == CleanupSummary(
        scanned=1,
        failed={FileCleanupOutcome.FAILED_STORAGE_PERMANENT.value: 1},
    )
    assert repo.row_for(key).deleted_at is None
    # Single programmed outage, single attempt: an in-process retry
    # would have consumed it and deleted the object.
    assert storage.head_object(object_key=key) is not None
    assert storage.deleted_keys == []


async def test_unknown_outcome_is_counted_and_self_heals_next_run() -> None:
    """An unknown-outcome timeout counts one failure and leaves the
    record; object-first ordering makes the next run safe either way —
    here the object survived, so it deletes normally."""
    storage = FakeObjectStorage()
    key = _stored_object(storage)
    repo = InMemoryCleanupRepository(
        [_row(object_key=key, retention_until=NOW - timedelta(days=30))]
    )
    storage.fail_with(UnknownOutcomeError("provider timeout"))

    failed_run = await cleanup_expired_files(NOW, repo, storage)

    assert failed_run == CleanupSummary(
        scanned=1,
        failed={FileCleanupOutcome.FAILED_STORAGE_UNKNOWN.value: 1},
    )
    assert repo.row_for(key).deleted_at is None

    healed_run = await cleanup_expired_files(NOW, repo, storage)

    assert healed_run == CleanupSummary(scanned=1, deleted=1)
    assert repo.row_for(key).deleted_at == NOW


# --- summary consistency + branch placeholder -----------------------------------------


async def test_summary_counts_partition_the_scan() -> None:
    """Mixed run: every scanned record lands in exactly one bucket."""
    storage = FakeObjectStorage()
    deleted_a = _stored_object(storage)
    deleted_b = _stored_object(storage)
    retained_future = _stored_object(storage)
    retained_hold = _stored_object(storage)
    missing_key = _absent_object(storage)
    throttled_key = _stored_object(storage)
    repo = InMemoryCleanupRepository(
        [
            _row(object_key=deleted_a, retention_until=NOW - timedelta(days=30)),
            _row(object_key=deleted_b, retention_until=NOW - timedelta(days=180)),
            _row(object_key=retained_future, retention_until=NOW + timedelta(days=90)),
            _row(
                object_key=retained_hold,
                retention_until=NOW - timedelta(days=30),
                legal_hold=True,
            ),
            _row(object_key=missing_key, retention_until=NOW - timedelta(days=90)),
            # Last candidate: consumes the single programmed outage.
            _row(object_key=throttled_key, retention_until=NOW - timedelta(days=30)),
        ]
    )
    storage.fail_with(TemporaryProviderError("s3 throttled"))

    summary = await cleanup_expired_files(NOW, repo, storage)

    assert summary == CleanupSummary(
        scanned=4,
        deleted=2,
        reconciled=1,
        failed={FileCleanupOutcome.FAILED_STORAGE_TEMPORARY.value: 1},
    )
    accounted = (
        summary.deleted
        + summary.reconciled
        + summary.already_deleted
        + sum(summary.skipped.values())
        + sum(summary.failed.values())
    )
    assert accounted == summary.scanned


async def test_placeholder_repository_reads_nothing() -> None:
    """Branch contract: the default production repository yields no
    candidates (safe no-op composition) and never claims a mark."""
    storage = FakeObjectStorage()
    summary = await cleanup_expired_files(NOW, PlaceholderCleanupRepository(), storage)

    assert summary == CleanupSummary()
    assert storage.deleted_keys == []

    record = FileRecord(
        submission_id=uuid.uuid4(),
        object_key="submissions/claim/never",
        retention_until=NOW - timedelta(days=30),
    )
    with pytest.raises(RuntimeError, match="PlaceholderCleanupRepository"):
        await PlaceholderCleanupRepository().mark_deleted(record, deleted_at=NOW)
