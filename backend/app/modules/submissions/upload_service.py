# backend/app/modules/submissions/upload_service.py
"""Presigned upload intent issuance and finalization (spec §10 steps 1-7,
§11, §13, §31.11, §32; backend-engineering §5-§7, §13, §16).

The presigned flow (spec §10): the student requests an upload intent, the
backend validates permissions/claim state/declared type/size caps/the
submission window, the storage port issues a SHORT-LIVED server-keyed
presigned URL, the browser uploads directly, the client notifies the
backend, the backend verifies the object exists with the declared
metadata, creates the Submission, and a worker validates asynchronously
(later plan task). This service owns steps 1-7 minus the worker.

Transaction shapes (one transaction, exactly one commit on the success
path; both flows):

``create_upload_intent``:

1. ``SELECT status, role FROM users WHERE id = :user_id FOR UPDATE`` —
   the same stable user-level resource claim/abandon lock FIRST, so one
   user's lifecycle writes share one serialization queue. BOTH columns
   are judged on the locked row (role §4.1, status §5.7) — the
   domain-invariant mirror of ``require_active_student_actor``: direct
   service callers and concurrent role/status changes cannot bypass the
   gate.
2. ``SELECT ... FROM assignment_claims WHERE id = :claim_id FOR UPDATE``
   — ownership is judged on the locked row.
3. Task facts via one lock-free SELECT (see LOCK ORDER below).
4. Clock sample, then the §10 step-2 checklist head (permissions, claim
   status, declared type, size caps, window) as pure rules
   (``UploadPolicyService``), then the storage port call, then the
   intent INSERT + one commit. A failed commit can strand an already
   issued presigned URL — harmless: it is never returned to the client
   and expires on its own.

``finalize_upload``:

0. Locator + remote HEAD, both lock-free/off-loop (S3 hardening P1):
   one lock-free SELECT of the intent's ``claim_id`` and ``object_key``
   (immutable columns — see LOCK ORDER below), then ``head_object``
   runs via ``asyncio.to_thread`` BEFORE any row lock is taken. This is
   safe ONLY because the upload URL is write-once (signed
   If-None-Match): an object that exists can no longer be replaced
   through any presigned URL, so the pre-lock HEAD's size/content-type
   answer remains valid through the short locked transaction — and a
   slow provider now delays locks it never holds instead of stretching
   the user/claim/intent lock hold time and blocking the event loop.
   The HEAD result is stashed, never acted on here: the state machine
   below decides whether it matters (a finalized replay whose object
   has since been retention-deleted still returns the Submission; an
   expired or burned intent answers without the HEAD changing the
   verdict).
1. User-row lock + account gate (same as create).
2. Claim row FOR UPDATE: ownership is judged on the locked row.
3. Intent row FOR UPDATE: the single-use state machine. FINALIZED
   (``finalized_submission_id`` set) -> return THAT Submission
   (idempotent replay, spec §32: never version N+1) — checked BEFORE
   the claim-submittability gate, so a delayed replay still answers
   the same Submission after the validation worker moved the claim to
   VALIDATING/UNDER_REVIEW (plan 04 task 7 fix of the T2 carry).
   BURNED (consumed with no submission) or past ``expires_at`` ->
   intent-not-found; the remedy is a fresh intent. OPEN -> proceed.
4. Claim-submittability gate on the locked claim row (spec §10 step 2:
   VALIDATING/UNDER_REVIEW mean a submission is in flight; the
   terminal states ended the claim), then the recomputed window (time
   may have passed since the intent was issued).
5. Verification against the STASHED head (spec §10 step 6): a missing
   object is a retryable client race (typed NOT_FOUND, intent stays
   consumable); a size or content-type mismatch means the stored
   object contradicts the cleared declaration — the intent is BURNED
   (conditional UPDATE ``consumed_at IS NULL`` + commit) and the typed
   error answers. With the write-once, size-pinned URL both mismatch
   branches are defense in depth (the provider already rejected such
   PUTs at the door); the recheck stays for a provider/bucket
   misconfigured out from under the presigning assumptions.
6. Retention snapshot (spec §13) from the Task's CURRENT policy at
   finalize time; version allocation ``max(version)+1`` under the claim
   lock (§31.11 — UNIQUE(claim_id, version) is the database backstop);
   Submission INSERT; the conditional single-use consume
   (``UPDATE ... WHERE consumed_at IS NULL`` carrying
   ``finalized_submission_id``); ``claim.latest_submission_id``; one
   commit.

LOCK ORDER (documented contract): user row -> claim row -> intent row.
This is a subsequence of the interfaces.md contract (users -> tasks ->
assignments/claims), preserved by reading the Task row LOCK-FREE: a
plain SELECT takes no lock, so no tasks-after-claims lock edge exists
to cycle against the claim flow's user -> task FOR SHARE -> claim locks.
The locator SELECT (intent.claim_id + intent.object_key, and nothing
else) reads immutable columns, so the unlocked peek cannot observe
half-written state. No
IntegrityError mapping exists here on purpose: every race the unique
constraints backstop (duplicate version, duplicate key, double consume)
is already serialized by the user/claim/intent row locks, so an
IntegrityError is by construction an unknown failure and must surface,
not become a conflict (backend-engineering §7).

Design decisions:

- **Intent storage: DB table, not Redis.** Auditable (the burned/finalized
  trail is business history), survives a Redis flush, and joins cleanly
  with submissions for support queries; Redis TTL expiry would silently
  destroy in-flight grants. The intent TTL bounds the table's OPEN-row
  lifetime; a cleanup sweep for expired-but-unconsumed rows belongs to
  the retention worker plan (spec §32 lists 文件清理 idempotency there).
- **Single-use via conditional UPDATE.** The consume is
  ``UPDATE upload_intents SET consumed_at = ..., finalized_submission_id
  = ... WHERE id = ... AND consumed_at IS NULL`` — atomic single-use by
  itself; the intent FOR UPDATE taken earlier is defense in depth and
  supplies the replay state, so under the locks the UPDATE always matches
  exactly one row. A zero rowcount fails safe (rollback + typed error),
  never a second Submission.
- **`submitted_at` is the finalize-time clock sample** (spec §11.2: the
  reward-tier reference is the accepted submission instant, not worker or
  review time); the model has no server default for exactly this reason.
- **Object keys are server-generated by the port** (spec §10 安全要求):
  ``create_upload_url`` derives ``submissions/{claim_id}/{uuid}``; the
  client filename never touches a path. ``original_filename`` is stored
  sanitized (``sanitize_filename``: path components stripped on both
  separators, non-printables dropped, 255-char cap, stable fallback) and
  is display-only metadata.
- **Declared type/size pinning + write-once (S3 hardening P0/P1):**
  the presigned URL carries the declared type's MIME
  (``DECLARED_TYPE_CONTENT_TYPES``), the declared byte size as a signed
  Content-Length, and a signed If-None-Match:* condition, so the
  provider rejects a PUT with a different Content-Type (403), a body of
  any other length (403), and any second PUT onto the same key (412) —
  inside or past the URL TTL, so a finalized object is immutable from
  the client side and ``submitted_at``/audit stay authoritative
  without trusting DB ``consumed_at`` to revoke a signed URL. The
  client sends the adapter's ``client_headers`` with its PUT and a body
  of exactly ``pinned_content_length`` bytes (browsers cannot set
  Content-Length — a forbidden header — and satisfy the pin with a Blob
  of the declared size); both are passed through from the adapter's
  ``UploadUrl`` verbatim, never rebuilt here (hardening P4c: the
  adapter owns the signing policy; the application echoes).
  Finalize re-checks
  the stored object's content type and size as defense in depth. Type
  and size decisions never read the filename. A client whose first PUT
  failed mid-flight after the object landed gets 412 on retry of the
  same URL: the object exists, so the remedy is finalize (the object
  is there) or a fresh intent for a NEW key.
- **Public DTOs never carry ``object_key``** (spec §40: 对象存储原始路
  径 must not leak): ``schemas.py`` builds responses field by field.
- **Idempotency-Key (spec §32) is a transport concern** for the router
  task; the service-level guarantee — replaying finalize returns the
  same Submission and the database admits one submission per intent —
  holds without one.
- **Async validation handoff (spec §10 step 8):** finalize does not
  validate. When a ``ValidationDispatcher`` port is injected, a
  successful finalize hands the fresh Submission to it — AFTER the
  commit, so the job can never race a row that is not yet visible, and
  only while the submission is still UPLOADED: a replay whose pipeline
  already started (VALIDATING or terminal) skips the enqueue, while a
  replay that still finds UPLOADED re-dispatches — that is exactly the
  recovery shape for an enqueue lost to a process death or broker
  outage between the commit and the dispatch (the client's natural
  retry of upload-complete re-arms it). The dispatch happens outside
  every transaction: an enqueue failure surfaces after the Submission
  already exists, and the retry path above makes the flow self-healing
  rather than compensating. The port is a sync callable (a ``.delay``
  publish), so the fake in tests captures calls without any broker.
- **Error vocabulary** stays inside the frozen registry: ownership and
  role are PERMISSION_DENIED 403, non-ACTIVE accounts ACCOUNT_NOT_ACTIVE
  403, non-submittable claims CLAIM_NOT_SUBMITTABLE 409, closed windows
  SUBMISSION_WINDOW_CLOSED 409, file policy failures FILE_TYPE_NOT_ALLOWED
  / FILE_TOO_LARGE 400, and unusable intents (missing, expired, burned)
  NOT_FOUND 404. No new codes were registered.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import String, Uuid, column, func, select, table, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.object_storage import ObjectStorage
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.submissions.enums import (
    FileType,
    RetentionPolicy,
    ValidationStatus,
)
from app.modules.submissions.models import Submission, UploadIntent
from app.modules.tasks.abandon_service import (
    ClaimNotFoundError,
    ClaimNotOwnedError,
)
from app.modules.tasks.claim_service import (
    AccountNotActiveError,
    Claimer,
    UserNotFoundError,
)
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import AssignmentClaim, Task
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "DECLARED_TYPE_CONTENT_TYPES",
    "DEFAULT_INTENT_TTL",
    "DEFAULT_UPLOAD_URL_TTL",
    "FILENAME_FALLBACK",
    "MAX_FILENAME_LENGTH",
    "RETENTION_POLICY_DAYS",
    "SUBMITTABLE_STATUSES",
    "ClaimNotSubmittableError",
    "FileTooLargeError",
    "FileTypeNotAllowedError",
    "IssuedUploadIntent",
    "SubmissionWindowClosedError",
    "UploadIntentNotFoundError",
    "UploadObjectMissingError",
    "UploadPolicyService",
    "UploadService",
    "UploadSizeMismatchError",
    "UploadTypeMismatchError",
    "ValidationDispatcher",
    "retention_snapshot",
    "sanitize_filename",
    "submission_window_open",
]


# --- frozen value sets and constants -------------------------------------------------


# Spec §10 checklist order: the states that still need student action.
# VALIDATING/UNDER_REVIEW mean a submission is already in flight; the
# terminal states ended the claim — both families are CLAIM_NOT_SUBMITTABLE.
SUBMITTABLE_STATUSES: tuple[ClaimStatus, ...] = (
    ClaimStatus.CLAIMED,
    ClaimStatus.REVISION_REQUIRED,
)

# Spec §13: dated retention policies snapshot submitted_at + N days;
# PERMANENT is the explicit flag (never a sentinel date).
RETENTION_POLICY_DAYS: dict[str, int] = {
    RetentionPolicy.DAYS_30.value: 30,
    RetentionPolicy.DAYS_90.value: 90,
    RetentionPolicy.DAYS_180.value: 180,
}

# MIME pinned on every presigned URL per declared type (spec §10: the
# provider rejects a PUT carrying a different Content-Type). Keys are the
# FileType members; keys and values must stay aligned with the CSV/XLSX/
# SQLITE universe the tasks module guards.
DECLARED_TYPE_CONTENT_TYPES: dict[FileType, str] = {
    FileType.CSV: "text/csv",
    FileType.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    FileType.SQLITE: "application/vnd.sqlite3",
}

# Short-lived grants (spec §10: 短时 presigned URL). The URL expires
# before the intent so a client that uploaded in time always has a live
# finalize window; both are injectable for tests and wired by the
# composition root.
DEFAULT_UPLOAD_URL_TTL = timedelta(minutes=10)
DEFAULT_INTENT_TTL = timedelta(minutes=15)

# Display-filename hygiene (spec §10): cap mirrors the String(255)
# column; the fallback keeps the NOT NULL column satisfiable for inputs
# that are nothing but path/whitespace.
MAX_FILENAME_LENGTH = 255
FILENAME_FALLBACK = "unnamed"

# Spec §10: the default file cap is 200 MB, adjustable per Task. The
# composition root wires Settings.max_upload_bytes_default (same default)
# into the service; the scalar lives here so the policy rules stay
# settings-free and unit-testable.
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Lock/verify seam for the users table (see module docstring): a typed
# Core-level light table, NOT the identity ORM model. Twin of
# claim_service's and abandon_service's private definitions — if
# interfaces.md ever registers a locking-read port, all three move
# behind it together.
_USERS_LOCK = table(
    "users",
    column("id", Uuid),
    column("status", String),
    column("role", String),
)


# --- messages (§29 envelope text) -----------------------------------------------------


_INTENT_NOT_FOUND_MESSAGE = "上传凭证不存在或已失效"
_NOT_STUDENT_MESSAGE = "仅学生账号可提交作业"
_NOT_SUBMITTABLE_MESSAGE = "该领取当前状态不可提交"
_TYPE_NOT_ALLOWED_MESSAGE = "该任务不接受此文件类型"
_FILE_TOO_LARGE_MESSAGE = "文件大小超出限制"
_WINDOW_CLOSED_MESSAGE = "提交时间窗口已关闭"
_OBJECT_MISSING_MESSAGE = "上传对象不存在，请完成上传后重试"
_SIZE_MISMATCH_MESSAGE = "上传文件实际大小与声明不一致"
_TYPE_MISMATCH_MESSAGE = "上传文件内容类型与声明不一致"


# --- typed exceptions (router-mapped) ---------------------------------------------


class UploadIntentNotFoundError(BusinessError):
    """No usable upload intent for the id: missing, expired, or burned by
    a failed verification. One uniform 404 — the remedy is always a
    fresh intent, so the branch reason is server-side detail only."""

    def __init__(self, intent_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _INTENT_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"intent_id": str(intent_id)},
        )


class UploaderNotStudentError(BusinessError):
    """The actor's role is not STUDENT (spec §4.1: submitting is a Student
    capability). Raised while holding the user-row lock, so direct service
    callers and concurrent role changes cannot bypass it."""

    def __init__(self, user_id: UUID, role: Role) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOT_STUDENT_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id), "role": role.value},
        )


class ClaimNotSubmittableError(BusinessError):
    """The claim's status accepts no student submission (spec §10 step 2):
    VALIDATING/UNDER_REVIEW (a submission is in flight) or terminal
    COMPLETED/ABANDONED/EXPIRED."""

    def __init__(self, status: ClaimStatus) -> None:
        super().__init__(
            ErrorCode.CLAIM_NOT_SUBMITTABLE,
            _NOT_SUBMITTABLE_MESSAGE,
            status_code=409,
            details={
                "claim_status": status.value,
                "submittable_statuses": [
                    submittable.value for submittable in SUBMITTABLE_STATUSES
                ],
            },
        )


class FileTypeNotAllowedError(BusinessError):
    """The declared type is outside the Task's allowed set (spec §10)."""

    def __init__(self, declared_type: str, allowed: list[str]) -> None:
        super().__init__(
            ErrorCode.FILE_TYPE_NOT_ALLOWED,
            _TYPE_NOT_ALLOWED_MESSAGE,
            status_code=400,
            details={"declared_type": declared_type, "allowed_file_types": allowed},
        )


