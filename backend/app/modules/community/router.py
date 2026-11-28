# backend/app/modules/community/router.py
"""Community HTTP API: thin routes and module composition root.

Covers comments, votes, reactions, reports, ratings, and moderation surfaces
(spec §20-§24, §28-§29, §33.1, §40). Domain invariants live in the community
services; Pydantic wire models live in ``wire_schemas.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from app.core import rbac
from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError, error_envelope
from app.core.observability import REQUEST_ID_HEADER
from app.db.session import get_db_session
from app.integrations.rate_limit import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.community.comment_service import (
    CommentModerationDeniedError,
    CommentService,
)
from app.modules.community.gates import require_task_moderation_site
from app.modules.community.models import (
    Comment,
    CommentReaction,
    CommentReport,
    CommentRevision,
    CommentVote,
)
from app.modules.community.moderation_service import ModerationService
from app.modules.community.rating_service import (
    RatingService,
    TaskCompletionRequiredError,
)
from app.modules.community.reaction_service import ReactionService
from app.modules.community.report_service import ReportService
from app.modules.community.schemas import (
    CommentPublic,
    CreateComment,
)
from app.modules.community.serializers import serialize_moderation_comment
from app.modules.community.settings_emoji_whitelist import SystemEmojiWhitelistProvider
from app.modules.community.vote_service import VoteService
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_community_actor,
    require_approved_community_actor,
    require_staff_management_actor,
)
from app.modules.identity.dependencies import (
    require_approved_student_actor as require_approved_student_for_reports,
)
from app.modules.identity.events import (
    Actor,
    DomainEventPublisher,
    LoggingEventPublisher,
)
from app.modules.system.service import EMOJI_WHITELIST, SystemSettingService

__all__ = [
    "compute_hot_score",
    "register_community_exception_handlers",
    "router",
]

# --- pagination bounds (the documented offset choice) ----------------------------

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

# The V1 hot-ordering page cap: ``sort=hot`` orders the whole rendered
# thread, so the service read uses one bounded page instead of an unbounded
# slice (a thread larger than this caps out like any other page — revisit
# with the profiling the comment_service fixpoint note promises).
_FULL_THREAD_FETCH_LIMIT = 10_000

# --- hot score (spec §24: replaceable implementation detail) ----------------------

# Engagement halves every 72 comment-hours: fresh discussion outranks a
# day-old tie, and a week of age cannot bury a genuinely hot thread's lead.
_HOT_HALF_LIFE_HOURS = 72.0


def compute_hot_score(
    *,
    created_at: datetime,
    likes: int,
    dislikes: int,
    reactions: int,
    now: datetime,
    half_life_hours: float = _HOT_HALF_LIFE_HOURS,
) -> float:
    """One comment's hot score (spec §24): engagement under exponential
    age decay, where engagement is ``likes - dislikes + reactions``.

    Pure and replaceable by design: the spec pins only the INPUTS (likes,
    dislikes, reactions, age — 不得将客户端传入的 hot_score 当事实值), so
    swapping this function swaps the whole ordering without touching any
    other surface. Zero engagement scores 0.0; net-negative engagement
    scores below it. Never store or echo the value — it orders a read,
    that is all.
    """
    age_hours = max((now - created_at).total_seconds() / 3600.0, 0.0)
    engagement = likes - dislikes + reactions
    # float(): typeshed types float ** float as Any; the value is a float.
    return float(engagement * 0.5 ** (age_hours / half_life_hours))


# --- typed-exception -> envelope mapping ------------------------------------------


def register_community_exception_handlers(app: FastAPI) -> None:
    """Attach this module's two non-default envelope handlers (the
    tasks/identity registration precedent).

    Every community service exception subclasses ``BusinessError`` and
    renders through the core handler with its frozen code/status. Two
    transport-level mappings remain: the endpoint limiter's plain
    ``RateLimitExceededError`` (429 ``RATE_LIMITED``) and the §29-specific
    ``RATING_NOT_ELIGIBLE`` for rating without a COMPLETED claim (the
    service's own generic ``PERMISSION_DENIED`` stays for direct service
    callers; one exception type, one transport code — the identity
    module's mapping discipline).
    """

    def _render(
        status_code: int,
        code: ErrorCode,
    ) -> Callable[[StarletteRequest, Exception], Coroutine[Any, Any, JSONResponse]]:
        async def handler(request: StarletteRequest, exc: Exception) -> JSONResponse:
            request_id = getattr(request.state, "request_id", None)
            headers = {REQUEST_ID_HEADER: request_id} if request_id else None
            return JSONResponse(
                status_code=status_code,
                content=error_envelope(code, str(exc), None, request_id),
                headers=headers,
            )

        return handler

    app.add_exception_handler(
        RateLimitExceededError, _render(429, ErrorCode.RATE_LIMITED)
    )
    app.add_exception_handler(
        TaskCompletionRequiredError, _render(403, ErrorCode.RATING_NOT_ELIGIBLE)
    )


# --- provider dependencies (module composition root) ------------------------------


@lru_cache
def get_community_redis() -> aioredis.Redis:
    """Process-wide Redis client for the community rate limiter (the
    tasks-router provider pattern; tests override this dependency to point
    at the flushed test database — a direct call inside a provider would
    dodge ``dependency_overrides``, which is the seam tests use)."""
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


RedisDep = Annotated[aioredis.Redis, Depends(get_community_redis)]
ClockDep = Annotated[Clock, Depends(get_business_clock)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_rate_limiter(clock: ClockDep, redis: RedisDep) -> RateLimiter:
    return RedisFixedWindowLimiter(redis=redis, clock=clock)


def get_event_publisher() -> DomainEventPublisher:
    """Interim adapter; the audit/outbox module wires persistent dispatch
    (the tasks-router seam, shared shape)."""
    return LoggingEventPublisher()


EventPublisherDep = Annotated[DomainEventPublisher, Depends(get_event_publisher)]


def get_comment_service(
    clock: ClockDep, settings: AppSettings, events: EventPublisherDep
) -> CommentService:
    return CommentService(
        comment_max_length=settings.comment_max_length,
        clock=clock,
        events=events,
        # audit: the durable audit_logs writer (G12; PR #2 hardening
        # pass 4a) — the get_report_service wiring pattern.
        audit=AuditLogWriter(),
    )


def get_vote_service() -> VoteService:
    return VoteService()


async def get_reaction_service(
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ReactionService:
    """The production whitelist binding (PR #5 final review fix B): the
    audited ``system_settings`` EMOJI_WHITELIST row is read ONCE per
    request on the request's own session, and the provider applies the
    row-over-seed priority and the wiring-time validation — the rule
    lives in the provider family, this composition only fetches the
    value (the ``SystemAcademicTermProvider`` pattern in points/router).

    G7: the row is the FACT, ``DefaultEmojiWhitelistProvider``'s spec
    §22 eight the INITIAL SEED. The storage read goes through the
    system module's service (the arrow points IN, the audit-module
    discipline: this composition root is the layer allowed to see both
    modules)."""
    configured = await SystemSettingService().get(db, EMOJI_WHITELIST)
    return ReactionService(
        whitelist=SystemEmojiWhitelistProvider(configured_value=configured)
    )


def get_report_service() -> ReportService:
    # moderation_key_secret defaults to Settings.token_secret inside the
    # service (the serializers' keyed-material contract); the audit
    # writer is wired explicitly so the composition root shows the
    # closures' side effects (the get_moderation_service pattern) —
    # stateless, flush-only, same transaction as the closure.
    return ReportService(audit=AuditLogWriter())


def get_rating_service() -> RatingService:
    return RatingService()


def get_moderation_service(
    clock: ClockDep, events: EventPublisherDep
) -> ModerationService:
    # audit: the durable audit_logs writer (G12, PR #2 hardening P0-5),
    # wired explicitly so the composition root shows the reveal's side
    # effects; stateless and flush-only.
    return ModerationService(clock=clock, events=events, audit=AuditLogWriter())


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CommunityActor = Annotated[Actor, Depends(require_approved_community_actor)]
StudentActor = Annotated[Actor, Depends(require_approved_student_for_reports)]
StaffActor = Annotated[Actor, Depends(require_staff_management_actor)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
CommentServiceDep = Annotated[CommentService, Depends(get_comment_service)]
VoteServiceDep = Annotated[VoteService, Depends(get_vote_service)]
ReactionServiceDep = Annotated[ReactionService, Depends(get_reaction_service)]
ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]
RatingServiceDep = Annotated[RatingService, Depends(get_rating_service)]
ModerationServiceDep = Annotated[ModerationService, Depends(get_moderation_service)]

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]
CommentSort = Annotated[Literal["latest", "hot"], Query()]


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


async def require_admin_management_actor(actor: StaffActor) -> Actor:
    """The Admin composition of the management gate (spec §4.3/§33.4):
    everything ``require_staff_management_actor`` demands (staff role,
    ACTIVE, confirmed TOTP) plus ADMIN — the hard-hide and reveal surfaces
    are Admin-only by the task 3/8 rulings, and the services re-check the
    role so a wiring slip cannot widen them."""
    if not rbac.is_admin(actor.role):
        raise BusinessError(
            ErrorCode.PERMISSION_DENIED,
            "只有管理员可以执行该操作",
            status_code=403,
        )
    return actor


AdminActor = Annotated[Actor, Depends(require_admin_management_actor)]


# --- wire schemas (defined in wire_schemas.py, re-exported for callers) -----------

from app.modules.community.wire_schemas import (  # noqa: E402
    CommentCreateRequest,
    CommentEditRequest,
    CommentListResponse,
    CommentPublicResponse,
    CommentReportListResponse,
    CommentReportResponse,
    ModerationCommentListResponse,
    ModerationCommentResponse,
    ModerationDeleteRequest,
    RatingRequest,
    RatingResponse,
    ReactionRequest,
    ReactionResponse,
    ReportClosureResponse,
    ReportDismissRequest,
    ReportHandleRequest,
    ReportRequest,
    ReportResponse,
    RevealIdentityResponse,
    RevealRequest,
    VoteRequest,
    VoteResponse,
)

# --- community participant surfaces (spec §21-§23, §28) ------------------------------

router = APIRouter()


@router.get("/tasks/{task_id}/comments", response_model=CommentListResponse)
async def list_task_comments(
    task_id: uuid.UUID,
    actor: Annotated[Actor, Depends(require_active_community_actor)],
    db: DbSession,
    comments: CommentServiceDep,
    clock: ClockDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
    sort: CommentSort = "latest",
) -> CommentListResponse:
    """One offset page of a published task's public comments (spec §21),
    ``sort=latest`` (created_at desc) or ``sort=hot`` (server-computed
    ordering per spec §24 — never a client value)."""
    if sort == "hot":
        page, total = await comments.list_comments(
            db, task_id, limit=_FULL_THREAD_FETCH_LIMIT, offset=0
        )
        engagement = await _engagement_totals(db, [comment.id for comment in page])
        rendered = _hot_ordered(page, engagement=engagement, now=clock.now())[
            offset : offset + limit
        ]
    else:
        rendered, total = await comments.list_comments(
            db, task_id, limit=limit, offset=offset
        )
    return CommentListResponse(
        items=[CommentPublicResponse.from_dto(comment) for comment in rendered],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/tasks/{task_id}/comments",
    response_model=CommentPublicResponse,
    status_code=201,
)
async def create_task_comment(
    task_id: uuid.UUID,
    body: CommentCreateRequest,
    actor: CommunityActor,
    db: DbSession,
    comments: CommentServiceDep,
    limiter: LimiterDep,
) -> CommentPublicResponse:
    """Publish one comment (or reply, when ``parent_id`` is set)
    immediately (spec §21.1): no pre-moderation — the rate limit is the
    paired abuse control. Anonymity is a display attribute of this one
    comment only (spec §21.4)."""
    await _enforce_rate_limit(limiter, "comments:create", str(actor.user_id))
    public = await comments.create_comment(
        db,
        actor,
        CreateComment(
            task_id=task_id,
            content=body.content,
            parent_id=body.parent_id,
            is_anonymous=body.is_anonymous,
        ),
    )
    return CommentPublicResponse.from_dto(public)


@router.patch("/comments/{comment_id}", response_model=CommentPublicResponse)
async def edit_comment(
    comment_id: uuid.UUID,
    body: CommentEditRequest,
    actor: CommunityActor,
    db: DbSession,
    comments: CommentServiceDep,
    limiter: LimiterDep,
) -> CommentPublicResponse:
    """Owner edit (spec §21.3): the previous version is appended to the
    revision history and the response carries ``edited=True``. Concurrent
    edits are last-write-wins with every superseded version retained (the
    module docstring ruling) — V1 deliberately serves no 409 here."""
    await _enforce_rate_limit(limiter, "comments:edit", str(actor.user_id))
    public = await comments.edit_comment(db, actor, comment_id, body.content)
    return CommentPublicResponse.from_dto(public)


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_own_comment(
    comment_id: uuid.UUID,
    actor: CommunityActor,
    db: DbSession,
    comments: CommentServiceDep,
) -> None:
    """Owner soft delete (spec §21.3): the trio lands on the row and the
    public list decides tombstone vs vanish. No rate bucket — the
    deleted-state guard makes it single-shot."""
    await comments.delete_own_comment(db, actor, comment_id)


@router.post("/comments/{comment_id}/vote", response_model=VoteResponse)
async def cast_comment_vote(
    comment_id: uuid.UUID,
    body: VoteRequest,
    actor: CommunityActor,
    db: DbSession,
    votes: VoteServiceDep,
    limiter: LimiterDep,
) -> VoteResponse:
    """Set the caller's stance on one comment (spec §22): ``value`` 1
    like, -1 dislike, 0 removes the vote; transitions are atomic in the
    service."""
    await _enforce_rate_limit(limiter, "comments:vote", str(actor.user_id))
    result = await votes.set_vote(db, actor.user_id, comment_id, body.value)
    return VoteResponse(
        current_value=result.current_value,
        likes=result.likes,
        dislikes=result.dislikes,
    )


@router.post("/comments/{comment_id}/reactions", response_model=ReactionResponse)
async def toggle_comment_reaction(
    comment_id: uuid.UUID,
    body: ReactionRequest,
    actor: CommunityActor,
    db: DbSession,
    reactions: ReactionServiceDep,
    limiter: LimiterDep,
) -> ReactionResponse:
    """Toggle one whitelisted emoji reaction (spec §22): ``added`` True
    when the reaction landed, False when it was removed; the same emoji
    again is the toggle. The per-emoji counts echo is the read the
    service delegates to this surface."""
    await _enforce_rate_limit(limiter, "comments:reaction", str(actor.user_id))
    added = await reactions.toggle_reaction(db, actor.user_id, comment_id, body.emoji)
    counts = await _reaction_counts(db, comment_id)
    return ReactionResponse(emoji=body.emoji, added=added, counts=counts)


@router.post(
    "/comments/{comment_id}/reports", response_model=ReportResponse, status_code=201
)
async def file_comment_report(
    comment_id: uuid.UUID,
    body: ReportRequest,
    actor: CommunityActor,
    db: DbSession,
    reports: ReportServiceDep,
    limiter: LimiterDep,
) -> ReportResponse:
    """File one report into the moderation queue (spec §23): the comment
    is never removed by the filing, and a duplicate (comment, reporter,
    category) is the idempotent return of the original report."""
    await _enforce_rate_limit(limiter, "comments:report", str(actor.user_id))
    report = await reports.report_comment(
        db, actor.user_id, comment_id, body.category, body.note
    )
    return ReportResponse(
        id=report.id,
        comment_id=report.comment_id,
        category=report.category,
        note=report.note,
        status=report.status,
        created_at=report.created_at,
    )


@router.put("/tasks/{task_id}/rating", response_model=RatingResponse)
async def put_task_rating(
    task_id: uuid.UUID,
    body: RatingRequest,
    actor: StudentActor,
    db: DbSession,
    ratings: RatingServiceDep,
) -> RatingResponse:
    """Rate (or re-rate) one task (spec §20): one row per (task, user)
    holding the latest value; only a completer of at least one Claim may
    rate — that denial renders the §29 ``RATING_NOT_ELIGIBLE`` envelope
    (the handler this module registers)."""
    row = await ratings.rate_task(db, actor.user_id, task_id, body.rating)
    return RatingResponse(
        task_id=row.task_id,
        rating=row.rating,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- staff surfaces (spec §21.4, §23, §28) ------------------------------------------


@router.get(
    "/teacher/tasks/{task_id}/comments/moderation",
    response_model=ModerationCommentListResponse,
)
async def list_moderation_comments(
    task_id: uuid.UUID,
    request: Request,
    actor: StaffActor,
    db: DbSession,
    settings: AppSettings,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> ModerationCommentListResponse:
    """The Teacher-safe comment listing for one task (spec §21.4):
    anonymous authors render 匿名用户 plus the pseudonymous moderation
    key, and soft deletes / hard hides stay visible as history with
    their flags. Standing mirrors the report queue (owner /
    MODERATE_COMMUNITY / Admin) — the destructive powers remain
    service-gated."""
    await _require_comment_moderation_viewer(db, actor, task_id)

    total = int(
        await db.scalar(
            select(func.count()).select_from(Comment).where(Comment.task_id == task_id)
        )
        or 0
    )
    items: list[ModerationCommentResponse] = []
    if total > 0 and offset < total:
        edited_flag = (
            select(CommentRevision.comment_id)
            .where(CommentRevision.comment_id == Comment.id)
            .exists()
        )
        rows = (
            await db.execute(
                select(Comment, _USERS.c.nickname, edited_flag)
                .select_from(Comment)
                .join(_USERS, _USERS.c.id == Comment.user_id)
                .where(Comment.task_id == task_id)
                .order_by(Comment.created_at.desc(), Comment.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        redact_hard_hidden = not rbac.is_admin(actor.role)
        items = [
            ModerationCommentResponse.from_dto(
                serialize_moderation_comment(
                    comment,
                    author_nickname=str(nickname),
                    key_secret=settings.token_secret,
                    edited=edited,
                    redact_hard_hidden=redact_hard_hidden,
                )
            )
            for comment, nickname, edited in rows
        ]
    audit_ctx = AuditContext.from_request(request)
    await AuditLogWriter().append(
        db,
        actor=actor,
        action="COMMUNITY_MODERATION_COMMENTS_READ",
        target_type="task",
        target_id=str(task_id),
        details={"limit": limit, "offset": offset, "returned": len(items)},
        ip_address=audit_ctx.ip_address,
        request_id=audit_ctx.request_id,
    )
    await db.commit()
    return ModerationCommentListResponse(
        items=items, total=total, limit=limit, offset=offset
    )


@router.get(
    "/teacher/tasks/{task_id}/reports", response_model=CommentReportListResponse
)
async def list_task_reports(
    task_id: uuid.UUID,
    request: Request,
    actor: StaffActor,
    db: DbSession,
    reports: ReportServiceDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> CommentReportListResponse:
    """The per-task report queue (spec §23; task 6's service): report
    facts, the reported comment in the Teacher-safe shape, and reporter
    identity for the moderators who act on it. Admin is admitted by the
    service's read ruling."""
    views, total = await reports.list_task_reports(
        db,
        actor,
        task_id,
        limit=limit,
        offset=offset,
        audit_context=AuditContext.from_request(request),
    )
    return CommentReportListResponse(
        items=[CommentReportResponse.from_dto(view) for view in views],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/tasks/{task_id}/reports/{report_id}/dismiss",
    response_model=ReportClosureResponse,
)
async def dismiss_task_report(
    task_id: uuid.UUID,
    report_id: uuid.UUID,
    body: ReportDismissRequest,
    actor: StaffActor,
    db: DbSession,
    reports: ReportServiceDep,
    request: Request,
) -> ReportClosureResponse:
    """Close one report as DISMISSED (PR #2 hardening step 10): the
    moderator judged no action warranted — the mandatory reason records
    why, one ``REPORT_DISMISSED`` audit row lands in the same
    transaction, and the comment is untouched (§23 不自动删除评论
    reaches closure). A same-state replay echoes the closed row; the
    other terminal state is the typed 409. Standing is the queue
    listing's (owner / MODERATE_COMMUNITY / Admin), so the queue's
    reader is its closer. No rate bucket — the moderation write
    surfaces (moderate-delete, hard hide, reveal) carry none: the
    state machine makes every replay single-shot."""
    report = await reports.dismiss_report(
        db,
        actor,
        task_id,
        report_id,
        body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return _closure_response(task_id, report)


@router.post(
    "/tasks/{task_id}/reports/{report_id}/handle",
    response_model=ReportClosureResponse,
)
async def handle_task_report(
    task_id: uuid.UUID,
    report_id: uuid.UUID,
    body: ReportHandleRequest,
    actor: StaffActor,
    db: DbSession,
    reports: ReportServiceDep,
    request: Request,
) -> ReportClosureResponse:
    """Close one report as HANDLED (PR #2 hardening step 10): registers
    that the moderator acted on the reported comment (through the
    existing audited removal paths or otherwise) — this endpoint books
    the conclusion and its ``REPORT_HANDLED`` audit row; the comment
    row is never touched here. Optional note, same-state replay
    idempotent, other terminal state the typed 409, standing as the
    dismiss surface above."""
    report = await reports.handle_report(
        db,
        actor,
        task_id,
        report_id,
        body.note,
        audit_context=AuditContext.from_request(request),
    )
    return _closure_response(task_id, report)


@router.delete("/teacher/comments/{comment_id}", status_code=204)
async def moderate_delete_comment(
    comment_id: uuid.UUID,
    body: ModerationDeleteRequest,
    actor: StaffActor,
    db: DbSession,
    comments: CommentServiceDep,
    request: Request,
) -> None:
    """Teacher moderation delete (spec §21.4): the service gates the
    actor to the task's owner or a MODERATE_COMMUNITY collaborator
    (Admin is refused here by the task 3 power-separation ruling),
    requires the reason, and publishes the audit event."""
    await comments.moderate_delete_comment(
        db,
        actor,
        comment_id,
        body.reason,
        audit_context=AuditContext.from_request(request),
    )


@router.post("/teacher/comments/{comment_id}/hard-hide", status_code=204)
async def hide_comment_subtree(
    comment_id: uuid.UUID,
    body: ModerationDeleteRequest,
    actor: AdminActor,
    db: DbSession,
    comments: CommentServiceDep,
    request: Request,
) -> None:
    """Admin-only hard hide of a whole subtree (spec §21.3 彻底隐藏):
    visibility removal for privacy/legal escalations — rows, content, and
    relations survive, the public list renders the subtree nothing, and
    the audit event carries the verbatim reason."""
    await comments.admin_hard_hide_subtree(
        db,
        actor,
        comment_id,
        body.reason,
        audit_context=AuditContext.from_request(request),
    )


@router.post(
    "/admin/comments/{comment_id}/reveal-identity",
    response_model=RevealIdentityResponse,
)
async def reveal_comment_identity(
    comment_id: uuid.UUID,
    body: RevealRequest,
    actor: AdminActor,
    db: DbSession,
    moderation: ModerationServiceDep,
    request: Request,
) -> RevealIdentityResponse:
    """The explicit, audited identity reveal (spec §21.4): Admin-only,
    reason mandatory and capped, and every call publishes the
    ``COMMENT_IDENTITY_REVEALED`` audit event. Ordinary moderation
    surfaces stay pseudonymous until this is called."""
    revealed = await moderation.request_identity_reveal(
        db,
        actor,
        comment_id,
        body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return RevealIdentityResponse(
        user_id=revealed.user_id,
        nickname=revealed.nickname,
        username=revealed.username,
    )


# --- internals ---------------------------------------------------------------------


def _closure_response(
    task_id: uuid.UUID, report: CommentReport
) -> ReportClosureResponse:
    """Render one closed report row (both closure routes share the
    shape). The asserts are the state machine's own contract: every
    return path of ``dismiss_report``/``handle_report`` carries the
    closure stamps — a fresh transition writes them, a same-state
    replay returns a row that already has them."""
    assert report.handled_by is not None and report.handled_at is not None
    return ReportClosureResponse(
        id=report.id,
        task_id=task_id,
        comment_id=report.comment_id,
        status=report.status,
        handled_by=report.handled_by,
        handled_at=report.handled_at,
    )


# Identity seam (the gates/service precedent): a typed Core-level light
# users table, NOT the identity ORM model — nickname for the moderation
# display join.
_USERS = table(
    "users",
    column("id", Uuid),
    column("nickname", String),
)


async def _engagement_totals(
    db: AsyncSession, comment_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int, int]]:
    """(likes, dislikes, reactions) per comment id over the given ids —
    the hot score's engagement inputs (spec §24), read in one grouped
    pass per table."""
    if not comment_ids:
        return {}
    vote_rows = await db.execute(
        select(CommentVote.comment_id, CommentVote.value, func.count())
        .where(CommentVote.comment_id.in_(comment_ids))
        .group_by(CommentVote.comment_id, CommentVote.value)
    )
    # dict.fromkeys shares one immutable (0, 0, 0) tuple — exactly the
    # zero-engagement default every id starts from.
    totals: dict[uuid.UUID, tuple[int, int, int]] = dict.fromkeys(
        comment_ids, (0, 0, 0)
    )
    for comment_id, value, count in vote_rows:
        likes, dislikes, reactions = totals[comment_id]
        if value == 1:
            totals[comment_id] = (likes + int(count), dislikes, reactions)
        else:
            totals[comment_id] = (likes, dislikes + int(count), reactions)
    reaction_rows = await db.execute(
        select(CommentReaction.comment_id, func.count())
        .where(CommentReaction.comment_id.in_(comment_ids))
        .group_by(CommentReaction.comment_id)
    )
    for comment_id, count in reaction_rows:
        likes, dislikes, reactions = totals[comment_id]
        totals[comment_id] = (likes, dislikes, reactions + int(count))
    return totals


def _hot_ordered(
    comments: Sequence[CommentPublic],
    *,
    engagement: dict[uuid.UUID, tuple[int, int, int]],
    now: datetime,
) -> list[CommentPublic]:
    """Order one rendered page's comments by the replaceable hot score:
    live comments by score desc (created_at desc, then id, as the stable
    tie-breaks), tombstones after every live comment (the module
    docstring ruling)."""

    def live_key(comment: CommentPublic) -> tuple[float, float, uuid.UUID]:
        likes, dislikes, reactions = engagement.get(comment.id, (0, 0, 0))
        score = compute_hot_score(
            created_at=comment.created_at,
            likes=likes,
            dislikes=dislikes,
            reactions=reactions,
            now=now,
        )
        return (-score, -comment.created_at.timestamp(), comment.id)

    tombstones = sorted(
        (comment for comment in comments if comment.deleted),
        key=lambda comment: (-comment.created_at.timestamp(), comment.id),
    )
    live = sorted(
        (comment for comment in comments if not comment.deleted), key=live_key
    )
    return live + tombstones


async def _reaction_counts(db: AsyncSession, comment_id: uuid.UUID) -> dict[str, int]:
    """The per-emoji reaction counts echo (the reaction service delegates
    this read to the surface)."""
    rows = await db.execute(
        select(CommentReaction.emoji, func.count())
        .where(CommentReaction.comment_id == comment_id)
        .group_by(CommentReaction.emoji)
    )
    return {emoji: int(count) for emoji, count in rows}


async def _require_comment_moderation_viewer(
    db: AsyncSession, actor: Actor, task_id: uuid.UUID
) -> None:
    """The moderation listing's standing (the report-queue ruling): the
    task's owner Teacher, a collaborator holding MODERATE_COMMUNITY, or
    Admin — a read that hides nothing (the read-surface policy:
    ``admit_admin=True`` in the shared gate). Existence answers first
    (unknown task -> the shared 404); the destructive moderation powers
    stay behind their own service checks. The predicate lives in
    ``gates.require_task_moderation_site`` since the final review (fix
    I1)."""
    await require_task_moderation_site(
        db,
        actor,
        task_id,
        admit_admin=True,
        error_factory=CommentModerationDeniedError,
    )
