# backend/app/modules/submissions/router.py
"""Submission/review HTTP API: thin routes + the module's composition
root (spec §10 steps 1-8, §11, §12.4, §14, §28 URL shapes, §29 envelope,
§33.1 rate limit, §33.3 short-lived download links, §40 no object keys;
backend-engineering §3, §13, §16).

Every route is thin — parse transport input, resolve dependencies, call
one service, serialize an explicit DTO — and owns no persistence logic.

Endpoints
---------

Student surface (``require_active_student_actor``, spec §4.1: the claim
lifecycle a submission hangs on is a Student capability):

===========  =========================================================
Method path  Purpose
===========  =========================================================
POST         ``/submissions/upload-intent`` — the §10 step-1 grant:
             claim + filename + declared type + size in, the
             single-use intent + SHORT-LIVED presigned upload URL out
             (rate-limited per user id).
POST         ``/submissions/upload-complete`` — the §10 steps 5-7
             finalize: intent id in the BODY, the created (or
             replayed, spec §32) Submission out, and the async
             validation job enqueued through the injected dispatcher.
GET          ``/submissions/{submission_id}/validation`` — the owner's
             §12.4 report view (no object key, no parser internals).
GET          ``/submissions/{submission_id}/download`` — a short-lived
             presigned GET minted AFTER the ownership/role check; the
             response carries the URL, never the key (spec §33.3).
===========  =========================================================

URL-shape ruling (documented deviation inside the spec §28 family): the
spec lists ``POST /submissions/{submission_id}/upload-complete``, but
the resource being completed is the single-use INTENT — no Submission
exists before the call succeeds, so there is no id to put in the path.
The route keeps the spec's ``/submissions/upload-complete`` segment with
``intent_id`` in the body (spec §28 allows URL adjustments; the domain
behavior and permission semantics are unchanged).

Teacher surface (``require_staff_management_actor``, spec §33.4 — staff
role + ACTIVE + confirmed TOTP; ownership/collaborator standing is
judged in the services/query):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/teacher/submissions/review-queue`` — the §41 queue:
             VALIDATED-but-undecided submissions on own/collaborated
             tasks, oldest first, offset-paginated, with claim/task
             context, the locked tier, the validation summary +
             preview, and the API download path.
POST         ``/teacher/submissions/{submission_id}/approve`` — the
             §14 ten-step transaction (idempotent replay answers
             ``already_reviewed``).
POST         ``/teacher/submissions/{submission_id}/revision-required``
             — §11.3 退回; the note is mandatory at the transport.
POST         ``/teacher/submissions/{submission_id}/invalidate-reward-lock``
             — §11.3 判无效; the reason is mandatory (audited).
===========  =========================================================

Composition root
----------------

- **The async-validation handoff** (spec §10 step 8): a small
  ``ValidationDispatcher`` port (defined in ``upload_service``) carries
  the enqueue out of the request handler. The production binding below
  is the real ``.delay`` publish on the shared Celery app the API
  factory's lifespan binds; tests override the provider with a
  capturing fake — deliberately NOT Celery eager mode, so no broker is
  contacted and the validation itself never runs inline against a
  storage adapter the test would have to fake anyway. ``request_id`` is
  threaded from the request middleware into the job's correlation id.
- **Object storage** (spec §10/§33.3) arrives through the
  ``ObjectStorage`` port. The provider is the settings-driven factory
  seam: the object-storage provider adapter does not exist yet, so no
  environment has a default binding — deployments and tests inject an
  implementation through this provider (tests bind the in-memory fake).
  Failing loudly beats silently talking to nothing.
- **The points port** (spec §14 step 8) is the points-module ledger
  adapter, constructed per request over the request's session: the
  grant joins the approve transaction and the caller's commit decides
  it (the adapter never commits — backend-engineering §5). Service-level
  tests bind the in-memory fake through the provider override.
- Services are assembled per request from injected dependencies; the
  rate limiter reuses the shared ``RateLimiter`` port with the
  ``submissions:upload-intent`` rule, normalized to the authenticated
  user id (spec §33.1 — an anti-hammering window; the business ceilings
  live in the services).

Status-code mapping: every submissions-module typed exception
subclasses ``BusinessError`` and reaches the core envelope handler
unchanged — ownership/role denials render ``PERMISSION_DENIED`` 403,
unknown aggregates ``NOT_FOUND`` 404, claim/window/lock conflicts the
frozen 409 family, blank review text ``VALIDATION_ERROR`` 400/422. Only
the endpoint limiter's ``RateLimitExceededError`` needs a handler here
(the tasks-router precedent; last-writer-wins between identical
renders).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from functools import lru_cache
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import error_envelope
from app.core.observability import REQUEST_ID_HEADER
from app.db.session import get_db_session
from app.integrations.object_storage import ObjectStorage
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_actor,
    require_active_student_actor,
    require_staff_management_actor,
)
from app.modules.identity.events import (
    Actor,
    DomainEventPublisher,
    LoggingEventPublisher,
)
from app.modules.points.ledger_service import PointsRewardPortAdapter
from app.modules.submissions.query_service import (
    ReviewQueueItem,
    SubmissionQueryService,
)
from app.modules.submissions.review_service import PointsRewardPort, ReviewService
from app.modules.submissions.schemas import (
    ApproveResponse,
    DownloadUrlResponse,
    InvalidateRewardLockRequest,
    ReviewQueueItemResponse,
    ReviewQueueResponse,
    RevisionRequiredRequest,
    RevisionRequiredResponse,
    SubmissionPublic,
    SubmissionValidationResponse,
    UploadCompleteRequest,
    UploadIntentRequest,
    UploadIntentResponse,
    ValidationReportPayload,
)
from app.modules.submissions.upload_service import (
    UploadService,
    ValidationDispatcher,
)

# --- pagination bounds (the documented offset choice) ------------------------------

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

# Spec §33.3: download links are short-lived. Minutes, not hours — the
# client re-requests through the authorized route when it expires.
DEFAULT_DOWNLOAD_URL_TTL = timedelta(minutes=10)


# --- typed-exception -> envelope mapping ------------------------------------------


def register_submissions_exception_handlers(app: FastAPI) -> None:
    """Attach the one non-BusinessError envelope handler this module can
    raise (the endpoint limiter's), through the same envelope builder as
    ``core.errors`` — the tasks-router precedent."""

    async def render_rate_limited(
        request: StarletteRequest, exc: Exception
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        headers = {REQUEST_ID_HEADER: request_id} if request_id else None
        return JSONResponse(
            status_code=429,
            content=error_envelope(ErrorCode.RATE_LIMITED, str(exc), None, request_id),
            headers=headers,
        )

    app.add_exception_handler(RateLimitExceededError, render_rate_limited)


# --- provider dependencies (module composition root) ------------------------------


@lru_cache
def get_submissions_redis() -> aioredis.Redis:
    """Process-wide Redis client for the endpoint rate limiter; tests
    override this dependency to point at the flushed test database (a
    direct call inside a provider would dodge ``dependency_overrides``)."""
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


RedisDep = Annotated[aioredis.Redis, Depends(get_submissions_redis)]
ClockDep = Annotated[Clock, Depends(get_business_clock)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_rate_limiter(clock: ClockDep, redis: RedisDep) -> RateLimiter:
    return RedisFixedWindowLimiter(redis=redis, clock=clock)


def get_object_storage() -> ObjectStorage:
    """Object-storage adapter factory (the composition seam).

    Production binding (hardening P0-1): ``S3ObjectStorage`` built from
    ``Settings`` — the required s3_* fields make a misconfigured
    deployment fail at Settings construction, so there is no silent
    fallback anywhere in this chain (the adapter's docstring documents
    the fail-closed wiring). Tests keep overriding this provider with
    the in-memory fake (``dependency_overrides``), which is exactly the
    seam this factory exists to provide.
    """
    from app.integrations.object_storage_s3 import S3ObjectStorage

    return S3ObjectStorage(get_settings())


class CeleryValidationDispatcher:
    """``ValidationDispatcher`` over ``validate_submission_job.delay``
    (the production binding).

    The job module import stays INSIDE the call: importing it at router
    module load would drag the validator sandbox into every API process
    start for a dispatch that may never fire.
    """

    def enqueue_validation(self, submission_id: uuid.UUID, request_id: str) -> None:
        from app.workers.jobs.validate_submission import validate_submission_job

        validate_submission_job.delay(str(submission_id), request_id)


def get_validation_dispatcher() -> ValidationDispatcher:
    """The production handoff: publish the real Celery task on the
    shared app the API factory's lifespan binds. Tests override this
    provider with a capturing fake — not eager mode, no broker."""
    return CeleryValidationDispatcher()


def get_event_publisher() -> DomainEventPublisher:
    """Interim adapter; the audit/outbox module wires persistent
    dispatch (the tasks-router seam's twin)."""
    return LoggingEventPublisher()


def get_points_port(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> PointsRewardPort:
    """The §14 step-8 grant port: the points-module ledger adapter over
    THIS request's session, constructed per request. The adapter uses
    the caller's session and never commits, so the grant joins the
    approve transaction and the route's commit/rollback decides its
    fate (the task-2 verified contract). The ledger arrives from the
    POINTS composition root with the default ranking-projection trigger
    bound (final-review C1: the workers' ``CeleryRankingDispatcher`) —
    the committed grant enqueues the board recompute through the
    ledger's constructor default, and the adapter threads the approve's
    idempotency key as the job's request_id. Service-level tests keep
    the in-memory fake through this provider override."""
    from app.modules.points.router import get_ledger_service

    return PointsRewardPortAdapter(ledger=get_ledger_service(), db=db)


def get_upload_service(
    clock: ClockDep,
    settings: AppSettings,
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    dispatcher: Annotated[ValidationDispatcher, Depends(get_validation_dispatcher)],
) -> UploadService:
    return UploadService(
        clock=clock,
        storage=storage,
        max_upload_bytes=settings.max_upload_bytes_default,
        dispatcher=dispatcher,
    )


def get_review_service(
    clock: ClockDep,
    events: Annotated[DomainEventPublisher, Depends(get_event_publisher)],
    points: Annotated[PointsRewardPort, Depends(get_points_port)],
) -> ReviewService:
    # The honor trigger binding (final review I3): the rankings module's
    # ClaimCompletedHonorsTrigger over the real HonorService. The
    # service's wrapper makes it failure-tolerant, so production binds
    # the real evaluation unconditionally; service-level tests inject
    # fakes or None through ReviewService directly.
    #
    # The notification recorder is the MERGE_CARRIES item 2 production
    # wiring: NotificationPort joins each review transaction so the
    # REVISION_REQUIRED / SUBMISSION_APPROVED intent commits with the
    # decision or not at all (the outbox rule). THE SAME clock instance
    # stamps the review decision and the notification registration
    # instants, and the import lives at this composition root — the one
    # layer allowed to see both modules (the tasks-router precedent).
    from app.modules.notifications.port import NotificationPort
    from app.modules.rankings.honor_service import ClaimCompletedHonorsTrigger

    return ReviewService(
        clock=clock,
        events=events,
        points=points,
        honors=ClaimCompletedHonorsTrigger(),
        notification_recorder=NotificationPort(clock=clock),
    )


def get_query_service() -> SubmissionQueryService:
    return SubmissionQueryService()


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
StudentActor = Annotated[Actor, Depends(require_active_student_actor)]
ActiveActor = Annotated[Actor, Depends(require_active_actor)]
StaffActor = Annotated[Actor, Depends(require_staff_management_actor)]
StorageDep = Annotated[ObjectStorage, Depends(get_object_storage)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
UploadServiceDep = Annotated[UploadService, Depends(get_upload_service)]
ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]
QueryServiceDep = Annotated[SubmissionQueryService, Depends(get_query_service)]

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]


async def _enforce_rate_limit(
    limiter: RateLimiter, bucket: str, identifier: str
) -> None:
    rule = RATE_LIMIT_RULES[bucket]
    await limiter.check(
        bucket=rule.bucket,
        identifier=identifier,
        limit=rule.limit,
        window_seconds=rule.window_seconds,
    )


# --- student surfaces (spec §10, §28) ------------------------------------------------

router = APIRouter()


@router.post(
    "/submissions/upload-intent", response_model=UploadIntentResponse, status_code=201
)
async def create_upload_intent(
    body: UploadIntentRequest,
    actor: StudentActor,
    db: DbSession,
    uploads: UploadServiceDep,
    limiter: LimiterDep,
) -> UploadIntentResponse:
    """Issue the single-use presigned upload grant (spec §10 step 1-5).

    The browser PUTs the file straight to storage — the 200 MB payload
    never streams through this API. The response carries the intent id,
    the short-lived URL, and the URL's expiry; the server-generated
    object key stays server-side (spec §40).
    """
    await _enforce_rate_limit(limiter, "submissions:upload-intent", str(actor.user_id))
    intent = await uploads.create_upload_intent(
        db,
        actor,
        claim_id=body.claim_id,
        filename=body.filename,
        declared_type=body.declared_type.value,
        size=body.size,
    )
    return UploadIntentResponse(
        intent_id=intent.intent_id,
        upload_url=intent.upload_url,
        expires_at=intent.url_expires_at,
        headers=intent.signed_headers,
    )


@router.post("/submissions/upload-complete", response_model=SubmissionPublic)
async def complete_upload(
    request: Request,
    body: UploadCompleteRequest,
    actor: StudentActor,
    db: DbSession,
    uploads: UploadServiceDep,
) -> SubmissionPublic:
    """Verify the uploaded object and create the Submission version
    (spec §10 steps 5-7), then hand it to the async validation pipeline
    through the dispatcher (step 8). Replaying a completed intent
    returns the SAME Submission (spec §32).

    The object-key-free public DTO rides the response; ``created_at``
    is a server default the INSERT did not return, so the row is
    refreshed before serialization (the tasks-router create precedent).
    """
    submission = await uploads.finalize_upload(
        db,
        actor,
        body.intent_id,
        request_id=getattr(request.state, "request_id", None),
    )
    await db.refresh(submission)
    return SubmissionPublic.from_domain(submission)


@router.get(
    "/submissions/{submission_id}/validation",
    response_model=SubmissionValidationResponse,
)
async def get_submission_validation(
    submission_id: uuid.UUID,
    actor: StudentActor,
    db: DbSession,
    queries: QueryServiceDep,
) -> SubmissionValidationResponse:
    """The owner's machine-validation report (spec §11.1, §12.4) — the
    persisted §12.4 shape plus the status projections; ``report`` is
    null until the async run finishes."""
    view = await queries.get_validation(db, actor, submission_id)
    return SubmissionValidationResponse(
        submission_id=view.submission_id,
        claim_id=view.claim_id,
        version=view.version,
        validation_status=view.validation_status,
        review_status=view.review_status,
        detected_type=view.detected_type,
        report=(
            ValidationReportPayload.from_persisted(view.report)
            if view.report is not None
            else None
        ),
    )


@router.get("/submissions/{submission_id}/download", response_model=DownloadUrlResponse)
async def download_submission(
    submission_id: uuid.UUID,
    actor: ActiveActor,
    db: DbSession,
    storage: StorageDep,
    queries: QueryServiceDep,
) -> DownloadUrlResponse:
    """Mint a short-lived presigned GET (spec §33.3).

    The guard is the broad ACTIVE one because BOTH authorized parties
    reach this route — the owning Student and the reviewing teacher
    (task owner / REVIEW_SUBMISSIONS collaborator / Admin); the actual
    ownership/role judgment runs inside the query service BEFORE the
    port signs. The response carries the URL, never the key.
    """
    url = await queries.create_download_url(
        db,
        actor,
        submission_id,
        storage,
        expires_in=DEFAULT_DOWNLOAD_URL_TTL,
    )
    return DownloadUrlResponse(url=url.url, expires_at=url.expires_at)


# --- teacher surfaces (spec §11.3, §14, §28, §41) ------------------------------------


@router.get("/teacher/submissions/review-queue", response_model=ReviewQueueResponse)
async def list_review_queue(
    actor: StaffActor,
    db: DbSession,
    queries: QueryServiceDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> ReviewQueueResponse:
    """One offset page of the review queue: the VALIDATED submissions
    still awaiting a decision on the actor's own/collaborated tasks,
    oldest first, with the reviewer's judging context."""
    items, total = await queries.list_review_queue(
        db, actor, limit=limit, offset=offset
    )
    return ReviewQueueResponse(
        items=[_queue_item_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def _queue_item_response(item: ReviewQueueItem) -> ReviewQueueItemResponse:
    """Serialize one queue row; the download link is the API path that
    mints the presigned URL per request (a presigned URL embedded in a
    listed page would expire under the client)."""
    return ReviewQueueItemResponse(
        submission_id=item.submission_id,
        claim_id=item.claim_id,
        task_id=item.task_id,
        task_title=item.task_title,
        platform=item.platform,
        keyword=item.keyword,
        version=item.version,
        original_filename=item.original_filename,
        declared_type=item.declared_type,
        detected_type=item.detected_type,
        file_size=item.file_size,
        submitted_at=item.submitted_at,
        review_status=item.review_status,
        claim_status=item.claim_status,
        reward_tier_locked=item.reward_tier_locked,
        locked_reward_points=item.locked_reward_points,
        validation=(
            ValidationReportPayload.from_persisted(item.validation)
            if item.validation is not None
            else None
        ),
        download_url=f"/api/v1/submissions/{item.submission_id}/download",
    )


@router.post(
    "/teacher/submissions/{submission_id}/approve", response_model=ApproveResponse
)
async def approve_submission(
    submission_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    reviews: ReviewServiceDep,
) -> ApproveResponse:
    """Run the §14 ten-step approve transaction (claim COMPLETED, lock
    CONFIRMED, one grant, assignment COMPLETED); a replay on a COMPLETED
    claim answers ``already_reviewed`` with nothing written."""
    result = await reviews.approve_submission(db, actor, submission_id)
    return ApproveResponse(
        claim_id=result.claim.id,
        claim_status=result.claim.status,
        reward_lock_status=result.claim.reward_lock_status,
        points_granted=result.grant.points_granted if result.grant else None,
        already_reviewed=result.already_reviewed,
    )


@router.post(
    "/teacher/submissions/{submission_id}/revision-required",
    response_model=RevisionRequiredResponse,
)
async def require_revision(
    submission_id: uuid.UUID,
    body: RevisionRequiredRequest,
    actor: StaffActor,
    db: DbSession,
    reviews: ReviewServiceDep,
) -> RevisionRequiredResponse:
    """Return the submission for fixes (spec §11.3): the claim moves to
    REVISION_REQUIRED with the §11.4 window; the existing lock survives.
    The note is mandatory at the transport (the teacher's guidance)."""
    claim = await reviews.require_revision(db, actor, submission_id, body.note)
    return RevisionRequiredResponse(
        claim_id=claim.id,
        claim_status=claim.status,
        reward_lock_status=claim.reward_lock_status,
        revision_deadline_at=claim.revision_deadline_at,
    )


@router.post(
    "/teacher/submissions/{submission_id}/invalidate-reward-lock",
    response_model=RevisionRequiredResponse,
)
async def invalidate_reward_lock(
    submission_id: uuid.UUID,
    body: InvalidateRewardLockRequest,
    actor: StaffActor,
    db: DbSession,
    reviews: ReviewServiceDep,
) -> RevisionRequiredResponse:
    """Cancel the PROVISIONAL lock and demand a resubmission (spec
    §11.3): the projection clears, the cancelled values survive on the
    append-only audit rows, and the reason is mandatory."""
    claim = await reviews.invalidate_reward_lock(db, actor, submission_id, body.reason)
    return RevisionRequiredResponse(
        claim_id=claim.id,
        claim_status=claim.status,
        reward_lock_status=claim.reward_lock_status,
        revision_deadline_at=claim.revision_deadline_at,
    )