class FileTooLargeError(BusinessError):
    """The declared size exceeds the binding cap — the Task's own limit or
    the deployment-wide ceiling, whichever is smaller (spec §10)."""

    def __init__(self, declared_size: int, limit: int) -> None:
        super().__init__(
            ErrorCode.FILE_TOO_LARGE,
            _FILE_TOO_LARGE_MESSAGE,
            status_code=400,
            details={"declared_size": declared_size, "limit": limit},
        )


class SubmissionWindowClosedError(BusinessError):
    """The claim's submission window is closed at the judged instant (spec
    §9.3: forbidden from grace_deadline_at on; §11.4: REVISION_REQUIRED
    claims read revision_deadline_at instead)."""

    def __init__(
        self,
        claim_status: ClaimStatus,
        grace_deadline_at: datetime | None,
        revision_deadline_at: datetime | None,
    ) -> None:
        super().__init__(
            ErrorCode.SUBMISSION_WINDOW_CLOSED,
            _WINDOW_CLOSED_MESSAGE,
            status_code=409,
            details={
                "claim_status": claim_status.value,
                "grace_deadline_at": (
                    grace_deadline_at.isoformat() if grace_deadline_at else None
                ),
                "revision_deadline_at": (
                    revision_deadline_at.isoformat() if revision_deadline_at else None
                ),
            },
        )


