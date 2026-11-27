# backend/app/modules/community/wire_schemas.py
"""Pydantic wire schemas for the community HTTP API.

Each response model copies fields explicitly from the domain DTOs in
``schemas.py`` so author/reporter privacy boundaries remain enforced at the
transport layer. Write bodies set ``extra="forbid"``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.community.schemas import (
    CommentPublic,
    CommentReportView,
    ModerationComment,
)


class CommentPublicResponse(BaseModel):
    """The public comment wire shape: exactly ``CommentPublic``'s fields
    (spec §21.4/§40 — no user id, no contact, no username; the hot score
    never rides either)."""

    id: uuid.UUID
    task_id: uuid.UUID
    parent_id: uuid.UUID | None
    content: str | None
    is_anonymous: bool
    author_display: str
    created_at: datetime
    updated_at: datetime
    edited: bool
    deleted: bool

    @classmethod
    def from_dto(cls, comment: CommentPublic) -> CommentPublicResponse:
        return cls(
            id=comment.id,
            task_id=comment.task_id,
            parent_id=comment.parent_id,
            content=comment.content,
            is_anonymous=comment.is_anonymous,
            author_display=comment.author_display,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            edited=comment.edited,
            deleted=comment.deleted,
        )


class CommentListResponse(BaseModel):
    items: list[CommentPublicResponse]
    total: int
    limit: int
    offset: int


class CommentCreateRequest(BaseModel):
    """Publish body (spec §21.1/§21.2). ``extra="forbid"``: a client
    cannot smuggle any other field past the parser — there is no hot
    score, no author override, nothing but the three spec inputs."""

    model_config = ConfigDict(extra="forbid")

    content: str
    parent_id: uuid.UUID | None = None
    is_anonymous: bool = False


class CommentEditRequest(BaseModel):
    """Edit body (spec §21.3): content only — ``parent_id`` immutability
    is the create-only cycle guard, so it is unrepresentable here."""

    model_config = ConfigDict(extra="forbid")

    content: str


class VoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Literal[-1, 0, 1]


class VoteResponse(BaseModel):
    """The caller's post-transition stance plus the comment's totals
    (exactly ``VoteResult``): the surface echoes the toggle without a
    second read."""

    current_value: int
    likes: int
    dislikes: int


class ReactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emoji: str = Field(max_length=16)


class ReactionResponse(BaseModel):
    """The toggle verdict plus the comment's per-emoji reaction counts —
    the echoing read the reaction service's docstring delegates to this
    surface."""

    emoji: str
    added: bool
    counts: dict[str, int]


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    note: str | None = None


class ReportResponse(BaseModel):
    """The caller's OWN report row (the report service returns it to the
    reporter; one's own identity is not a leak, and no other reporter's
    identity exists in the shape)."""

    id: uuid.UUID
    comment_id: uuid.UUID
    category: str
    note: str | None
    status: str
    created_at: datetime


class RatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5)


class RatingResponse(BaseModel):
    """The rater's own rating echo (spec §20: the PUBLIC surface stays
    aggregate-only; your own value back to you is not a disclosure)."""

    task_id: uuid.UUID
    rating: int
    created_at: datetime
    updated_at: datetime


class ModerationCommentResponse(BaseModel):
    """The Teacher-safe moderation wire shape: exactly
    ``ModerationComment``'s fields — 匿名用户 display, the pseudonymous
    ``moderation_key`` exactly on anonymous records, the hard-hidden
    flag, and never a raw author id or contact fact."""

    id: uuid.UUID
    task_id: uuid.UUID
    parent_id: uuid.UUID | None
    content: str | None
    is_anonymous: bool
    author_display: str
    created_at: datetime
    updated_at: datetime
    edited: bool
    deleted: bool
    moderation_key: str | None = None
    hard_hidden: bool = False

    @classmethod
    def from_dto(cls, comment: ModerationComment) -> ModerationCommentResponse:
        return cls(
            id=comment.id,
            task_id=comment.task_id,
            parent_id=comment.parent_id,
            content=comment.content,
            is_anonymous=comment.is_anonymous,
            author_display=comment.author_display,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            edited=comment.edited,
            deleted=comment.deleted,
            moderation_key=comment.moderation_key,
            hard_hidden=comment.hard_hidden,
        )


class ModerationCommentListResponse(BaseModel):
    items: list[ModerationCommentResponse]
    total: int
    limit: int
    offset: int


class CommentReportResponse(BaseModel):
    """One report-queue row (exactly ``CommentReportView``): the reported
    comment in the Teacher-safe shape plus the REPORTER identity — the
    moderation surface spec §23 admits (被举报用户不可看到举报者身份 is
    the student-side wall; moderators act on reports and see who filed
    them). Never composed into a student-facing response."""

    id: uuid.UUID
    comment: ModerationCommentResponse
    category: str
    note: str | None
    status: str
    reporter_user_id: uuid.UUID | None
    reporter_nickname: str | None
    created_at: datetime
    handled_by: uuid.UUID | None
    handled_at: datetime | None

    @classmethod
    def from_dto(cls, view: CommentReportView) -> CommentReportResponse:
        return cls(
            id=view.id,
            comment=ModerationCommentResponse.from_dto(view.comment),
            category=view.category,
            note=view.note,
            status=view.status,
            reporter_user_id=view.reporter_user_id,
            reporter_nickname=view.reporter_nickname,
            created_at=view.created_at,
            handled_by=view.handled_by,
            handled_at=view.handled_at,
        )


class CommentReportListResponse(BaseModel):
    items: list[CommentReportResponse]
    total: int
    limit: int
    offset: int


class ReportDismissRequest(BaseModel):
    """Dismissal body (PR #2 hardening step 10): ``reason`` is a
    mandatory part of the governance decision — absent here is the 422
    parse refusal, blank-after-trim is the service's typed rejection,
    and ``extra="forbid"`` keeps a reason all a caller can send (the
    ModerationDeleteRequest shape)."""

    model_config = ConfigDict(extra="forbid")

    reason: str


class ReportHandleRequest(BaseModel):
    """Handling body: the optional governor note, normalized by the
    service with the filing-note rules (blank is no note)."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = None


class ReportClosureResponse(BaseModel):
    """The closed report row's facts back to the moderator: identity of
    the decision target, the terminal status, and the closure stamps
    (the actor is the caller themself, so ``handled_by`` is no
    disclosure). The queue listing beside it renders the same fields
    in the full row shape."""

    id: uuid.UUID
    task_id: uuid.UUID
    comment_id: uuid.UUID
    status: str
    handled_by: uuid.UUID
    handled_at: datetime


class ModerationDeleteRequest(BaseModel):
    """Reason-mandatory bodies (spec §21.3/§21.4): blank-after-trim is the
    service's typed rejection; the schema forbids extras so a reason is
    all a caller can send."""

    model_config = ConfigDict(extra="forbid")

    reason: str


class RevealRequest(BaseModel):
    """The reveal reason (spec §21.4 每次追溯): mandatory (blank-after-trim
    is the service's typed rejection) and capped at 1000 characters on the
    raw body — the audit payload is bounded material, not a moderation
    essay."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class RevealIdentityResponse(BaseModel):
    """Exactly ``RevealedIdentity``: the one wire shape where the student
    number (``username``) is representable at all (spec §21.4)."""

    user_id: uuid.UUID
    nickname: str
    username: str
