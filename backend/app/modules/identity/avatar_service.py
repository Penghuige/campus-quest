# backend/app/modules/identity/avatar_service.py
"""Account avatar: upload, delete, and the display read (QA defect #4).

The service half of the avatar capability (spec amendment D1-D5 in
docs/superpowers/specs/2026-09-30-avatar-profile-amendment-proposal.md,
owner-approved 2026-10-03). Nickname/phone/email/password changes
already existed in ``ProfileService``; this module owns the ONE piece
the account model lacked — the stored avatar image.

Design decisions pinned by the amendment:

- **Validation is server-side and magic-byte based** (D2): the
  client-declared Content-Type and filename are transport metadata
  only; a mislabeled SVG or script payload fails regardless of what it
  claims to be. No imaging dependency — the three accepted signatures
  are identifiable from their header bytes alone.
- **The object key is storage plumbing, never product data** (D1):
  it appears in no DTO, no log line, and no audit payload. Display
  goes through the ``/users/{id}/avatar`` byte proxy (D3), which never
  redirects to a presigned URL.
- **Replacement stores the new object BEFORE the row flips and deletes
  the old object only AFTER the row's commit**: a storage failure
  leaves the previous avatar intact (no half-replaced state), and a
  delete failure degrades to an orphaned object — harmless — instead
  of a dangling pointer. The row is the single authority.
- **Every committed change appends a ``USER_AVATAR_CHANGED`` audit
  row** in the same transaction (G12), recording the change event and
  size/type facts but never the key.

The storage port is synchronous (boto3 under the S3 adapter), so every
call crosses a thread boundary via ``asyncio.to_thread`` — the
``upload_service.finalize_upload`` precedent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.errors import ProviderError
from app.integrations.object_storage import (
    AVATAR_CONTENT_TYPES,
    ObjectStorage,
    StoredObject,
)
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.events import Actor
from app.modules.identity.models import User

#: D2 cap: 2 MiB. The whole payload is validated in memory (bounded by
#: this constant), which is also why ``read_object`` may buffer whole.
AVATAR_MAX_BYTES = 2 * 1024 * 1024

#: The audit-stream action for every committed avatar change (uploads,
#: replacements, deletions — actual state transitions only, the
#: ``account_admin_service`` naming convention).
AUDIT_USER_AVATAR_CHANGED = "USER_AVATAR_CHANGED"

_AVATAR_TOO_LARGE_MESSAGE = "头像文件不能超过 2 MB"
_AVATAR_BAD_FORMAT_MESSAGE = "头像格式仅支持 PNG、JPEG 或 WebP"
_AVATAR_NOT_FOUND_MESSAGE = "该账号未设置头像"

#: The shortest header any accepted signature needs (WebP: RIFF at 0,
#: WEBP at 8). Shorter payloads cannot be a valid avatar of any of the
#: three formats, so they are rejected before signature inspection.
_MIN_HEADER_BYTES = 12

_logger = logging.getLogger(__name__)


def detect_avatar_type(data: bytes) -> str | None:
    """Classify ``data`` as png/jpeg/webp by its magic bytes, else None.

    Pure function (unit-tested in ``test_avatar_magic``); the service
    layer is the only caller and combines it with the size cap.
    """
    if len(data) < _MIN_HEADER_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def avatar_etag(object_key: str) -> str:
    """The display ETag (D3): a stable hash of the stored object key.

    Every upload mints a fresh key, so the ETag changes exactly when
    the avatar bytes change — a correct conditional-GET validator
    without reading the object.
    """
    return f'"{hashlib.sha256(object_key.encode()).hexdigest()[:32]}"'


async def read_avatar_body(stream: AsyncIterable[bytes]) -> bytes:
    """Read the raw upload body, bounded by the cap DURING the read.

    The avatar upload carries the image as the request body itself (no
    multipart envelope — see the profile route): a Starlette
    ``UploadFile`` would spool an unbounded body to disk before any
    application check runs, while this reader rejects the moment the
    running total crosses ``AVATAR_MAX_BYTES``.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > AVATAR_MAX_BYTES:
            raise BusinessError(
                ErrorCode.FILE_TOO_LARGE,
                _AVATAR_TOO_LARGE_MESSAGE,
                status_code=400,
                details={"limit": AVATAR_MAX_BYTES},
            )
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class AvatarImage:
    """One avatar ready for transport: bytes + validator + caching hint."""

    etag: str
    content_type: str
    content: bytes


