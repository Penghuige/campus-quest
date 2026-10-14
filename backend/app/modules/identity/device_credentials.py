# backend/app/modules/identity/device_credentials.py
"""Remembered-device cookie credentials (V1.1 §2.2, Wave 1).

The 30-day "remember this device" option for ADMIN email-code login:
``issue`` mints one cookie value, stores only its SHA-256 digest, and
hands the plaintext back exactly once for the response's ``Set-Cookie``;
``validate`` resolves a presented cookie to its user (bumping
``last_used_at``) or None; ``revoke_all`` drops every device credential
of one user — called by session-kick and self-service management so a
stolen cookie cannot outlive the account's other credentials (Review
Focus 3).

Plain module functions, not a service class: every operation is one
stateless table round-trip with no wiring beyond the session the caller
already holds. Expiry is a caller-injected ``Clock`` comparison, never
a slept wall clock (backend-engineering §11).

Deliberately NOT an index on ``token_hash``: lookup volume is one row
per ADMIN login attempt, and the unique-ish digest scan stays trivial
at that shape; the index would only serve a brute-force workload the
cookie's 256 bits already make hopeless.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.identity.models import RememberedDevice

#: The browser-facing cookie name — the frontend contract (V1.1 §2.2).
DEVICE_COOKIE_NAME = "cq_staff_device"
DEVICE_TTL_DAYS = 30
_COOKIE_MAX_AGE_SECONDS = DEVICE_TTL_DAYS * 24 * 3600


def _hash_cookie(cookie_value: str) -> str:
    return sha256(cookie_value.encode()).hexdigest()


async def issue(db: AsyncSession, user_id: UUID, *, clock: Clock) -> str:
    """Mint one device credential; return the cookie VALUE once.

    Only the SHA-256 digest is persisted, so a database leak yields
    nothing replayable (see the model docstring).
    """
    cookie_value = secrets.token_urlsafe(32)
    db.add(
        RememberedDevice(
            user_id=user_id,
            token_hash=_hash_cookie(cookie_value),
            expires_at=clock.now() + timedelta(days=DEVICE_TTL_DAYS),
        )
    )
    await db.flush()
    return cookie_value


async def validate(
    db: AsyncSession, cookie_value: str, *, clock: Clock
) -> UUID | None:
    """Resolve a presented cookie; None when unknown or expired.

    A success bumps ``last_used_at`` so the self-service device list can
    show real recency; the bump rides the caller's transaction.
    """
    row = await db.scalar(
        select(RememberedDevice).where(
            RememberedDevice.token_hash == _hash_cookie(cookie_value)
        )
    )
    if row is None or row.expires_at <= clock.now():
        return None
    row.last_used_at = clock.now()
    await db.flush()
    return row.user_id


async def revoke_all(db: AsyncSession, user_id: UUID) -> int:
    """Delete every device credential of ``user_id``; return the count."""
    result = await db.execute(
        delete(RememberedDevice).where(RememberedDevice.user_id == user_id)
    )
    await db.flush()
    # rowcount is the matched-row count (the cleanup-jobs cast precedent).
    return int(cast("CursorResult[Any]", result).rowcount)
