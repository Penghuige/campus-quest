# backend/app/modules/tasks/router.py
"""Task/claim HTTP API: thin routes + the module's composition root.

Spec §6-§9 (tasks/assignments/claims/deadlines), §28 (``/api/v1`` prefix
and URL shapes), §29 (envelope), §33.1 (rate limit), §40/§42 (privacy:
card fields, no Assignment lists, own-claim-only platform/keyword), §41
(Teacher workbench); backend-engineering §3 (router standard), §9 (DTO
separation), §16 (security boundary).

Every route is thin — parse transport input, resolve dependencies, call
one service, serialize an explicit DTO — and owns no persistence logic.
The heavier design decisions live here:

Endpoints
---------

Student surfaces (``require_active_actor``; spec §4.1, §5.7 — an ACTIVE
account of any role may browse and claim):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/tasks`` — offset-paginated PUBLISHED cards (§42).
GET          ``/tasks/{task_id}`` — published detail + own claim.
POST         ``/tasks/{task_id}/claim`` — random claim (§8.3).
GET          ``/me/claims`` — own claim history (all statuses).
POST         ``/claims/{claim_id}/abandon`` — abandon + release (§8.5).
===========  =========================================================

Teacher surfaces (``require_staff_management_actor``; spec §4.2-§4.3,
§33.4 — staff role + ACTIVE + confirmed TOTP; ownership/collaborator
checks live in the services):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/teacher/tasks`` — workbench list: own + collaborated
             tasks, every status incl. DRAFT (§41).
GET          ``/teacher/tasks/{task_id}`` — full workbench detail incl.
             contract fields (owner/collaborator/Admin).
POST         ``/teacher/tasks`` — create DRAFT.
PATCH        ``/teacher/tasks/{task_id}`` — edit under the V1 rule.
POST         ``/teacher/tasks/{task_id}/publish|pause|resume|close|archive``
POST         ``/teacher/tasks/{task_id}/assignments/import/preview``
POST         ``/teacher/tasks/{task_id}/assignments/import/confirm``
PUT          ``/teacher/tasks/{task_id}/collaborators/{teacher_id}``
DELETE       ``/teacher/tasks/{task_id}/collaborators/{teacher_id}``
GET          ``/teacher/tasks/{task_id}/statistics``
===========  =========================================================

Status-code mapping
-------------------

Every tasks-module typed exception subclasses ``BusinessError`` and
reaches the core envelope handler unchanged — this table IS the mapping,
chosen for stability: the business-conflict family renders 409, state
gates 403, unknown aggregates 404.

============================================  =======================  ======
Typed exception                                Envelope code             HTTP
============================================  =======================  ======
``TaskNotFoundError`` (and the read side's     ``NOT_FOUND``              404
not-visible/unknown id paths:
a missing aggregate IS the system-404 semantic,
no registry change)
``UserNotFoundError``                          ``NOT_FOUND``              404
``ClaimNotFoundError``                         ``NOT_FOUND``              404
``CollaboratorNotFoundError``                  ``NOT_FOUND``              404
``InvalidPreviewTokenError``                   ``NOT_FOUND``              404
``ClaimNotOwnedError`` + every service/        ``PERMISSION_DENIED``      403
guard role/ownership denial
``AccountNotActiveError``                      ``ACCOUNT_NOT_ACTIVE``     403
``TaskNotClaimableError`` (incl. the publish   ``TASK_NOT_CLAIMABLE``     409
gate's cutoff block, aligned here)
``ClaimCutoffReachedError``                    ``CLAIM_CUTOFF_REACHED``   409
``AssignmentLimitReachedError``                ``ASSIGNMENT_LIMIT_REACHED`` 409
``ActiveClaimExistsError``                     ``TASK_ACTIVE_CLAIM_EXISTS`` 409
``NoAssignmentAvailableError``                 ``NO_ASSIGNMENT_AVAILABLE`` 409
``AbandonLimitReachedError``                   ``ABANDON_LIMIT_REACHED``  409
``ClaimNotAbandonableError``                   ``CLAIM_NOT_ABANDONABLE``  409
``DuplicateCollaboratorError``                 ``VALIDATION_ERROR``       409
``DuplicateAssignmentsError``                  ``VALIDATION_ERROR``       409
``IllegalTransitionError`` /                   ``VALIDATION_ERROR``       400
``ImmutableTaskFieldError`` / create-edit
field validation
``RateLimitExceededError``                     ``RATE_LIMITED``           429
============================================  =======================  ======

Other transport decisions
-------------------------

- **Pagination: offset** (the documented V1 choice) for ``/tasks`` and
  ``/me/claims`` — ``limit`` (1..50, default 20) + ``offset`` >= 0, with
  ``total`` so clients can render page counts. Cursor pagination
  revisits when the catalogue outgrows stable-offset assumptions.
- **Claim requests cannot choose work.** ``ClaimRequest`` is fieldless
  with ``extra="forbid"``: an ``assignment_id`` in the body is a 422
  before any service call (the test pins it); random allocation stays a
  server decision under lock (spec §8.3).
- **Import preview is a raw-body upload.** The CSV travels as the
  request body (``Content-Type: text/csv``) instead of multipart — the
  importer wants bytes, the byte cap fast-fails before any parsing, and
  no form-multipart dependency is pulled in for one file field.
- **Rate limiting (spec §33.1)** on claim + abandon, normalized to the
  authenticated user id through the shared ``RateLimiter`` port and
  ``RATE_LIMIT_RULES`` (``tasks:claim``, ``claims:abandon``); an
  exhausted window renders the 429 envelope. These windows are
  anti-hammering — the business ceilings (§8.2 quota, §8.5 daily cap)
  live in the services.
- **No CSRF obligation here.** The refresh cookie is scoped to
  ``/api/v1/auth`` (identity router); these routes authorize purely by
  Bearer header, so no ambient cookie authority exists to forge with.
- **Providers are the module composition root.** The Redis client is
  process-cached; services are assembled per request from injected
  clock/settings/publisher dependencies, so tests override a dependency,
  never service internals. ``Settings``-driven wiring:
  ``daily_abandon_limit`` + ``business_timezone`` into
  ``AbandonService``, the ``assignment_import_*`` caps into the importer,
  ``max_upload_bytes_default`` into ``TaskService``. The rating summary
  port ships as ``NullRatingSummaryPort`` until the community module
  wires the real adapter.
"""

