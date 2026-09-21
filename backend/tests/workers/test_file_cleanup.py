# backend/tests/workers/test_file_cleanup.py
"""File-retention cleanup worker tests (plan 07 T7; spec §13, §27;
hardening pass 4b deletion-claim ruling; pass 5a claim leases).

Two layers:

- The service logic (scan / guard / claim / delete / reconcile /
  idempotency) runs pure in-memory against an in-memory
  ``CleanupRepository`` (mirroring the real due predicate and the real
  leased claim) and ``FakeObjectStorage`` — no PostgreSQL, no broker.
- The REAL repository (``SubmissionCleanupRepository``, wired at the
  merge per MERGE_CARRIES item 3) runs against real PostgreSQL
  (integration-marked): the due predicate's exclusion set is judged on
  actual rows, ``mark_deleted`` is the idempotent compare-and-set the
  racing-redelivery path relies on, and the pass-4b deletion claim, its
  races, the pass-5a lease/takeover semantics, and the unified
  serialization boundary proofs live in ``test_cleanup_deletion_claim.py``.

Coverage per the brief:
- Retention matrix: expired 30/90/180-day objects deleted; future,
  permanent, legal-hold, and under-review (protected) files retained —
  both via repo-level exclusion and via the service-level guards that
  must survive a stale or buggy listing (§27 不得误删).
- Missing object (§27): a row the database believes present reconciles
  with a warning and keeps its metadata; a row already marked deleted is
  an idempotent success with no warning.
- Idempotency: a second run over the same state issues zero additional
  provider delete calls (pinned by the fake's call recording); the
  claim-exclusivity layer — a redelivered job holding a stale snapshot
  cannot claim a row whose lease is live, so it never reaches the
  provider at all — plus the pass-5a lease mirror: an EXPIRED lease
  re-candidates the row and the takeover claim resets both timestamps.
- Failure taxonomy: temporary / permanent / unknown provider errors
  release the claim, leave the record otherwise untouched, count
  exactly one failure, and never retry in-process — a single programmed
  outage staying effective proves the one-attempt rule; the next run
  re-claims and retries or self-heals via the object-first ordering.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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
from tests.fakes.integrations import FakeObjectStorage

NOW = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
CLAIM_ID = UUID("12345678-1234-5678-1234-567812345678")
TTL = timedelta(minutes=5)
# The claim lease the real repository wires from Settings (the
# cleanup_claim_lease_seconds default); the in-memory mirror must agree
# so its exclusivity horizon matches the real one under test.
LEASE = timedelta(seconds=300)


# --- in-memory repository (the merge target's predicate, mirrored) -------------------


@dataclass
class Row:
    """Mutable live database row; the service only ever sees snapshots.

    ``claim_protected`` mirrors the real claim predicate's join guard
    (the row's AssignmentClaim inside VALIDATING / UNDER_REVIEW) so the
    in-memory claim can re-evaluate it; ``cleanup_claimed_at`` /
    ``cleanup_lease_expires_at`` are the deletion-claim lease the real
    claim transaction sets (pass 5a).
    """

    submission_id: UUID
    object_key: str
    retention_until: datetime | None
    permanent: bool = False
    legal_hold: bool = False
    protected: bool = False
    deleted_at: datetime | None = None
    cleanup_claimed_at: datetime | None = None
    cleanup_lease_expires_at: datetime | None = None
    claim_protected: bool = False


class InMemoryCleanupRepository:
    """Due-candidate discovery + the leased deletion claim (with crash
    takeover) + idempotent ``mark_deleted`` in memory.

    ``collect_due_files`` mirrors the real query's exclusion set (spec
    §13/§27): permanent rows, legal-hold rows, protected rows (claims in
    review states), not-yet-due rows, rows already marked deleted, and
    rows under a LIVE lease never become candidates — a claimed row
    whose lease EXPIRED does (the pass-5a crash takeover; a claimed row
    with a NULL lease stays excluded, the fail-safe reading). With
    ``stale_listing=True`` it yields every row untouched — the
    stale/buggy-listing shape the service-level guards must survive on
    their own.

    ``claim_for_cleanup`` mirrors the real claim's guarded lease: EVERY
    guard (including the claim-status join mirror and the live-lease
    conjunct) is re-evaluated against the row's CURRENT state at claim
    time, and only the winner gets ``cleanup_claimed_at`` +
    ``cleanup_lease_expires_at`` (a takeover resets both — the pass-5a
    semantics the PostgreSQL proofs pin in test_cleanup_deletion_claim.py).

    ``mark_deleted`` is the idempotent compare-and-set the racing
    redelivery path needs: an already-marked row answers ALREADY_DELETED
    without overwriting the earlier instant.
    """

    def __init__(self, rows: list[Row], *, stale_listing: bool = False) -> None:
        self.rows = rows
        self.stale_listing = stale_listing
        self.mark_calls: list[str] = []
        self.claim_calls: list[str] = []

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

    def _claimable(self, row: Row, now: datetime) -> bool:
        lease_live = (
            row.cleanup_claimed_at is not None and row.cleanup_lease_expires_at is None
        ) or (
            row.cleanup_claimed_at is not None
            and row.cleanup_lease_expires_at is not None
            and row.cleanup_lease_expires_at > now
        )
        return (
            not row.permanent
            and not row.legal_hold
            and not row.protected
            and not row.claim_protected
            and row.deleted_at is None
            and not lease_live
            and row.retention_until is not None
            and row.retention_until <= now
        )

    async def collect_due_files(
        self, now: datetime, *, limit: int = 500
    ) -> list[FileRecord]:
        if self.stale_listing:
            due = self.rows
        else:
            due = [row for row in self.rows if self._claimable(row, now)]
        return [self.snapshot(row) for row in due[:limit]]

    async def claim_for_cleanup(self, record: FileRecord, *, now: datetime) -> bool:
        self.claim_calls.append(record.object_key)
        row = self.row_for(record.object_key)
        if not self._claimable(row, now):
            return False
        row.cleanup_claimed_at = now
        row.cleanup_lease_expires_at = now + LEASE
        return True

    async def release_cleanup_claim(self, record: FileRecord) -> None:
        row = self.row_for(record.object_key)
        if row.cleanup_claimed_at is not None and row.deleted_at is None:
            row.cleanup_claimed_at = None
            row.cleanup_lease_expires_at = None

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
    """§27 branch 1, claim-era shape: a redelivered job replays a STALE
    snapshot (the racing worker already claimed, deleted the object, and
    marked the row). The claim's ``cleanup_claimed_at IS NULL`` /
    ``deleted_at IS NULL`` conjuncts refuse the replay FIRST — no
    provider call, no warning, and the earlier deleted_at instant is
    not overwritten."""
    storage = FakeObjectStorage()
    gone_key = _absent_object(storage)
    earlier_instant = NOW - timedelta(minutes=5)
    row = _row(object_key=gone_key, retention_until=NOW - timedelta(days=30))
    row.deleted_at = earlier_instant
    row.cleanup_claimed_at = earlier_instant
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

    assert outcome is FileCleanupOutcome.SKIPPED_CLAIM_LOST
    assert caplog.records == []
    assert row.deleted_at == earlier_instant
    assert storage.deleted_keys == []


async def test_deleted_object_with_already_marked_row_counts_once() -> None:
    """A racing delivery that already completed both steps makes every
    replay claim-lost — the file is never counted as deleted twice and
    the provider delete is never re-issued."""
    storage = FakeObjectStorage()
    present_key = _stored_object(storage)
    earlier_instant = NOW - timedelta(minutes=5)
    row = _row(object_key=present_key, retention_until=NOW - timedelta(days=30))
    row.deleted_at = earlier_instant
    row.cleanup_claimed_at = earlier_instant
    repo = InMemoryCleanupRepository([row])
    stale_snapshot = FileRecord(
        submission_id=row.submission_id,
        object_key=present_key,
        retention_until=row.retention_until,
    )

    outcome = await cleanup_expired_file(
        stale_snapshot, repo=repo, storage=storage, now=NOW
    )

    assert outcome is FileCleanupOutcome.SKIPPED_CLAIM_LOST
    assert storage.deleted_keys == []
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


# --- the deletion claim (pass 4b) -----------------------------------------------------


async def test_claim_reevaluates_guards_against_current_state() -> None:
    """The claim — not the scanned snapshot — is the deletion authority:
    a protection that landed on the ROW after the scan (legal hold, a
    claim entering the review pipeline) makes the claim fail, the
    provider is never called, and the object survives (the TOCTOU the
    boolean-snapshot pipeline had; the two-connection PostgreSQL proof
    is in test_cleanup_deletion_claim.py)."""
    storage = FakeObjectStorage()
    legal_hold_key = _stored_object(storage)
    review_key = _stored_object(storage)
    rows = [
        _row(
            object_key=legal_hold_key,
            retention_until=NOW - timedelta(days=30),
        ),
        _row(
            object_key=review_key,
            retention_until=NOW - timedelta(days=30),
        ),
    ]
    repo = InMemoryCleanupRepository(rows)

    # The SCAN — snapshots taken while both rows are still eligible.
    stale_records = await repo.collect_due_files(NOW)

    # Protections land between the scan and the claim.
    repo.row_for(legal_hold_key).legal_hold = True
    repo.row_for(review_key).claim_protected = True

    scanned = 0
    outcomes: list[FileCleanupOutcome] = []
    for record in stale_records:
        scanned += 1
        outcomes.append(
            await cleanup_expired_file(record, repo=repo, storage=storage, now=NOW)
        )

    assert outcomes == [
        FileCleanupOutcome.SKIPPED_CLAIM_LOST,
        FileCleanupOutcome.SKIPPED_CLAIM_LOST,
    ]
    assert scanned == 2
    assert storage.deleted_keys == []
    assert repo.mark_calls == []
    for row in rows:
        assert row.deleted_at is None
        assert row.cleanup_claimed_at is None
        assert storage.head_object(object_key=row.object_key) is not None


async def test_provider_failure_releases_claim_for_the_next_scan() -> None:
    """Every non-FileNotFoundError provider outcome RELEASES the claim:
    the row is claimable again (the next scan re-claims and retries) and
    a protection writer is never blocked on a dead claim."""
    storage = FakeObjectStorage()
    key = _stored_object(storage)
    row = _row(object_key=key, retention_until=NOW - timedelta(days=30))
    repo = InMemoryCleanupRepository([row])
    storage.fail_with(TemporaryProviderError("s3 throttled"))

    failed_run = await cleanup_expired_files(NOW, repo, storage)

    assert failed_run == CleanupSummary(
        scanned=1,
        failed={FileCleanupOutcome.FAILED_STORAGE_TEMPORARY.value: 1},
    )
    # Released, not stranded: the claim window closed with the failure.
    assert row.cleanup_claimed_at is None
    assert row.deleted_at is None

    retried_run = await cleanup_expired_files(NOW, repo, storage)

    assert retried_run == CleanupSummary(scanned=1, deleted=1)
    # The claim stays set after the COMPLETED deletion (deleted_at is
    # the completion record) — the next scan excludes the row.
    assert row.cleanup_claimed_at == NOW
    third_run = await cleanup_expired_files(NOW, repo, storage)
    assert third_run == CleanupSummary()


async def test_successful_deletion_keeps_claim_as_the_held_marker() -> None:
    """After a completed deletion the claim stays set with deleted_at
    recording the completion — the marker pair the protection writers
    distinguish in-flight (claimed, unmarked) from finished (marked)."""
    storage = FakeObjectStorage()
    key = _stored_object(storage)
    row = _row(object_key=key, retention_until=NOW - timedelta(days=30))
    repo = InMemoryCleanupRepository([row])

    summary = await cleanup_expired_files(NOW, repo, storage)

    assert summary == CleanupSummary(scanned=1, deleted=1)
    assert row.cleanup_claimed_at == NOW
    assert row.deleted_at == NOW


# --- the claim lease (pass 5a) --------------------------------------------------------


async def test_live_lease_is_exclusive_and_expired_lease_is_taken_over() -> None:
    """The claim is a LEASE (pass 5a): while it lives, a redelivered job
    sees claim-lost and issues no provider call, and the row leaves the
    candidate set; once it EXPIRES (a crashed worker), the row
    re-enters the candidate set and the takeover claim resets BOTH
    timestamps and completes the deletion. A claimed row with a NULL
    lease never re-candidates (the fail-safe reading)."""
    storage = FakeObjectStorage()
    key = _stored_object(storage)
    row = _row(object_key=key, retention_until=NOW - timedelta(days=30))
    repo = InMemoryCleanupRepository([row])
    crashed = FileRecord(
        submission_id=row.submission_id,
        object_key=key,
        retention_until=row.retention_until,
    )

    # The first delivery claims... then dies before the provider call.
    assert await repo.claim_for_cleanup(crashed, now=NOW) is True
    assert row.cleanup_claimed_at == NOW
    assert row.cleanup_lease_expires_at == NOW + LEASE

    # While the lease lives: not a candidate, and a redelivery holding
    # the stale snapshot cannot re-claim.
    assert await repo.collect_due_files(NOW + timedelta(seconds=1)) == []
    assert await repo.claim_for_cleanup(crashed, now=NOW + timedelta(seconds=1)) is (
        False
    )

    # After the lease expires: a candidate again, and the takeover
    # resets both timestamps before deleting.
    takeover_now = NOW + LEASE + timedelta(seconds=1)
    healed = await cleanup_expired_files(takeover_now, repo, storage)
    assert healed == CleanupSummary(scanned=1, deleted=1)
    assert storage.deleted_keys == [key]
    assert row.cleanup_claimed_at == takeover_now
    assert row.cleanup_lease_expires_at == takeover_now + LEASE
    assert row.deleted_at == takeover_now

    # The NULL-lease fail-safe: a claimed row with an unknown lease
    # never re-candidates, however far the clock advances.
    null_lease_key = _stored_object(storage)
    null_lease_row = _row(
        object_key=null_lease_key, retention_until=NOW - timedelta(days=30)
    )
    null_lease_row.cleanup_claimed_at = NOW - timedelta(days=1)
    null_lease_row.cleanup_lease_expires_at = None
    repo.rows.append(null_lease_row)
    assert await repo.collect_due_files(NOW + timedelta(days=2)) == []


# --- summary consistency + job-module wiring ------------------------------------------


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


def test_cleanup_job_module_registered_in_job_modules() -> None:
    # A real worker process imports no test module: the cleanup job
    # module must be named in JOB_MODULES or its task stays invisible
    # to worker startup (the pinned task list in test_celery_wiring
    # fails on drift from the other side).
    from app.workers.celery_app import JOB_MODULES

    assert "app.workers.jobs.cleanup_files" in JOB_MODULES


# --- the real repository against real PostgreSQL (MERGE_CARRIES item 3) ---------------


_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing cleanup-repository tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def _repo_factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so
    asyncio.run phases on fresh loops never share a pooled connection
    (the test_expire_claims convention)."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(slots=True)
class _RepoSeed:
    """One seeded world: a teacher/student pair, one task, one
    assignment+claim per scenario, and one submission per claim."""

    submission_ids: dict[str, UUID]
    task_ids: list[UUID]
    user_ids: list[UUID]


async def _seed_repo_world(
    maker: async_sessionmaker[AsyncSession], run: str
) -> _RepoSeed:
    """Seed the exclusion matrix: due / not-due / permanent / legal-hold
    / already-deleted snapshots, plus claims in every status family
    (actionable, review-pipeline, terminal) all with DUE snapshots —
    only the review-pipeline ones must be excluded."""
    from app.modules.identity.enums import Role, UserStatus
    from app.modules.identity.models import User
    from app.modules.submissions.models import Submission
    from app.modules.tasks.enums import (
        AssignmentAvailability,
        ClaimStatus,
        DeadlineMode,
        RewardLockStatus,
        TaskRarity,
        TaskStatus,
        TaskType,
    )
    from app.modules.tasks.models import Assignment, AssignmentClaim, Task

    async with maker() as session:
        teacher = User(
            username=f"t{run}",
            password_hash=_PASSWORD_HASH,
            nickname=f"老师{run[-4:]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        session.add(teacher)
        await session.flush()

        # scenario -> (claim status, retention flags); every claim below
        # carries a DUE retention snapshot unless noted.
        scenarios: dict[str, dict[str, Any]] = {
            "due_claimed": {"claim": ClaimStatus.CLAIMED},
            "due_revision": {"claim": ClaimStatus.REVISION_REQUIRED},
            "due_completed": {"claim": ClaimStatus.COMPLETED},
            "due_expired": {"claim": ClaimStatus.EXPIRED},
            "due_abandoned": {"claim": ClaimStatus.ABANDONED},
            "protected_validating": {"claim": ClaimStatus.VALIDATING},
            "protected_under_review": {"claim": ClaimStatus.UNDER_REVIEW},
            "not_due": {"claim": ClaimStatus.COMPLETED, "future": True},
            "permanent": {"claim": ClaimStatus.COMPLETED, "permanent": True},
            "legal_hold": {"claim": ClaimStatus.COMPLETED, "legal_hold": True},
            "already_deleted": {
                "claim": ClaimStatus.COMPLETED,
                "already_deleted": True,
            },
        }
        # One student per scenario: the ACTIVE-claim partial unique
        # index (user_id, task_id) forbids two concurrent claims of one
        # task by the same student.
        students: dict[str, User] = {}
        for index, name in enumerate(scenarios):
            student = User(
                username=f"2025{run}{index:02d}",
                password_hash=_PASSWORD_HASH,
                nickname=f"同学{run[-4:]}",
                phone_e164=None,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
            session.add(student)
            students[name] = student
        await session.flush()
        task = Task(
            owner_teacher_id=teacher.id,
            title="期末课程问卷数据采集",
            description="采集问卷数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema={"columns": [{"name": "note", "type": "string"}]},
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
        )
        session.add(task)
        await session.flush()

        submission_ids: dict[str, UUID] = {}
        for index, (name, spec) in enumerate(scenarios.items()):
            assignment = Assignment(
                task_id=task.id,
                platform="xiaohongshu",
                keyword=f"问卷{index}",
                availability_status=AssignmentAvailability.OCCUPIED,
            )
            session.add(assignment)
            await session.flush()
            claim = AssignmentClaim(
                assignment_id=assignment.id,
                task_id=task.id,
                user_id=students[name].id,
                status=spec["claim"].value,
                claimed_at=NOW - timedelta(days=400),
                deadline_at=NOW - timedelta(days=200),
                grace_deadline_at=NOW - timedelta(days=199),
                reward_policy_snapshot={"version": 1},
                base_reward_points_snapshot=100,
                submission_schema_version=1,
                reward_lock_status=RewardLockStatus.NONE,
                terminal_at=(
                    NOW - timedelta(days=198)
                    if spec["claim"]
                    in (
                        ClaimStatus.COMPLETED,
                        ClaimStatus.ABANDONED,
                        ClaimStatus.EXPIRED,
                    )
                    else None
                ),
            )
            session.add(claim)
            await session.flush()
            offset = timedelta(days=30) if spec.get("future") else -timedelta(days=30)
            submission = Submission(
                claim_id=claim.id,
                version=1,
                object_key=f"submissions/{claim.id}/{uuid.uuid4()}",
                original_filename="数据.csv",
                declared_type="CSV",
                file_size=1024,
                submitted_at=NOW - timedelta(days=365),
                validation_status="VALIDATED",
                retention_until=None if spec.get("permanent") else NOW + offset,
                retention_permanent=bool(spec.get("permanent")),
                legal_hold=bool(spec.get("legal_hold")),
                deleted_at=(
                    NOW - timedelta(days=1) if spec.get("already_deleted") else None
                ),
            )
            session.add(submission)
            await session.flush()
            claim.latest_submission_id = submission.id
            submission_ids[name] = submission.id
        await session.commit()
        return _RepoSeed(
            submission_ids=submission_ids,
            task_ids=[task.id],
            user_ids=[teacher.id, *(student.id for student in students.values())],
        )


async def _cleanup_repo_world(
    maker: async_sessionmaker[AsyncSession], seed: _RepoSeed
) -> None:
    """Explicit committed cleanup in FK order (submissions -> claims ->
    assignments -> tasks -> users)."""
    from sqlalchemy import select

    from app.modules.identity.models import User
    from app.modules.submissions.models import Submission
    from app.modules.tasks.models import Assignment, AssignmentClaim, Task

    async with maker() as session:
        claim_ids = (
            (
                await session.execute(
                    select(AssignmentClaim.id).where(
                        AssignmentClaim.task_id.in_(seed.task_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if claim_ids:
            await session.execute(
                delete(Submission).where(Submission.claim_id.in_(claim_ids))
            )
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.id.in_(claim_ids))
            )
        await session.execute(
            delete(Assignment).where(Assignment.task_id.in_(seed.task_ids))
        )
        await session.execute(delete(Task).where(Task.id.in_(seed.task_ids)))
        await session.execute(delete(User).where(User.id.in_(seed.user_ids)))
        await session.commit()


@pytest.mark.integration
def test_real_repository_due_predicate_excludes_protected_rows() -> None:
    """The real SubmissionCleanupRepository over PostgreSQL yields
    EXACTLY the due, deletable snapshots (§13/§27): retention due, not
    permanent, no legal hold, not already marked, and the claim OUTSIDE
    the review pipeline (VALIDATING / UNDER_REVIEW excluded; actionable
    and terminal claims eligible — the file's retention is independent
    of how the claim ended)."""
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _repo_factory()
    run = uuid.uuid4().hex[:8]
    seed = asyncio.run(_seed_repo_world(maker, run))
    try:
        repository = SubmissionCleanupRepository(maker, lease_seconds=300)
        records = asyncio.run(repository.collect_due_files(NOW))

        expected = {
            seed.submission_ids["due_claimed"],
            seed.submission_ids["due_revision"],
            seed.submission_ids["due_completed"],
            seed.submission_ids["due_expired"],
            seed.submission_ids["due_abandoned"],
        }
        assert {record.submission_id for record in records} == expected
        # The snapshot shape the service consumes: candidate rows carry
        # their due instant and no flags.
        for record in records:
            assert record.retention_until is not None
            assert record.retention_until <= NOW
            assert record.permanent is False
            assert record.legal_hold is False
            assert record.protected is False
            assert record.deleted_at is None
    finally:
        asyncio.run(_cleanup_repo_world(maker, seed))


@pytest.mark.integration
def test_real_repository_mark_deleted_is_idempotent_compare_and_set() -> None:
    """mark_deleted records the instant once (MARKED) and answers
    ALREADY_DELETED on the second call without overwriting the earlier
    instant — the racing-redelivery contract the §27 branches rely on."""
    from app.modules.submissions.models import Submission
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _repo_factory()
    run = uuid.uuid4().hex[:8]
    seed = asyncio.run(_seed_repo_world(maker, run))
    try:
        repository = SubmissionCleanupRepository(maker, lease_seconds=300)
        target = seed.submission_ids["due_completed"]
        record = FileRecord(
            submission_id=target,
            object_key="submissions/x/y",  # unused by mark_deleted
            retention_until=NOW - timedelta(days=30),
        )
        first = asyncio.run(repository.mark_deleted(record, deleted_at=NOW))
        assert first is MarkOutcome.MARKED
        second = asyncio.run(
            repository.mark_deleted(record, deleted_at=NOW + timedelta(minutes=5))
        )
        assert second is MarkOutcome.ALREADY_DELETED

        async def _observe() -> datetime | None:
            async with maker() as session:
                row = await session.get(Submission, target)
                assert row is not None
                return row.deleted_at

        # The earlier instant survived the racing second mark.
        assert asyncio.run(_observe()) == NOW
    finally:
        asyncio.run(_cleanup_repo_world(maker, seed))
