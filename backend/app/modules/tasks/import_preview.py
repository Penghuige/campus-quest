# backend/app/modules/tasks/import_preview.py
"""Server-side import preview store (spec §7.1 steps 4-5).

Single responsibility: minting, storing, and atomically consuming the
single-use preview token whose payload is the exact canonicalized row
set confirm will insert. Redis is the only dependency; the orchestration
lives in ``importer``.

Design decisions:

- **Preview payload is stored server-side, immutable, single-use.**
  Preview mints a 256-bit ``secrets.token_urlsafe`` token and stores the
  canonicalized valid rows as one Redis string under
  ``assignment-import:preview:<token>``; confirm consumes it with a
  single ``GETDEL`` (the OTP verified-token pattern: exactly one
  concurrent caller receives the payload) and imports exactly the stored
  rows — never a re-upload. The Redis key holds the raw token rather
  than an HMAC of it (unlike OTP, spec §33.2 mandates no plaintext OTP
  material in Redis; replaying an import token only repeats an
  already-authorized insert of already-vetted rows, and the token is
  burned on first use anyway). Lifetime is a business TTL stamped inside
  the payload and checked against the injected Clock at confirm; the
  Redis TTL is the cleanup backstop (business TTL + grace, the OTP
  module's discipline).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import redis.asyncio as aioredis

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.tasks.import_parsing import AssignmentImportError, AssignmentPreviewRow

_PREVIEW_KEY_PREFIX = "assignment-import:preview:"
# Redis backstop TTL = business TTL + grace (see module docstring).
_REDIS_TTL_GRACE_SECONDS = 600

_TOKEN_INVALID_MESSAGE = "导入预览不存在、已失效或已被确认"


# --- typed exceptions (router-mapped) ---------------------------------------------


class InvalidPreviewTokenError(BusinessError):
    """Unknown, expired, already-consumed, or task-mismatched token.

    All four indistinguishable to the caller (NOT_FOUND/404): the remedy
    is the same — re-preview — and distinguishing them would leak preview
    lifecycle state.
    """

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _TOKEN_INVALID_MESSAGE,
            status_code=404,
        )


# --- DTOs (explicit, never serialized ORM objects) ---------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentImportPreview:
    """Preview outcome (spec §7.1 step 4: counts + per-row errors).

    ``preview_token`` is not None iff at least one row is importable and
    no file-level error rejected the file; it is the ONLY handle confirm
    accepts, and the payload behind it is exactly ``valid`` as shown.
    """

    task_id: UUID
    total_rows: int
    valid: tuple[AssignmentPreviewRow, ...]
    errors: tuple[AssignmentImportError, ...]
    preview_token: str | None
    expires_at: datetime | None

    @property
    def valid_count(self) -> int:
        return len(self.valid)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def _preview_key(token: str) -> str:
    return f"{_PREVIEW_KEY_PREFIX}{token}"


async def store_preview(
    redis: aioredis.Redis,
    *,
    task_id: UUID,
    rows: Sequence[AssignmentPreviewRow],
    expires_at: datetime,
    ttl_seconds: int,
) -> str:
    """Mint the single-use token and store its immutable row payload.

    The Redis TTL is the cleanup backstop (business TTL + grace); the
    authoritative expiry is the ``expires_at`` timestamp stamped inside
    the payload and checked at consume time against the Clock.
    """
    preview_token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "task_id": str(task_id),
            "rows": [
                {
                    "row_number": row.row_number,
                    "platform": row.platform,
                    "keyword": row.keyword,
                }
                for row in rows
            ],
            "expires_at": expires_at.timestamp(),
        },
        ensure_ascii=False,
    )
    await redis.set(
        _preview_key(preview_token),
        payload,
        ex=ttl_seconds + _REDIS_TTL_GRACE_SECONDS,
    )
    return preview_token


async def consume_preview(
    redis: aioredis.Redis,
    *,
    clock: Clock,
    task_id: UUID,
    preview_token: str,
) -> tuple[AssignmentPreviewRow, ...]:
    """Atomically consume the token and return its immutable rows.

    ``GETDEL`` is the single consume: exactly one concurrent caller
    receives the payload (the OTP verified-token discipline).
    Unknown, already-consumed, expired, task-mismatched, and
    malformed payloads are all the same typed NOT_FOUND.
    """
    raw = await redis.getdel(_preview_key(preview_token))
    if raw is None:
        raise InvalidPreviewTokenError
    try:
        loaded: object = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise InvalidPreviewTokenError from None
    if not isinstance(loaded, dict):
        raise InvalidPreviewTokenError
    raw_task_id = loaded.get("task_id")
    raw_rows = loaded.get("rows")
    raw_expires_at = loaded.get("expires_at")
    if not isinstance(raw_task_id, str) or not isinstance(raw_rows, list):
        raise InvalidPreviewTokenError
    try:
        bound_task = UUID(raw_task_id)
    except ValueError:
        raise InvalidPreviewTokenError from None
    if bound_task != task_id:
        raise InvalidPreviewTokenError
    if not isinstance(raw_expires_at, (int, float)):
        raise InvalidPreviewTokenError
    if float(raw_expires_at) <= clock.now().timestamp():
        raise InvalidPreviewTokenError
    rows: list[AssignmentPreviewRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            raise InvalidPreviewTokenError
        row_number = item.get("row_number")
        platform = item.get("platform")
        keyword = item.get("keyword")
        if (
            not isinstance(row_number, int)
            or not isinstance(platform, str)
            or not isinstance(keyword, str)
        ):
            raise InvalidPreviewTokenError
        rows.append(
            AssignmentPreviewRow(
                row_number=row_number, platform=platform, keyword=keyword
            )
        )
    if not rows:
        raise InvalidPreviewTokenError
    return tuple(rows)