from __future__ import annotations

import uuid
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
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_actor,
    require_staff_management_actor,
)
from app.modules.identity.directory import SqlAlchemyUserDirectory
from app.modules.identity.events import (
    Actor,
    DomainEventPublisher,
    LoggingEventPublisher,
)
from app.modules.tasks.abandon_service import AbandonService
from app.modules.tasks.claim_service import ClaimService
from app.modules.tasks.collaborator_service import TaskCollaboratorService
from app.modules.tasks.importer import AssignmentImportService
from app.modules.tasks.models import Task
from app.modules.tasks.query_service import (
    NullRatingSummaryPort,
    RatingSummaryPort,
    TaskQueryService,
)
from app.modules.tasks.schemas import (
    ClaimRequest,
    ClaimResponse,
    CollaboratorAddRequest,
    CollaboratorResponse,
    CreateTask,
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
    MyClaimResponse,
    MyClaimsResponse,
    PublishedTaskDetail,
    TaskCardResponse,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskListResponse,
    TaskStatisticsResponse,
    TaskTransitionResponse,
    TaskUpdateRequest,
    TeacherTaskListItemResponse,
    TeacherTaskListResponse,
    TeacherTaskResponse,
    UpdateTask,
)
from app.modules.tasks.service import TaskService

# --- pagination bounds (the documented offset choice) ----------------------------

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

# --- typed-exception -> envelope mapping ------------------------------------------


def register_tasks_exception_handlers(app: FastAPI) -> None:
    """Attach the one non-BusinessError envelope handler this module can
    raise (the endpoint limiter's), through the same envelope builder as
    ``core.errors``.

    Every tasks-module typed exception already subclasses
    ``BusinessError`` with its frozen code/status (see the module
    docstring table), so the core handler renders them unchanged. The
    identity router registers an equivalent ``RateLimitExceededError``
    handler; re-registering here keeps this module self-contained —
    last-writer-wins between two identical renders.
    """

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
def get_tasks_redis() -> aioredis.Redis:
    """Process-wide Redis client for import preview tokens.

    ``decode_responses=True`` keeps replies as ``str`` (the importer
    JSON-decodes anyway); tests override this dependency to point at the
    flushed test database — a direct call inside a provider would dodge
    ``dependency_overrides``, which is the seam integration tests use.
    """
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


