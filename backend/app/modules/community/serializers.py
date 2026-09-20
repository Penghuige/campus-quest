# backend/app/modules/community/serializers.py
"""Privacy-enforcing comment serializers (spec §21.1, §21.4, §40).

The single place author identity becomes (or refuses to become) a
display value. Every read path — the service returns, task 9's router
responses, any future projection — goes through these functions, so the
anonymity contract is enforced at one seam instead of per call site:

- public: anonymous renders exactly ``匿名用户``; named renders the
  nickname and nothing else. The DTO has no user_id field (schemas.py),
  so what the serializer does not put in cannot leak out.
- moderation (task 8 placeholder): NO author material at all — the
  pseudonymous ``moderation_key`` is a None seam until that task derives
  it server-side.

XSS posture (spec §21.1 防 XSS): ``content`` passes through verbatim as
plain text. The module never produces, escapes, or sanitizes HTML — the
stored bytes equal the submitted bytes after control-character
normalization, and the transport renders JSON; a client that treats a
JSON string value as markup is the client's defect to fix.
"""

from __future__ import annotations

from app.modules.community.models import Comment
from app.modules.community.schemas import CommentPublic, ModerationComment

__all__ = [
    "ANONYMOUS_AUTHOR_DISPLAY",
    "serialize_moderation_comment",
    "serialize_public_comment",
]

# spec §21.4: 普通用户只看到“匿名用户”.
ANONYMOUS_AUTHOR_DISPLAY = "匿名用户"


def serialize_public_comment(
    comment: Comment, *, author_nickname: str, edited: bool = False
) -> CommentPublic:
    """Build the public DTO explicitly from a Comment row.

    ``author_nickname`` is the ONLY join material the caller supplies;
    the row's ``user_id`` is deliberately unread here so the serializer
    itself cannot smuggle it into the shape. ``edited`` arrives from the
    caller (the listing query computes CommentRevision existence; a fresh
    create passes False).
    """
    return CommentPublic(
        id=comment.id,
        task_id=comment.task_id,
        parent_id=comment.parent_id,
        content=comment.content,
        is_anonymous=comment.is_anonymous,
        author_display=(
            ANONYMOUS_AUTHOR_DISPLAY if comment.is_anonymous else author_nickname
        ),
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        edited=edited,
        deleted=comment.deleted_at is not None,
    )


def serialize_moderation_comment(
    comment: Comment, *, edited: bool = False
) -> ModerationComment:
    """Build the moderation DTO — no author identity, key seam unfilled.

    Task 8 extends this signature with the derived ``moderation_key``;
    until then the base shape is stable so moderation surfaces can be
    built against it.
    """
    return ModerationComment(
        id=comment.id,
        task_id=comment.task_id,
        parent_id=comment.parent_id,
        content=comment.content,
        is_anonymous=comment.is_anonymous,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        edited=edited,
        deleted=comment.deleted_at is not None,
        moderation_key=None,
    )
