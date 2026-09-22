# backend/app/modules/submissions/query_service.py
"""Submission read models: the owner's validation report, authorized
short-lived download grants, and the teacher review queue (spec §11,
§12.4, §28, §33.3, §40, §41; backend-engineering §3, §16).

Read-only queries with their authorization gates — the router's thin
surface delegates here, exactly like the tasks module's
``TaskQueryService``. No method writes or commits.

Authorization shapes:

- ``get_validation`` is OWNER-ONLY (spec §40): the submission's claim
  must belong to the actor; anyone else gets the typed 403, never the
  report. The view carries the student-safe §12.4 projection — the
  persisted report already contains no object keys and no parser
  internals (``validators.common`` builds exactly that shape), and the
  view adds only the status columns.
- ``create_download_url`` verifies ownership/role BEFORE signing (spec
  §33.3, backend-engineering §16): the claim's owner, the task's owner
  teacher, an Admin, or a ``REVIEW_SUBMISSIONS`` collaborator pass; a
  VIEW_TASK-only collaborator, an unrelated teacher, and any other
  student are refused. Only then is the storage port asked for a
  short-lived URL — providers sign without checking existence, so the
  business authorization is entirely this service's job.
- ``list_review_queue`` mirrors the workbench-list visibility (spec
  §41): tasks the actor owns OR collaborates on — Admin is deliberately
  not special-cased to the whole site (the §41 admin console has its
  own surfaces). The pool is machine-VALIDATED submissions whose review
  is still undecided (``PENDING_REVIEW``/``UNDER_REVIEW``): failed
  submissions answer to the student's own report, decided ones are
  history. Oldest first — review is a FIFO queue.

Lock-free by design: every query is a plain SELECT over committed
state; the writers that change these verdicts (validation service,
review service) serialize through the claim/submission row locks they
already own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.integrations.object_storage import DownloadUrl, ObjectStorage
from app.modules.identity.events import Actor
from app.modules.submissions.enums import ReviewStatus, ValidationStatus
from app.modules.submissions.models import Submission
from app.modules.submissions.validation_service import SubmissionNotFoundError
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.models import (
    Assignment,
    AssignmentClaim,
    Task,
    TaskCollaborator,
)

__all__ = [
    "ReviewQueueItem",
    "SubmissionNotOwnedError",
    "SubmissionQueryService",
    "SubmissionValidationView",
]

#: The review statuses that still await a teacher decision — the queue
#: pool. ``UNDER_REVIEW`` is included for forward-compatibility (the
#: claim-side state of the same name); today the submission projection
#: stays ``PENDING_REVIEW`` until a decision lands.
_QUEUE_REVIEW_STATUSES: tuple[ReviewStatus, ...] = (
    ReviewStatus.PENDING_REVIEW,
    ReviewStatus.UNDER_REVIEW,
)

_NOT_OWNED_MESSAGE = "只能查看自己的提交"


class SubmissionNotOwnedError(BusinessError):
    """The submission exists but belongs to another student's claim (spec
    §40: the validation report and the download grant are owner/reviewer
    surfaces)."""

    def __init__(self, submission_id: UUID, actor_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOT_OWNED_MESSAGE,
            status_code=403,
            details={"submission_id": str(submission_id), "actor_id": str(actor_id)},
        )


@dataclass(frozen=True, slots=True)
class SubmissionValidationView:
    """The owner-facing validation outcome of one submission."""

    submission_id: UUID
    claim_id: UUID
    version: int
    validation_status: str
    review_status: str
    detected_type: str | None
    report: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    """One review-queue row with its reviewer context."""

    submission_id: UUID
    claim_id: UUID
    task_id: UUID
    task_title: str
    platform: str
    keyword: str
    version: int
    original_filename: str
    declared_type: str
    detected_type: str | None
    file_size: int
    submitted_at: datetime
    review_status: str
    claim_status: str
    reward_tier_locked: int | None
    locked_reward_points: int | None
    validation: dict[str, Any] | None


class SubmissionQueryService:
    """Read-side queries for the submission surfaces."""

    # -- student surfaces ------------------------------------------------------

    async def get_validation(
        self, db: AsyncSession, actor: Actor, submission_id: UUID
    ) -> SubmissionValidationView:
        """The OWNER's validation report view (spec §12.4 student-facing
        rendering). Other actors — including staff — get the typed 403:
        the teacher surface for the same submission is the review queue."""
        submission, owner_id = await self._load_with_owner(db, submission_id)
        if owner_id != actor.user_id:
            raise SubmissionNotOwnedError(submission_id, actor.user_id)
        return SubmissionValidationView(
            submission_id=submission.id,
            claim_id=submission.claim_id,
            version=submission.version,
            validation_status=submission.validation_status,
            review_status=submission.review_status,
            detected_type=submission.detected_type,
            report=submission.validation_report,
        )

    async def create_download_url(
        self,
        db: AsyncSession,
        actor: Actor,
        submission_id: UUID,
        storage: ObjectStorage,
        *,
        expires_in: timedelta,
    ) -> DownloadUrl:
        """Sign a short-lived GET after verifying ownership/role (spec
        §33.3; backend-engineering §16). The port call is the last step —
        authorization is judged purely on database state, never on
        anything the client carries."""
        submission, owner_id = await self._load_with_owner(db, submission_id)
        if owner_id != actor.user_id:
            claim = await self._load_claim(db, submission.claim_id)
            await self._require_download_standing(db, claim, actor, submission_id)
        # Worker-context-style sync port call: presigning is a local HMAC
        # (or one provider round trip), acceptable inside the handler.
        return storage.create_download_url(
            object_key=submission.object_key, expires_in=expires_in
        )

    # -- teacher surfaces ------------------------------------------------------

    async def list_review_queue(
        self,
        db: AsyncSession,
        actor: Actor,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[ReviewQueueItem], int]:
        """One offset page of the actor's review queue (spec §28/§41):
        VALIDATED-but-undecided submissions on tasks the actor owns or
        collaborates on, oldest first (FIFO review)."""
        collaborated = select(TaskCollaborator.task_id).where(
            TaskCollaborator.teacher_id == actor.user_id
        )
        visibility = or_(
            Task.owner_teacher_id == actor.user_id, Task.id.in_(collaborated)
        )
        pool = (
            Submission.validation_status == ValidationStatus.VALIDATED.value,
            Submission.review_status.in_(
                [status.value for status in _QUEUE_REVIEW_STATUSES]
            ),
            visibility,
        )

        total = int(
            await db.scalar(
                select(func.count())
                .select_from(Submission)
                .join(AssignmentClaim, AssignmentClaim.id == Submission.claim_id)
                .join(Task, Task.id == AssignmentClaim.task_id)
                .where(*pool)
            )
            or 0
        )
        if total == 0 or offset >= total:
            return [], total

        rows = (
            await db.execute(
                select(
                    Submission,
                    AssignmentClaim,
                    Assignment,
                    Task,
                )
                .join(AssignmentClaim, AssignmentClaim.id == Submission.claim_id)
                .join(Assignment, Assignment.id == AssignmentClaim.assignment_id)
                .join(Task, Task.id == AssignmentClaim.task_id)
                .where(*pool)
                .order_by(Submission.submitted_at.asc(), Submission.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return [
            ReviewQueueItem(
                submission_id=submission.id,
                claim_id=claim.id,
                task_id=task.id,
                task_title=task.title,
                platform=assignment.platform,
                keyword=assignment.keyword,
                version=submission.version,
                original_filename=submission.original_filename,
                declared_type=submission.declared_type,
                detected_type=submission.detected_type,
                file_size=submission.file_size,
                submitted_at=submission.submitted_at,
                review_status=submission.review_status,
                claim_status=claim.status,
                reward_tier_locked=claim.reward_tier_locked,
                locked_reward_points=claim.locked_reward_points,
                validation=submission.validation_report,
            )
            for submission, claim, assignment, task in rows
        ], total

    # -- shared loading ----------------------------------------------------------

    @staticmethod
    async def _load_with_owner(
        db: AsyncSession, submission_id: UUID
    ) -> tuple[Submission, UUID]:
        """One join-free pair: the Submission and its claim's owner id.

        The claim row is fetched by primary key right after — the FK
        guarantees it exists, and the fresh read keeps the ownership
        judgment off any possibly-stale identity-map copy.
        """
        submission = await db.scalar(
            select(Submission).where(Submission.id == submission_id)
        )
        if submission is None:
            raise SubmissionNotFoundError(submission_id)
        owner_id = await db.scalar(
            select(AssignmentClaim.user_id).where(
                AssignmentClaim.id == submission.claim_id
            )
        )
        assert owner_id is not None  # submissions.claim_id FK
        return submission, owner_id

    @staticmethod
    async def _load_claim(db: AsyncSession, claim_id: UUID) -> AssignmentClaim:
        claim = await db.get(AssignmentClaim, claim_id)
        assert claim is not None  # submissions.claim_id FK
        return claim

    async def _require_download_standing(
        self,
        db: AsyncSession,
        claim: AssignmentClaim,
        actor: Actor,
        submission_id: UUID,
    ) -> None:
        """The reviewer arm of the download gate: task owner, Admin, or a
        ``REVIEW_SUBMISSIONS`` collaborator (spec §4.2/§4.3 — the same
        standing that may decide the submission). The
        ``review_service._require_review_permission`` query pattern with
        the frozen ``CollaboratorPermission`` vocabulary; a read-only
        denial needs no rollback."""
        owner_id = await db.scalar(
            select(Task.owner_teacher_id).where(Task.id == claim.task_id)
        )
        if owner_id is None or owner_id == actor.user_id or is_admin(actor.role):
            return
        permissions = await db.scalar(
            select(TaskCollaborator.permissions).where(
                TaskCollaborator.task_id == claim.task_id,
                TaskCollaborator.teacher_id == actor.user_id,
            )
        )
        if (
            permissions is None
            or CollaboratorPermission.REVIEW_SUBMISSIONS not in permissions
        ):
            raise SubmissionNotOwnedError(submission_id, actor.user_id)