RedisDep = Annotated[aioredis.Redis, Depends(get_tasks_redis)]
ClockDep = Annotated[Clock, Depends(get_business_clock)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_rate_limiter(clock: ClockDep, redis: RedisDep) -> RateLimiter:
    return RedisFixedWindowLimiter(redis=redis, clock=clock)


def get_event_publisher() -> DomainEventPublisher:
    """Interim adapter; the audit/outbox module wires persistent dispatch
    (see the outbox contract in docs/architecture/interfaces.md)."""
    return LoggingEventPublisher()


def get_rating_summary_port() -> RatingSummaryPort:
    """Interim stand-in; the community module wires the TaskRating-backed
    adapter."""
    return NullRatingSummaryPort()


def get_task_service(clock: ClockDep, settings: AppSettings) -> TaskService:
    return TaskService(clock=clock, max_upload_bytes=settings.max_upload_bytes_default)


def get_claim_service(clock: ClockDep) -> ClaimService:
    # max_active_claims stays at the spec §8.2 default of 3.
    return ClaimService(clock=clock)


def get_abandon_service(
    clock: ClockDep,
    settings: AppSettings,
    events: Annotated[DomainEventPublisher, Depends(get_event_publisher)],
) -> AbandonService:
    return AbandonService(
        clock=clock,
        business_timezone=settings.business_timezone,
        events=events,
        daily_abandon_limit=settings.daily_abandon_limit,
    )


def get_import_service(
    redis: RedisDep, clock: ClockDep, settings: AppSettings
) -> AssignmentImportService:
    return AssignmentImportService(
        redis=redis,
        clock=clock,
        max_file_bytes=settings.assignment_import_max_file_bytes,
        max_rows=settings.assignment_import_max_rows,
        keyword_max_length=settings.assignment_import_keyword_max_length,
        preview_ttl_seconds=settings.assignment_import_preview_ttl_seconds,
    )


def get_collaborator_service() -> TaskCollaboratorService:
    return TaskCollaboratorService(user_directory=SqlAlchemyUserDirectory())


def get_task_query_service() -> TaskQueryService:
    return TaskQueryService()


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
ActiveActor = Annotated[Actor, Depends(require_active_actor)]
StaffActor = Annotated[Actor, Depends(require_staff_management_actor)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
RatingPortDep = Annotated[RatingSummaryPort, Depends(get_rating_summary_port)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
ClaimServiceDep = Annotated[ClaimService, Depends(get_claim_service)]
AbandonServiceDep = Annotated[AbandonService, Depends(get_abandon_service)]
ImportServiceDep = Annotated[AssignmentImportService, Depends(get_import_service)]
CollaboratorServiceDep = Annotated[
    TaskCollaboratorService, Depends(get_collaborator_service)
]
QueryServiceDep = Annotated[TaskQueryService, Depends(get_task_query_service)]

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


# --- student surfaces (spec §28, §42) -----------------------------------------------

router = APIRouter()


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    actor: ActiveActor,
    db: DbSession,
    queries: QueryServiceDep,
    ratings: RatingPortDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> TaskListResponse:
    """One offset page of PUBLISHED task cards (spec §42): counts and card
    facts only — the Assignment list never rides along."""
    cards, total = await queries.list_published_tasks(
        db, rating_port=ratings, limit=limit, offset=offset
    )
    return TaskListResponse(
        items=[TaskCardResponse.from_view(card) for card in cards],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: uuid.UUID,
    actor: ActiveActor,
    db: DbSession,
    queries: QueryServiceDep,
    ratings: RatingPortDep,
) -> TaskDetailResponse:
    """Published detail + the viewer's own non-terminal claim, if any."""
    detail: PublishedTaskDetail = await queries.get_published_task(
        db, task_id=task_id, viewer_id=actor.user_id, rating_port=ratings
    )
    return TaskDetailResponse.from_view(detail)


@router.post("/tasks/{task_id}/claim", response_model=ClaimResponse, status_code=201)
async def claim_task(
    task_id: uuid.UUID,
    actor: ActiveActor,
    db: DbSession,
    claims: ClaimServiceDep,
    queries: QueryServiceDep,
    limiter: LimiterDep,
    body: ClaimRequest | None = None,
) -> ClaimResponse:
    """Claim one random AVAILABLE assignment (spec §8.3).

    ``ClaimRequest`` is fieldless and forbids extras: the caller cannot
    name an assignment — the server picks randomly under lock. The
    response is owner-scoped and names the assigned unit's
    platform/keyword (the only student surface that ever does).
    """
    await _enforce_rate_limit(limiter, "tasks:claim", str(actor.user_id))
    claim = await claims.claim_random_assignment(db, actor.user_id, task_id)
    view = await queries.claim_view(db, claim)
    return ClaimResponse.from_view(view)


@router.get("/me/claims", response_model=MyClaimsResponse)
async def list_my_claims(
    actor: ActiveActor,
    db: DbSession,
    queries: QueryServiceDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> MyClaimsResponse:
    """The actor's own claim history, newest first; assignment
    platform/keyword are visible here because every row is the owner's."""
    views, total = await queries.list_own_claims(
        db, user_id=actor.user_id, limit=limit, offset=offset
    )
    return MyClaimsResponse(
        items=[MyClaimResponse.from_view(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/claims/{claim_id}/abandon", response_model=ClaimResponse)
async def abandon_claim(
    claim_id: uuid.UUID,
    actor: ActiveActor,
    db: DbSession,
    abandons: AbandonServiceDep,
    queries: QueryServiceDep,
    limiter: LimiterDep,
) -> ClaimResponse:
    """Abandon the actor's own claim and release its assignment (spec
    §8.5); replaying a successful abandon returns the same terminal row."""
    await _enforce_rate_limit(limiter, "claims:abandon", str(actor.user_id))
    claim = await abandons.abandon_claim(db, actor.user_id, claim_id)
    view = await queries.claim_view(db, claim)
    return ClaimResponse.from_view(view)


# --- teacher surfaces (spec §28, §41) -----------------------------------------------


@router.get("/teacher/tasks", response_model=TeacherTaskListResponse)
async def list_teacher_tasks(
    actor: StaffActor,
    db: DbSession,
    queries: QueryServiceDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> TeacherTaskListResponse:
    """The workbench Task list (spec §41): the actor's own tasks plus the
    ones they collaborate on, every status including DRAFT, offset-
    paginated. Card-level facts only — contract fields ride the detail."""
    items, total = await queries.list_teacher_tasks(
        db, actor, limit=limit, offset=offset
    )
    return TeacherTaskListResponse(
        items=[TeacherTaskListItemResponse.from_view(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/teacher/tasks/{task_id}", response_model=TeacherTaskResponse)
async def get_teacher_task(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    queries: QueryServiceDep,
) -> TeacherTaskResponse:
    """The full workbench detail of one task (spec §41): everything the
    owner configured, contract fields included, DRAFT readable (unlike
    the student surface). Owner, any collaborator, or Admin."""
    task = await queries.get_teacher_task(db, actor, task_id)
    return TeacherTaskResponse.from_domain(task)


@router.post("/teacher/tasks", response_model=TeacherTaskResponse, status_code=201)
async def create_task(
    body: TaskCreateRequest,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TeacherTaskResponse:
    """Create a DRAFT task owned by the actor (spec §6)."""
    task = await tasks.create_task(
        db,
        actor,
        CreateTask(
            title=body.title,
            description=body.description,
            base_reward_points=body.base_reward_points,
            deadline_mode=body.deadline_mode,
            allowed_file_types=body.allowed_file_types,
            max_file_size_bytes=body.max_file_size_bytes,
            task_type=body.task_type,
            rarity=body.rarity,
            fixed_deadline_at=body.fixed_deadline_at,
            duration_minutes=body.duration_minutes,
            claim_cutoff_minutes=body.claim_cutoff_minutes,
            submission_schema=body.submission_schema,
            submission_schema_version=body.submission_schema_version,
            notify_24h=body.notify_24h,
            notify_4h=body.notify_4h,
            notification_channels=body.notification_channels,
        ),
    )
    # created_at / retention_policy are server defaults the INSERT did not
    # return; load them before serializing (async attribute access on an
    # unloaded column would otherwise try to lazy-load and fail).
    await db.refresh(task)
    return TeacherTaskResponse.from_domain(task)


@router.patch("/teacher/tasks/{task_id}", response_model=TeacherTaskResponse)
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdateRequest,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TeacherTaskResponse:
    """Partial edit under the V1 edit rule (spec §6.2); ownership and the
    field-split are service concerns."""
    task = await tasks.update_task(db, actor, task_id, UpdateTask(**body.model_dump()))
    return TeacherTaskResponse.from_domain(task)


def _transition_response(service: TaskService, task: Task) -> TaskTransitionResponse:
    """Normalize a lifecycle verb's outcome: the landing
    status plus the claim verdict the claim side itself enforces."""
    return TaskTransitionResponse(
        task_id=task.id,
        status=task.status,
        claimable=service.is_claimable(task),
        published_at=task.published_at,
        closed_at=task.closed_at,
    )


@router.post("/teacher/tasks/{task_id}/publish", response_model=TaskTransitionResponse)
async def publish_task(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TaskTransitionResponse:
    result = await tasks.publish_task(db, actor, task_id)
    return TaskTransitionResponse(
        task_id=result.task_id,
        status=result.status.value,
        claimable=result.claimable,
        published_at=result.published_at,
        closed_at=None,
    )


@router.post("/teacher/tasks/{task_id}/pause", response_model=TaskTransitionResponse)
async def pause_task(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TaskTransitionResponse:
    task = await tasks.pause_task(db, actor, task_id)
    return _transition_response(tasks, task)


@router.post("/teacher/tasks/{task_id}/resume", response_model=TaskTransitionResponse)
async def resume_task(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TaskTransitionResponse:
    task = await tasks.resume_task(db, actor, task_id)
    return _transition_response(tasks, task)


@router.post("/teacher/tasks/{task_id}/close", response_model=TaskTransitionResponse)
async def close_task(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TaskTransitionResponse:
    task = await tasks.close_task(db, actor, task_id)
    return _transition_response(tasks, task)


@router.post("/teacher/tasks/{task_id}/archive", response_model=TaskTransitionResponse)
async def archive_task(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    tasks: TaskServiceDep,
) -> TaskTransitionResponse:
    task = await tasks.archive_task(db, actor, task_id)
    return _transition_response(tasks, task)


@router.post(
    "/teacher/tasks/{task_id}/assignments/import/preview",
    response_model=ImportPreviewResponse,
)
async def preview_assignment_import(
    task_id: uuid.UUID,
    request: Request,
    actor: StaffActor,
    db: DbSession,
    imports: ImportServiceDep,
) -> ImportPreviewResponse:
    """Parse and pre-check a CSV upload (spec §7.1 steps 1-4).

    The file is the raw request body (``text/csv``); the importer's byte
    cap rejects oversize payloads before any parsing work.
    """
    data = await request.body()
    preview = await imports.preview_assignments(db, actor, task_id, data)
    return ImportPreviewResponse.from_domain(preview)


@router.post(
    "/teacher/tasks/{task_id}/assignments/import/confirm",
    response_model=ImportConfirmResponse,
)
async def confirm_assignment_import(
    task_id: uuid.UUID,
    body: ImportConfirmRequest,
    actor: StaffActor,
    db: DbSession,
    imports: ImportServiceDep,
) -> ImportConfirmResponse:
    """Insert exactly the previewed rows in one transaction (spec §7.1
    steps 5-6), consuming the single-use preview token."""
    result = await imports.confirm_assignments(db, actor, task_id, body.preview_token)
    return ImportConfirmResponse(task_id=result.task_id, inserted=result.inserted)


@router.put(
    "/teacher/tasks/{task_id}/collaborators/{teacher_id}",
    response_model=CollaboratorResponse,
)
async def add_collaborator(
    task_id: uuid.UUID,
    teacher_id: uuid.UUID,
    body: CollaboratorAddRequest,
    actor: StaffActor,
    db: DbSession,
    collaborators: CollaboratorServiceDep,
) -> CollaboratorResponse:
    """Grant a capability set on the task to a Teacher (spec §4.2); the
    standing / grant-within-own-set / target-role rules are the service's."""
    row = await collaborators.add_collaborator(
        db, actor, task_id, teacher_id, body.permissions
    )
    return CollaboratorResponse(
        task_id=row.task_id, teacher_id=row.teacher_id, permissions=row.permissions
    )


@router.delete("/teacher/tasks/{task_id}/collaborators/{teacher_id}", status_code=204)
async def remove_collaborator(
    task_id: uuid.UUID,
    teacher_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    collaborators: CollaboratorServiceDep,
) -> None:
    await collaborators.remove_collaborator(db, actor, task_id, teacher_id)


@router.get(
    "/teacher/tasks/{task_id}/statistics", response_model=TaskStatisticsResponse
)
async def get_task_statistics(
    task_id: uuid.UUID,
    actor: StaffActor,
    db: DbSession,
    queries: QueryServiceDep,
    ratings: RatingPortDep,
) -> TaskStatisticsResponse:
    """The workbench aggregate (spec §41): counts only, by construction."""
    statistics = await queries.get_task_statistics(db, actor, task_id, ratings)
    return TaskStatisticsResponse.from_domain(statistics)