class UploadObjectMissingError(BusinessError):
    """``head_object`` found no object for the intent's key (spec §10 step
    6): the client notified before the upload landed. Retryable — the
    intent stays consumable."""

    def __init__(self, intent_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _OBJECT_MISSING_MESSAGE,
            status_code=404,
            details={"intent_id": str(intent_id)},
        )


class UploadSizeMismatchError(BusinessError):
    """The stored object's actual size contradicts the declared size that
    cleared the size policy (spec §10 step 6): the upload is corrupt, the
    intent is burned, and the remedy is a fresh intent."""

    def __init__(self, declared_size: int, actual_size: int) -> None:
        super().__init__(
            ErrorCode.FILE_TOO_LARGE,
            _SIZE_MISMATCH_MESSAGE,
            status_code=400,
            details={"declared_size": declared_size, "actual_size": actual_size},
        )


class UploadTypeMismatchError(BusinessError):
    """The stored object's content type contradicts the declared type's
    pinned MIME — the presigned URL already rejects such PUTs, so this is
    the defense-in-depth recheck. Same burn-and-re-intent remedy."""

    def __init__(self, declared_type: str, actual: str, expected: str) -> None:
        super().__init__(
            ErrorCode.FILE_TYPE_NOT_ALLOWED,
            _TYPE_MISMATCH_MESSAGE,
            status_code=400,
            details={
                "declared_type": declared_type,
                "actual_content_type": actual,
                "expected_content_type": expected,
            },
        )


