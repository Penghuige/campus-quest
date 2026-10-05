# backend/app/modules/identity/users_router.py
"""Cross-user public account reads (spec amendment D3).

The one surface where one account reads another's presentational
material: the avatar byte proxy. Identity owns ``User`` and its
storage, so the route lives here even though the readers are community
surfaces (comment threads, leaderboards) — the module docstring in
``avatar_service`` pins why this is a PROXY, never a presigned
redirect: the object key must not enter the browser's network layer or
DOM (D1 privacy discipline).

Visibility: any authenticated actor (student/teacher/admin alike) may
read any account's avatar — avatars are public presentational
material once shown beside content, and the route resolves the actor
BEFORE the conditional-GET shortcut, so a 304 never bypasses
authentication. Anonymous requests are 401 regardless of caching.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.avatar_service import AvatarService
from app.modules.identity.dependencies import get_actor
from app.modules.identity.events import Actor
from app.modules.identity.providers import DbSession, get_avatar_service

router = APIRouter()

#: Private because the surface is login-gated; one day because the key
#: (hence the ETag) changes on every upload — no need to revalidate
#: more often than the avatar itself can change.
_CACHE_CONTROL = "private, max-age=86400"

_NO_AVATAR_MESSAGE = "该用户没有头像"


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 §13.1.2 semantics, liberal in what we accept: each
    comma-separated token, ``W/``-weak-prefix-stripped and
    quote-compared; ``*`` matches any current representation."""
    for token in if_none_match.split(","):
        candidate = token.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


@router.get("/users/{user_id}/avatar")
async def get_user_avatar(
    user_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_actor)],
    avatars: Annotated[AvatarService, Depends(get_avatar_service)],
    db: DbSession,
) -> Response:
    """One account's avatar bytes, login-gated (spec amendment D3).

    404 covers both "no such user" and "user has no avatar" — probing
    which UUIDs exist is not a surface this endpoint offers.
    """
    image = await avatars.load_avatar(db, user_id)
    if image is None:
        raise BusinessError(ErrorCode.NOT_FOUND, _NO_AVATAR_MESSAGE, status_code=404)
    headers = {"ETag": image.etag, "Cache-Control": _CACHE_CONTROL}
    if _etag_matches(request.headers.get("If-None-Match", ""), image.etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=image.content, media_type=image.content_type, headers=headers
    )
