# backend/app/modules/files/cleanup_service.py
"""File-retention cleanup service (plan 07 T7; spec §13, §27;
backend-engineering §12; hardening pass 4b deletion-claim ruling; pass
5a leases + unified serialization boundary; final pass A fencing).

Worker owns orchestration, service owns judgement: the Celery shell
(``app/workers/jobs/cleanup_files.py``) samples the clock, constructs
the repository and the object-storage adapter, and calls
``cleanup_expired_files``. Every delete / retain decision lives here.

Deletion is CLAIMED under a LEASE with an OWNERSHIP TOKEN (pass 4b
claim; pass 5a P0-1/P0-2; final pass A fencing),
not snapshot-judged: the repository's claim is a short transaction that
locks the submission row and then the claim row — the SAME lock order
the protection writers take — re-evaluates every §13/§27 retain guard
against CURRENT committed state under both locks, and writes
``cleanup_claimed_at`` + ``cleanup_lease_expires_at`` + a fresh
``cleanup_claim_token`` before committing;
the provider delete runs OUTSIDE the transaction. The protection writers
(``app.modules.submissions.cleanup_claim``) check for an unfinished
claim under the claim row lock they already hold, so protection and
claim serialize on the same row locks in the same order — whichever
side commits first wins, the other side's guard sees the committed
outcome (the claim-ownership ruling: an UNFINISHED claim blocks
protection even past lease expiry; deletion recovery is the retryable
side). The claim window is bounded by the lease: a worker that dies
between its claim commit and the provider delete leaves an EXPIRED
lease, the scan re-candidates the row, and the next run takes over —
re-claiming under the same guarded boundary, REWRITING the token — so
no crash can strand an object or block a protection writer forever
(P0-2). FENCING closes the slow-but-alive residue of that takeover: a
worker resuming after its lease expired and a takeover happened holds a
STALE token, so its release/mark matches zero rows and is abandoned —
it can never clear or complete its successor's claim (ABA closure), and
paired with the ruling it can never delete an object after a protection
committed. The service-level snapshot guards remain as the
stale-listing defense in front.

Pass 4b also adds the orphan-intent cleanup (``cleanup_orphaned_intents``):
expired intents that never finalized hold stored objects nobody else
will ever remove. Same claim primitive over the intent's lease columns
(pass 5a splits ``cleanup_claimed_at`` / ``cleanup_lease_expires_at``
from the ``cleanup_deleted_at`` DONE marker; final pass A adds the
ownership token to the same statement, so the intent side gets the same
stale-worker fencing), only ever claimed AFTER
``expires_at`` — the presigned URL TTL is deployed shorter than the
intent TTL, so no legal PUT can land after the intent expired (a
deployment invariant this cleanup depends on; both TTLs are injectable,
wired from Settings). Intents carry no protection semantics — finalize
refuses expired intents under the intent-row lock it already takes — so
their claim stays one conditional UPDATE and an expired lease converges
by takeover + idempotent delete. Finalized intents are never touched:
their object belongs to the Submission row and the retention pipeline
above.

The worker consumes file state through two seams:

- ``FileRecord`` — the retention snapshot of ONE stored object: the
  ``retention_until`` snapshot XOR the explicit ``permanent`` flag (§13:
  permanent retention is a real marker, never a "very large date"), the
  ``legal_hold`` and under-review ``protected`` flags, and
  ``deleted_at`` (None while the database believes the object present).
- ``CleanupRepository`` — due-candidate discovery, the lock-ordered
  leased claim/release pair that owns the deletion right (with crash
  takeover once a lease expires and ownership-token fencing against
  stale workers), and the idempotent ``mark_deleted`` compare-and-set.
  The real query excludes permanent, legal-hold,
  protected (claims in review states, §27 不得误删尚在审核中的文件),
  not-yet-due, already-deleted, and live-leased rows BY CONSTRUCTION;
  the service re-validates every guard per record anyway, and the claim
  re-evaluates them under the row locks, so a stale or buggy listing
  can never reach the provider delete.

Ordering (§13): the object is deleted FIRST, then the database state is
updated. Business metadata, validation reports, and audit records are
never removed — ``mark_deleted`` only records the deletion instant on
the same row. No row lock is ever held across the provider call (the
claim is declarative — that is the whole point of it).

Idempotency (§27), absorbed at three layers:

1. By construction — already-deleted rows leave the candidate set, so a
   re-run over the same state issues zero provider delete calls.
2. Claim exclusivity — a redelivered job holding a STALE snapshot
   cannot claim a row whose lease is live, so it issues no provider
   call at all (SKIPPED_CLAIM_LOST); the token-fenced compare-and-set
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
reason, the CLAIM IS RELEASED (so the next scan can retry immediately
and the protection writers are not blocked on a dead claim), and the
record stays otherwise untouched.

Unexpected exceptions (a repository/DB failure or a storage bug outside
the taxonomy) are NOT swallowed: they propagate so redelivery and
operators see them, mirroring the claim-expiry jobs' loud-bug rule. A
crash between claim and release strands the claim only until its LEASE
EXPIRES (pass 5a): the next scan takes the row over, re-evaluates every
guard under the protection paths' own row locks, and converges — the
deliberate fail-safe of pass 4b (manual claim clearing) is now the
bounded-window path the lease automates.
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
    """Result of the idempotent, token-fenced ``mark_deleted``
    compare-and-set."""

    MARKED = "MARKED"
    ALREADY_DELETED = "ALREADY_DELETED"
    #: The claim's ownership token no longer matches: the lease expired
    #: mid-flight and a takeover rewrote it — the completion belongs to
    #: the takeover worker, and this stale delivery writes nothing.
    CLAIM_LOST = "CLAIM_LOST"


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
        (claims in review states), not already marked deleted, and not
        under a live cleanup lease — a claimed row whose lease EXPIRED
        is included (the crashed-claim takeover, pass 5a). Exclusion by
        construction is the first idempotency layer; the claim
        re-evaluates every guard under the row locks regardless.
        """
        ...

    async def claim_for_cleanup(
        self, record: FileRecord, *, now: datetime
    ) -> UUID | None:
        """Claim the deletion right over one record (pass 5a unified
        serialization boundary; final pass A fencing) — returns the
        OWNERSHIP TOKEN this claim wrote, or None when the claim was
        lost.

        ONE short transaction locked in the protection paths' own order
        — submission row FOR UPDATE first, then the claim row FOR
        UPDATE. Under BOTH locks every §13/§27 guard is re-evaluated
        against current committed state — not permanent, no legal hold,
        retention still due, not already deleted, the claim's status
        still outside the review pipeline, and no live lease held — and
        the same transaction writes the lease
        (``cleanup_claimed_at = now``,
        ``cleanup_lease_expires_at = now + lease``) plus a FRESH
        ``cleanup_claim_token`` and commits. A token is returned only
        for the winner; a takeover (the previous lease expired) resets
        both timestamps and REWRITES the token, which is what fences the
        old worker's late release/mark out of the row. Because the
        protection writers check for an unfinished claim under the claim
        row lock, the two sides serialize on the same locks: a
        protection committing between this scan and the claim makes the
        claim fail and the object survive, and a protection arriving
        mid-claim waits, then sees the committed claim (the typed 409 —
        an unfinished claim blocks protection even past lease expiry,
        the claim-ownership ruling). No lock is carried across the
        provider call.
        """
        ...

    async def release_cleanup_claim(self, record: FileRecord, *, token: UUID) -> None:
        """Release the claim: clear all three claim columns, CAS'd on
        ``token`` — a stale worker whose row a takeover re-claimed
        matches zero rows and abandons (the takeover's live claim is
        untouched). The object is NOT marked deleted; the release means
        "this worker gives up, the row is unclaimed". NOT used by the
        submission failure path since the post-merge P0 (Option B):
        provider failures there KEEP the claim for a lease takeover
        (correctness never depends on a wall-clock provider bound).
        Retained for administrative repair and the fencing tests.
        """
        ...

    async def mark_deleted(
        self,
        record: FileRecord,
        *,
        token: UUID,
        deleted_at: datetime,
    ) -> MarkOutcome:
        """Record the deletion instant on the file's own row, CAS'd on
        the claim's ownership token.

        Idempotent compare-and-set: a row already marked deleted answers
        ``ALREADY_DELETED`` without overwriting the earlier instant; a
        live row this delivery still owns records ``deleted_at`` and
        answers ``MARKED``; a row whose token a takeover rewrote answers
        ``CLAIM_LOST`` (logged and abandoned by the repository — the
        completion belongs to the takeover). Only the deletion state
        changes — business metadata, validation reports, and audit
        records stay untouched (§13).
        """
        ...