# --- pure helpers ---------------------------------------------------------------------


def sanitize_filename(filename: str) -> str:
    """Reduce a client filename to display-only metadata (spec §10).

    Both path separators are stripped to the final segment, control and
    non-printable characters are dropped, surrounding whitespace is
    trimmed, the result is capped at ``MAX_FILENAME_LENGTH``, and an
    empty outcome falls back to a stable placeholder. The value never
    feeds paths, keys, or type decisions.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character.isprintable())
    name = name.strip()
    return name[:MAX_FILENAME_LENGTH] or FILENAME_FALLBACK


def retention_snapshot(
    retention_policy: str, submitted_at: datetime
) -> tuple[datetime | None, bool]:
    """Snapshot the Task's retention policy at finalize time (spec §13).

    Returns ``(retention_until, retention_permanent)`` — exactly one
    bound: dated policies compute ``submitted_at + N days``; PERMANENT is
    the explicit flag with a NULL expiry. An unknown policy string is a
    loud ValueError, not a silent default (the column's CHECK makes it
    unreachable via the database; this guards direct callers).
    """
    if retention_policy == RetentionPolicy.PERMANENT.value:
        return None, True
    days = RETENTION_POLICY_DAYS.get(retention_policy)
    if days is None:
        raise ValueError(
            f"unknown retention policy {retention_policy!r}; expected one of "
            f"{sorted(RETENTION_POLICY_DAYS)} or "
            f"{RetentionPolicy.PERMANENT.value!r}"
        )
    return submitted_at + timedelta(days=days), False


def submission_window_open(claim: AssignmentClaim, now: datetime) -> bool:
    """Is the claim's submission window open at ``now`` (spec §9.3/§11.4)?

    CLAIMED reads ``grace_deadline_at`` (strictly before it — §9.3 fixes
    the boundary instant itself as closed); REVISION_REQUIRED reads
    ``revision_deadline_at`` (which §11.4 keeps at or after grace, so the
    revision chance survives review delay). A missing or naive deadline
    fails closed instead of raising: no window, no submission.
    """
    status = ClaimStatus(claim.status)
    if status is ClaimStatus.REVISION_REQUIRED:
        deadline = claim.revision_deadline_at
    else:
        deadline = claim.grace_deadline_at
    if deadline is None or deadline.tzinfo is None:
        return False
    return now < deadline.astimezone(UTC)


# --- the policy rules -----------------------------------------------------------------


class UploadPolicyService:
    """The spec §10 step-2 checklist as pure, unit-testable rules.

    ``check`` evaluates the whole head — account (role, then status),
    ownership, claim submittability, declared type, size caps, submission
    window — in the spec's own order, raising the first failure's typed
    business code. Every fact arrives as an input (the locked rows, the
    task, the declarations, the clock instant), so the §9.3/§11.4 window
    boundaries and the §10 rejection matrix run with a FrozenClock and
    no database (backend-engineering §4/§21).
    """

    def require_active_student(self, claimer: Claimer) -> None:
        """Account gate on the locked row (spec §4.1, §5.7): role STUDENT
        and status ACTIVE. Role first — a role mismatch is a permission
        outcome even on a non-ACTIVE account (the transport guard's
        capability-then-state order)."""
        if claimer.role is not Role.STUDENT:
            raise UploaderNotStudentError(claimer.id, claimer.role)
        if claimer.status is not UserStatus.ACTIVE:
            raise AccountNotActiveError(claimer.id)

    def check(
        self,
        claimer: Claimer,
        claim: AssignmentClaim,
        task: Task,
        declared_type: str,
        declared_size: int,
        now: datetime,
        *,
        max_upload_bytes: int,
    ) -> None:
        """Run the §10 step-2 checklist head; return when eligible.

        Precedence: account (role, then status) -> ownership -> claim
        status -> declared type -> size caps -> window — the spec's own
        listing order, so a request failing several gates answers the
        most fundamental one.
        """
        self.require_active_student(claimer)
        self._require_owned_claim(claim, claimer.id)
        self._require_submittable_claim(claim)
        self._require_allowed_type(task, declared_type)
        self._require_within_size_caps(task, declared_size, max_upload_bytes)
        self._require_window_open(claim, now)

    @staticmethod
    def _require_owned_claim(claim: AssignmentClaim, user_id: UUID) -> None:
        if claim.user_id != user_id:
            raise ClaimNotOwnedError(claim.id, user_id)

    @staticmethod
    def _require_submittable_claim(claim: AssignmentClaim) -> None:
        status = ClaimStatus(claim.status)
        if status not in SUBMITTABLE_STATUSES:
            raise ClaimNotSubmittableError(status)

    @staticmethod
    def _require_allowed_type(task: Task, declared_type: str) -> None:
        allowed = list(task.allowed_file_types or [])
        if declared_type not in allowed:
            raise FileTypeNotAllowedError(declared_type, allowed)

    @staticmethod
    def _require_within_size_caps(
        task: Task, declared_size: int, max_upload_bytes: int
    ) -> None:
        # The binding cap is the smaller of the Task's own limit and the
        # deployment ceiling (spec §10: 文件上限默认 200 MB，可按 Task 调整 —
        # a Task may tighten, never widen past the deployment bound).
        limit = min(task.max_file_size_bytes, max_upload_bytes)
        if declared_size > limit:
            raise FileTooLargeError(declared_size, limit)

    @staticmethod
    def _require_window_open(claim: AssignmentClaim, now: datetime) -> None:
        if not submission_window_open(claim, now):
            raise SubmissionWindowClosedError(
                ClaimStatus(claim.status),
                claim.grace_deadline_at,
                claim.revision_deadline_at,
            )


# --- the async-validation handoff port (spec §10 step 8) ----------------------------


@runtime_checkable
class ValidationDispatcher(Protocol):
    """Hand a freshly finalized Submission to the async validation
    pipeline (the Celery job in production; a capturing fake in tests).

    ``enqueue_validation`` is a SYNC publish (a ``.delay`` call): the
    broker round trip is the port implementation's business, and keeping
    it sync means the no-broker fake is a plain list append.
    ``request_id`` is the correlation id the job threads through its
    logs (spec §15/§34); callers that have none get a generated hex.
    """

    def enqueue_validation(self, submission_id: UUID, request_id: str) -> None: ...


# --- the service ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuedUploadIntent:
    """What ``create_upload_intent`` hands back: the persisted intent's
    id, the short-lived presigned URL for the client, both expiries
    (URL and grant), and the signing contract PASSED THROUGH from the
    storage adapter — ``signed_headers`` is the adapter's
    ``UploadUrl.client_headers`` verbatim (the headers the client must
    echo; Content-Length is browser-forbidden and is not among them),
    plus ``pinned_content_length``, the exact byte count the PUT body
    must carry. The service never reconstructs these (hardening P4c:
    the adapter owns the signing policy; the application echoes).

    The PUBLIC wire shape (``UploadIntentResponse``) is built explicitly
    in ``schemas.py`` and never carries the object key (spec §40).
    """

    intent_id: UUID
    claim_id: UUID
    object_key: str
    upload_url: str
    url_expires_at: datetime
    intent_expires_at: datetime
    signed_headers: dict[str, str]
    pinned_content_length: int


class UploadService:
    """Issue and finalize presigned upload intents (spec §10 steps 1-7).

    ``clock`` is the business time source, ``storage`` the object-storage
    port (fake in tests), and the TTLs/byte cap are injectable scalars
    the composition root wires from Settings — the service itself never
    reads the environment (backend-engineering §11/§17). ``dispatcher``
    (optional) receives the async-validation handoff on a successful
    finalize (see the module docstring's dispatch rule); ``None`` keeps
    the pre-pipeline behavior for direct service callers.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        storage: ObjectStorage,
        upload_url_ttl: timedelta = DEFAULT_UPLOAD_URL_TTL,
        intent_ttl: timedelta = DEFAULT_INTENT_TTL,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        dispatcher: ValidationDispatcher | None = None,
    ) -> None:
        self._clock = clock
        self._storage = storage
        self._upload_url_ttl = upload_url_ttl
        self._intent_ttl = intent_ttl
        self._max_upload_bytes = max_upload_bytes
        self._policy = UploadPolicyService()
        self._dispatcher = dispatcher

    async def create_upload_intent(
        self,
        db: AsyncSession,
        actor: Actor,
        claim_id: UUID,
        filename: str,
        declared_type: str,
        size: int,
    ) -> IssuedUploadIntent:
        """Validate the §10 step-2 checklist and issue a single-use
        presigned upload grant.

        Raises the typed business codes (4xx) for every checklist failure;
        commits exactly once, only on the success path.
        """
        # (1) Same-user serialization FIRST (see module docstring), then
        # the account gate on the row we actually locked — role first,
        # then status.
        claimer = await self._lock_account(db, actor)

        # (2) Claim row under FOR UPDATE: ownership is judged on the
        # locked row.
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == claim_id)
            .with_for_update()
        )
        if claim is None:
            raise ClaimNotFoundError(claim_id)
        if claim.user_id != actor.user_id:
            raise ClaimNotOwnedError(claim_id, actor.user_id)

        # (3) Task facts via one lock-free SELECT (LOCK ORDER note in the
        # module docstring): a single statement reads one consistent row
        # version, and the §6.2/§13 snapshot contracts make later Task
        # edits non-retroactive anyway.
        task = await db.scalar(select(Task).where(Task.id == claim.task_id))
        if task is None:
            raise TaskNotFoundError(claim.task_id)

        # CLOCK SAMPLING CONTRACT (mirrors claim_service): ``now`` is
        # sampled HERE — after every lock the flow takes (user row FOR
        # UPDATE above, claim row FOR UPDATE above) and before the first
        # rule that consumes it. Sampling before the locks would let the
        # lock-wait skew the window comparison and the persisted
        # expires_at backwards by however long the lock was held.
        now = self._clock.now()

        # (4) The §10 step-2 checklist head as pure rules over the locked
        # rows, the task facts, and the declarations.
        self._policy.check(
            claimer,
            claim,
            task,
            declared_type,
            size,
            now,
            max_upload_bytes=self._max_upload_bytes,
        )

        # (5) Server-generated key + short-lived WRITE-ONCE, size-pinned
        # presigned URL via the port (spec §10: the key never derives
        # from the filename; the declared size is signed as the PUT's
        # Content-Length and the URL admits exactly one successful PUT).
        # A failed later commit strands this URL — it is never returned
        # and expires on its own.
        url = self._storage.create_upload_url(
            claim_id=claim.id,
            content_type=DECLARED_TYPE_CONTENT_TYPES[FileType(declared_type)],
            expires_in=self._upload_url_ttl,
            content_length=size,
        )

        # (6) Persist the single-use grant; one commit.
        intent = UploadIntent(
            claim_id=claim.id,
            object_key=url.object_key,
            filename=sanitize_filename(filename),
            declared_type=FileType(declared_type).value,
            declared_size=size,
            expires_at=now + self._intent_ttl,
        )
        db.add(intent)
        await db.flush()
        await db.commit()
        # The signing contract travels WITH the grant as an ADAPTER
        # PASSTHROUGH (hardening P4c): what was signed is exactly what
        # the client is told to send. This service no longer rebuilds
        # the header set from its own DECLARED_TYPE_CONTENT_TYPES view —
        # an independent reconstruction would desync from the real
        # signature the moment the adapter's signing policy changes.
        # Content-Length is not among the echoed headers (browser
        # forbidden header): it rides as the pinned byte count.
        if url.pinned_content_length is None:
            # Unreachable in this flow: the service always pins the
            # declared size above; an unsigned length back from the
            # adapter is a port-contract violation, not a client
            # outcome. Fail loud instead of echoing an untyped None.
            raise RuntimeError(
                "storage adapter returned an unsigned content length for "
                "a pinned upload intent"
            )
        return IssuedUploadIntent(
            intent_id=intent.id,
            claim_id=claim.id,
            object_key=url.object_key,
            upload_url=url.url,
            url_expires_at=url.expires_at,
            intent_expires_at=intent.expires_at,
            signed_headers=dict(url.client_headers),
            pinned_content_length=url.pinned_content_length,
        )

    async def finalize_upload(
        self,
        db: AsyncSession,
        actor: Actor,
        intent_id: UUID,
        *,
        request_id: str | None = None,
    ) -> Submission:
        """Verify the uploaded object and create the Submission version.

        Replaying a successful finalize returns the SAME Submission (spec
        §32); a burned or expired intent answers intent-not-found. Raises
        the typed business codes (4xx) otherwise; commits exactly once on
        the success path (the burn path commits its consumption alone).
        A successful finalize hands the Submission to the injected
        ``ValidationDispatcher`` when the pipeline has not started yet
        (see the module docstring's dispatch rule); ``request_id`` is the
        correlation id threaded to the job.
        """
        # (0) Locator + remote HEAD, lock-free and off the event loop
        # (module docstring step 0): the SELECT reads only immutable
        # columns, and the provider call happens BEFORE any row lock, so
        # a slow HEAD delays no lock and blocks no loop. Safe because the
        # upload URL is write-once: an existing object cannot change
        # afterwards, so this answer stays valid through the locked
        # transaction below. The result is stashed, never acted on here —
        # the state machine decides whether it matters.
        locator = (
            await db.execute(
                select(UploadIntent.claim_id, UploadIntent.object_key).where(
                    UploadIntent.id == intent_id
                )
            )
        ).one_or_none()
        if locator is None:
            raise UploadIntentNotFoundError(intent_id)
        head = await asyncio.to_thread(
            self._storage.head_object, object_key=locator.object_key
        )

        # (1) Same-user serialization + account gate as in create.
        await self._lock_account(db, actor)

        # (2) Claim row under FOR UPDATE: ownership is judged on the
        # locked row. NOTE (T2 carry, fixed in plan 04 task 7): the
        # claim-SUBMITTABILITY gate runs AFTER the intent state machine
        # below — a delayed replay of an already-finalized intent must
        # return the SAME Submission even when the claim has since
        # moved to VALIDATING/UNDER_REVIEW (the validation worker's
        # transition), instead of degrading the no-duplicate invariant
        # into a confusing CLAIM_NOT_SUBMITTABLE 409. The gate still
        # runs under the claim lock before any new Submission exists.
        claim = await db.scalar(
            select(AssignmentClaim)
            .where(AssignmentClaim.id == locator.claim_id)
            .with_for_update()
        )
        if claim is None:
            # The FK guarantees the row exists; this is the defensive
            # 404 for a corrupted graph.
            raise ClaimNotFoundError(locator.claim_id)
        if claim.user_id != actor.user_id:
            raise ClaimNotOwnedError(locator.claim_id, actor.user_id)
        status = ClaimStatus(claim.status)

        # (3) Intent row under FOR UPDATE: the authoritative single-use
        # state machine.
        intent = await db.scalar(
            select(UploadIntent).where(UploadIntent.id == intent_id).with_for_update()
        )
        if intent is None:
            raise UploadIntentNotFoundError(intent_id)
        if intent.finalized_submission_id is not None:
            # Idempotent replay (spec §32): the SAME Submission, never a
            # version N+1 — judged BEFORE the submittability gate (see
            # step 2's note). The stashed HEAD is irrelevant here even
            # when the object has since been retention-deleted.
            submission = await db.get(Submission, intent.finalized_submission_id)
            if submission is None:
                # Unreachable while the FK holds; fail safe, never
                # fabricate a second submission.
                raise UploadIntentNotFoundError(intent_id)
            self._dispatch_if_pipeline_not_started(submission, request_id)
            return submission
        if intent.consumed_at is not None:
            # Burned by a failed verification: the only remedy is a fresh
            # intent.
            raise UploadIntentNotFoundError(intent_id)

        # (4) Claim-submittability gate (moved after the replay; still
        # on the locked row, still before any write).
        if status not in SUBMITTABLE_STATUSES:
            raise ClaimNotSubmittableError(status)

        # CLOCK SAMPLING CONTRACT: after every lock (user, claim, intent)
        # and before the expiry comparison, the window recheck, and the
        # persisted submitted_at/consumed_at — one sample feeds all of
        # them, so lock-wait cannot skew the accepted instant.
        now = self._clock.now()
        if intent.expires_at.tzinfo is None or now >= intent.expires_at:
            raise UploadIntentNotFoundError(intent_id)
        if not submission_window_open(claim, now):
            raise SubmissionWindowClosedError(
                status, claim.grace_deadline_at, claim.revision_deadline_at
            )

        # (5) Object verification (spec §10 step 6) against the pre-lock
        # HEAD: write-once makes its size/content-type answer durable
        # through this transaction, so the consume decision and the
        # object state are still judged in one place — the locks were
        # just no longer held while the provider answered. With the
        # size-pinned, write-once URL both mismatch branches are defense
        # in depth; they stay for a deployment whose provider stops
        # enforcing the signed conditions.
        if head is None:
            # The client notified before the upload landed: retryable,
            # the intent stays consumable.
            raise UploadObjectMissingError(intent_id)
        expected_content_type = DECLARED_TYPE_CONTENT_TYPES[
            FileType(intent.declared_type)
        ]
        if head.size != intent.declared_size:
            await self._burn_intent(db, intent.id, now)
            raise UploadSizeMismatchError(intent.declared_size, head.size)
        if head.content_type != expected_content_type:
            await self._burn_intent(db, intent.id, now)
            raise UploadTypeMismatchError(
                intent.declared_type, head.content_type, expected_content_type
            )

        # (6) Retention snapshot from the Task's CURRENT policy at
        # finalize time (spec §13), one lock-free consistent read.
        task = await db.scalar(select(Task).where(Task.id == claim.task_id))
        if task is None:
            raise TaskNotFoundError(claim.task_id)
        retention_until, retention_permanent = retention_snapshot(
            task.retention_policy, now
        )

        # (7) Version allocation under the claim-row lock (spec §31.11):
        # monotonic per claim; UNIQUE(claim_id, version) is the database
        # backstop should the serialization ever be bypassed.
        current_max = await db.scalar(
            select(func.max(Submission.version)).where(Submission.claim_id == claim.id)
        )
        submission = Submission(
            claim_id=claim.id,
            version=(current_max or 0) + 1,
            object_key=intent.object_key,
            original_filename=intent.filename,
            declared_type=intent.declared_type,
            file_size=head.size,
            submitted_at=now,
            # Explicit (not the server default): the dispatch rule below
            # reads the attribute right after the commit, and an unloaded
            # server-default column would turn that read into a lazy load.
            validation_status=ValidationStatus.UPLOADED.value,
            retention_until=retention_until,
            retention_permanent=retention_permanent,
        )
        db.add(submission)
        await db.flush()  # populate the server-generated id

        # (8) Single-use consume: the conditional UPDATE is the atomic
        # guard itself; under the intent-row lock it always matches one
        # row, and a zero rowcount fails safe instead of doubling. The
        # cast is the typed seam for `CursorResult.rowcount` (the
        # declared `Result` type does not carry it, but every driver
        # result here does — session_service's informational getattr
        # variant would silently mask a missing attribute; this check is
        # load-bearing).
        consumed = cast(
            "CursorResult[Any]",
            await db.execute(
                update(UploadIntent)
                .where(UploadIntent.id == intent.id, UploadIntent.consumed_at.is_(None))
                .values(consumed_at=now, finalized_submission_id=submission.id)
            ),
        )
        if consumed.rowcount != 1:
            await db.rollback()
            raise UploadIntentNotFoundError(intent_id)

        # (9) Claim projection + one commit.
        claim.latest_submission_id = submission.id
        await db.commit()
        self._dispatch_if_pipeline_not_started(submission, request_id)
        return submission

    def _dispatch_if_pipeline_not_started(
        self, submission: Submission, request_id: str | None
    ) -> None:
        """Hand the submission to the async-validation pipeline when it is
        still UPLOADED (see the module docstring): after the commit on the
        fresh path, and on the replay path only as the lost-enqueue
        recovery — VALIDATING or terminal replays mean the pipeline is
        alive and the job's own idempotency already covers it.
        """
        if self._dispatcher is None:
            return
        if submission.validation_status != ValidationStatus.UPLOADED.value:
            return
        self._dispatcher.enqueue_validation(
            submission.id, request_id if request_id is not None else uuid4().hex
        )

    async def _lock_account(self, db: AsyncSession, actor: Actor) -> Claimer:
        """Lock the actor's user row and gate role+status on it (the
        claim_service pattern; see module docstring step 1)."""
        account = (
            await db.execute(
                select(_USERS_LOCK.c.status, _USERS_LOCK.c.role)
                .where(_USERS_LOCK.c.id == actor.user_id)
                .with_for_update()
            )
        ).one_or_none()
        if account is None:
            raise UserNotFoundError(actor.user_id)
        claimer = Claimer(
            id=actor.user_id,
            status=UserStatus(account.status),
            role=Role(account.role),
        )
        self._policy.require_active_student(claimer)
        return claimer

    async def _burn_intent(
        self, db: AsyncSession, intent_id: UUID, now: datetime
    ) -> None:
        """Consume an intent WITHOUT a finalized submission: the stored
        object contradicted the cleared declaration, so the grant is
        corrupt and the remedy is a fresh intent. The consumption is
        committed before the typed error raises, so the burn survives."""
        await db.execute(
            update(UploadIntent)
            .where(UploadIntent.id == intent_id, UploadIntent.consumed_at.is_(None))
            .values(consumed_at=now)
        )
        await db.commit()