class AvatarService:
    """Own-account avatar changes plus the login-gated display read."""

    def __init__(
        self, *, storage: ObjectStorage, audit: AuditLogWriter | None = None
    ) -> None:
        self._storage = storage
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    async def upload_avatar(
        self, db: AsyncSession, actor: Actor, content: bytes
    ) -> User:
        """Validate, store, and flip the account's avatar pointer.

        The row lock (``FOR UPDATE``) serializes concurrent uploads by
        the same account (two tabs racing): both stores succeed, but
        the row updates apply in lock order and each transaction
        deletes exactly the key it replaced — no double delete, no
        survivor pointing at a deleted object.
        """
        if len(content) > AVATAR_MAX_BYTES:
            raise BusinessError(
                ErrorCode.FILE_TOO_LARGE,
                _AVATAR_TOO_LARGE_MESSAGE,
                status_code=400,
                details={"limit": AVATAR_MAX_BYTES},
            )
        detected = detect_avatar_type(content)
        if detected is None:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _AVATAR_BAD_FORMAT_MESSAGE,
                status_code=400,
            )
        content_type = AVATAR_CONTENT_TYPES[detected]

        user = await self._locked_user(db, actor.user_id)
        previous_key = user.avatar_object_key

        # Store first: a storage failure leaves the current avatar
        # fully intact instead of half-replaced.
        new_key = await asyncio.to_thread(
            self._storage.store_avatar,
            user_id=user.id,
            content=content,
            content_type=content_type,
        )
        user.avatar_object_key = new_key
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_USER_AVATAR_CHANGED,
            target_type="USER",
            target_id=str(user.id),
            # Facts only — the object key never enters an audit row.
            details={"bytes": len(content), "content_type": content_type},
        )
        await db.commit()

        # Post-commit cleanup of the replaced object: a failure here is
        # an orphan, not a broken account (the row already points at
        # the new key). FileNotFoundError is the §27 idempotent shape.
        if previous_key is not None:
            await self._delete_object_quietly(previous_key)
        return user

    async def delete_avatar(self, db: AsyncSession, actor: Actor) -> User:
        """Clear the pointer and delete the stored object (D5)."""
        user = await self._locked_user(db, actor.user_id)
        key = user.avatar_object_key
        if key is None:
            raise BusinessError(
                ErrorCode.NOT_FOUND,
                _AVATAR_NOT_FOUND_MESSAGE,
                status_code=404,
            )
        user.avatar_object_key = None
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_USER_AVATAR_CHANGED,
            target_type="USER",
            target_id=str(user.id),
        )
        await db.commit()
        await self._delete_object_quietly(key)
        return user

    async def load_avatar(self, db: AsyncSession, user_id: UUID) -> AvatarImage | None:
        """The display read (D3): bytes + ETag for one account's avatar.

        ``None`` covers both "no such user" and "user has no avatar" —
        the caller renders the same 404 either way, and the difference
        never leaks (probing which UUIDs exist is not a surface this
        endpoint offers).
        """
        row = (
            await db.execute(select(User.avatar_object_key).where(User.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        stored: StoredObject = await asyncio.to_thread(
            self._storage.read_object, object_key=row
        )
        return AvatarImage(
            etag=avatar_etag(row),
            content_type=stored.content_type,
            content=stored.content,
        )

    async def _locked_user(self, db: AsyncSession, user_id: UUID) -> User:
        result = await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        return result.scalar_one()

    async def _delete_object_quietly(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(self._storage.delete_object, object_key=object_key)
        except FileNotFoundError:
            # Already gone; the row is the authority and already moved.
            pass
        except (OSError, ProviderError):
            # The port's documented failure taxonomy. ProviderError does
            # NOT derive from OSError (review finding) — without it, a
            # post-commit cleanup failure would escape as a 500 that
            # contradicts the committed row, and the client's retry
            # would then hit the upload rate limit for nothing. Here it
            # stays what the contract says: an orphaned object, a
            # committed success.
            _logger.warning(
                "avatar object cleanup failed (orphaned, row already "
                "committed); key omitted by privacy contract",
                exc_info=True,
            )