class OrphanIntentRepository(Protocol):
    """Port for the orphan-intent cleanup (pass 4b): candidate
    discovery plus the same leased claim/release primitive over
    ``upload_intents`` (pass 5a splits the claim columns from the done
    marker).

    The production implementation is the same
    ``SubmissionCleanupRepository`` (one repository, two tables), kept
    as a separate seam so the service function states exactly what it
    consumes.
    """

    async def collect_due_intents(
        self, now: datetime, *, limit: int = CLEANUP_BATCH_LIMIT
    ) -> list[IntentRecord]:
        """Expired intents that never finalized, oldest expiry first.

        The real query yields ONLY not-yet-done rows with
        ``expires_at <= now`` and ``finalized_submission_id IS NULL`` —
        open-expired and burned intents alike — that are not under a
        LIVE cleanup lease; a claimed row whose lease expired is
        included (crash takeover, pass 5a). A finalized intent's object
        belongs to its Submission and the retention pipeline.
        """
        ...

    async def claim_intent(self, intent: IntentRecord, *, now: datetime) -> UUID | None:
        """Claim one intent for deletion — returns the OWNERSHIP TOKEN
        this claim wrote, or None when the claim was lost. A single
        conditional UPDATE
        re-evaluating every guard (``expires_at <= now``, never
        finalized, not done, no live lease) and setting the lease
        columns (``cleanup_claimed_at = now`` plus
        ``cleanup_lease_expires_at = now + lease`` and a fresh
        ``cleanup_claim_token``; pass 5a splits them
        from the ``cleanup_deleted_at`` done marker, which
        ``mark_intent_deleted`` sets only after the delete settled). A
        takeover (expired lease) resets the timestamps and rewrites the
        token, fencing the dead worker's late release/mark out of the
        row. Finalize can
        never lose to this claim: it holds the intent-row lock before
        its own expiry check, so the two serialize on the row; intents
        have no protection transition, so the single statement is their
        whole serialization boundary.
        """
        ...

    async def mark_intent_deleted(
        self, intent: IntentRecord, *, token: UUID, deleted_at: datetime
    ) -> None:
        """Record the deletion's completion, CAS'd on the ownership
        token: set the done marker (``cleanup_deleted_at``) only while
        NULL AND the row still carries ``token`` — idempotent for the
        owner, abandoned without a write by a fenced-out stale worker
        (the takeover records the completion on its own claim)."""
        ...

    async def release_intent(self, intent: IntentRecord, *, token: UUID) -> None:
        """Release the claim after a provider failure (clear all three
        claim columns, CAS'd on ``token`` — a fenced-out stale worker
        abandons); the next scan retries."""
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
    CLAIM — the lock-ordered short transaction that re-evaluates every
    guard against current state and writes the lease plus the ownership
    token; losing the claim (a protection landed since the scan, or
    another delivery holds a live lease) retains the object with
    SKIPPED_CLAIM_LOST and no provider call. The token threads into
    every later release/mark (final pass A fencing): a worker whose
    lease expired mid-flight loses those CASes to the takeover and its
    writes are abandoned. A missing object splits on the repository's
    compare-and-set: already marked = idempotent success (§27 branch
    1); believed present = reconcile with a warning (§27 branch 2).
    Provider failures KEEP the unfinished claim (post-merge P0,
    Option B): correctness must not depend on any wall-clock provider
    bound, so protection stays blocked until a lease takeover
    converges the deletion; the s3_* timeout settings are availability
    controls, not a safety proof.
    """
    skipped = _guard(record, now)
    if skipped is not None:
        return skipped

    token = await repo.claim_for_cleanup(record, now=now)
    if token is None:
        return FileCleanupOutcome.SKIPPED_CLAIM_LOST

    try:
        storage.delete_object(object_key=record.object_key)
    except FileNotFoundError:
        outcome = await repo.mark_deleted(record, token=token, deleted_at=now)
        if outcome is MarkOutcome.ALREADY_DELETED:
            return FileCleanupOutcome.ALREADY_DELETED
        if outcome is MarkOutcome.CLAIM_LOST:
            # Fenced out mid-reconcile: the takeover owns the row and
            # its own 404-deletes converge the same mark (with the
            # warning) on ITS claim — this delivery stays silent so one
            # convergence logs exactly one warning.
            return FileCleanupOutcome.RECONCILED_MISSING
        logger.warning(
            "file_cleanup.reconcile_missing_object",
            extra={
                "submission_id": str(record.submission_id),
                "object_key": record.object_key,
            },
        )
        return FileCleanupOutcome.RECONCILED_MISSING
    except TemporaryProviderError:
        # Post-merge P0 hotfix (Option B): a provider failure KEEPS the
        # unfinished deletion claim. Releasing it would let
        # takeover-plus-release open protection while a predecessor
        # worker's already-sent DELETE could still be in flight — no
        # wall-clock timeout arithmetic can prove otherwise (connect and
        # read timeouts bound socket waits, not a request's total
        # lifetime). The claim stays; the lease authorizes another
        # cleanup worker to take over and converge the deletion, and
        # protection stays blocked until the cleanup state truly
        # settles (mark_deleted or the §27 missing-object reconcile).
        return FileCleanupOutcome.FAILED_STORAGE_TEMPORARY
    except PermanentProviderError:
        # Same Option B ruling: the claim is held for takeover — a
        # permanent provider error still leaves the object's fate
        # unresolved, and an open protection window over a possibly
        # deleted object is the one outcome §27 forbids.
        return FileCleanupOutcome.FAILED_STORAGE_PERMANENT
    except UnknownOutcomeError:
        # The delete may or may not have happened — the sharpest case
        # for holding the claim: object-first ordering converges on the
        # next takeover (present -> delete again, gone -> reconcile /
        # already-deleted), and until then protection cannot open over
        # an undecided deletion.
        return FileCleanupOutcome.FAILED_STORAGE_UNKNOWN

    outcome = await repo.mark_deleted(record, token=token, deleted_at=now)
    if outcome is MarkOutcome.ALREADY_DELETED:
        # A racing delivery completed both steps; this one is the
        # idempotent no-op, not a second deletion.
        return FileCleanupOutcome.ALREADY_DELETED
    # MARKED, or CLAIM_LOST with the provider delete already issued:
    # either way the OBJECT is gone and the row's completion is owned by
    # a live claim (this one, or the takeover that fenced this one out)
    # — count the deletion; the fenced row converges on that claim.
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
    """Decide and act on ONE orphan-intent candidate: claim the lease
    (the guards live entirely inside the conditional claim — there is
    no boolean snapshot to trust), then delete the object, then record
    the done marker (pass 5a: the claim columns no longer double as
    it). The claim returns the ownership token and every later
    release/mark CASes on it (final pass A fencing): a worker whose
    lease expired mid-flight loses the row to a takeover and its late
    writes are abandoned, never applied to the successor's claim.

    A missing object is the idempotent SUCCESS shape (§27): most
    expired intents were never uploaded, and a re-run over a cleaned
    intent re-claims nothing. An expired lease makes the row a
    candidate again, so a worker that died between claim and delete is
    taken over here and converges. Provider failures release the lease
    for the next scan.
    """
    token = await repo.claim_intent(intent, now=now)
    if token is None:
        return OrphanIntentOutcome.SKIPPED_CLAIM_LOST

    try:
        storage.delete_object(object_key=intent.object_key)
    except FileNotFoundError:
        await repo.mark_intent_deleted(intent, token=token, deleted_at=now)
        return OrphanIntentOutcome.MISSING_OBJECT
    except TemporaryProviderError:
        await repo.release_intent(intent, token=token)
        return OrphanIntentOutcome.FAILED_STORAGE_TEMPORARY
    except PermanentProviderError:
        await repo.release_intent(intent, token=token)
        return OrphanIntentOutcome.FAILED_STORAGE_PERMANENT
    except UnknownOutcomeError:
        await repo.release_intent(intent, token=token)
        return OrphanIntentOutcome.FAILED_STORAGE_UNKNOWN
    await repo.mark_intent_deleted(intent, token=token, deleted_at=now)
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
