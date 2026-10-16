# backend/app/modules/identity/repository.py
"""Persistence queries for the identity module (backend-engineering §5-7).

Repositories never commit or roll back: the service boundary owns the
transaction lifetime. Queries only read here; the service performs the
writes so the whole invariant stays visible in one place.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User, UserSession


class UserRepository:
    """Read queries over ``users`` needed by registration, login, and the
    request-authentication dependencies."""

    async def find_by_phone_e164(
        self, session: AsyncSession, phone_e164: str
    ) -> User | None:
        """The account currently bound to ``phone_e164``, if any.

        This powers the friendly pre-registration check only; global phone
        uniqueness is ultimately enforced by the partial unique index
        ``uq_users_phone_e164`` (spec §5.4: 并发注册最终必须由数据库约束兜底).
        """
        # `AsyncSession.scalar` is typed Any; the annotation keeps the
        # declared contract under strict mypy instead of leaking Any.
        bound: User | None = await session.scalar(
            select(User).where(User.phone_e164 == phone_e164)
        )
        return bound

    async def find_by_username(
        self, session: AsyncSession, username: str
    ) -> User | None:
        """The account with exactly ``username``, if any (spec §5.2).

        The caller strips outer whitespace at the login boundary (spec
        §5.2); the match itself is exact — inner characters are never
        rewritten, and the lookup relies on the ``username`` UNIQUE index.
        """
        found: User | None = await session.scalar(
            select(User).where(User.username == username)
        )
        return found

    async def find_by_id(self, session: AsyncSession, user_id: UUID) -> User | None:
        """The account row for ``user_id``, if any.

        Read-only lookup for cross-module consumers (the UserDirectory
        port); identity rules never branch on existence alone here.
        """
        found: User | None = await session.scalar(
            select(User).where(User.id == user_id)
        )
        return found

    async def find_by_email_normalized(
        self, session: AsyncSession, email_normalized: str
    ) -> User | None:
        """The account currently holding ``email_normalized``, if any (§5.5).

        V1 uniqueness covers non-null normalized emails whether verified or
        not (see models.py), so this is the friendly pre-check for both the
        first bind and a re-bind; the partial unique index
        ``uq_users_email_normalized`` adjudicates every race.
        """
        found: User | None = await session.scalar(
            select(User).where(User.email_normalized == email_normalized)
        )
        return found

    async def find_with_live_session(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        session_id: UUID,
        now: datetime,
    ) -> User | None:
        """The ``user_id`` row iff ``session_id`` is its LIVE session.

        One JOIN answers both halves of request authentication (spec
        §5.6): joining on ``UserSession.user_id == User.id`` binds the
        access token's ``sub`` to its ``sid`` (a pair borrowed from
        another user resolves to nothing), and the liveness predicates —
        never revoked, never replaced, not yet expired — are evaluated
        server-side, so a session killed by ``revoke_all`` or refresh
        rotation takes its access tokens down immediately, before the
        JWT's own ``exp``. Expiry is compared against the injected
        business ``now`` (backend-engineering §11), never the database
        clock.
        """
        found: User | None = await session.scalar(
            select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                User.id == user_id,
                UserSession.id == session_id,
                UserSession.revoked_at.is_(None),
                UserSession.replaced_by.is_(None),
                UserSession.expires_at > now,
            )
        )
        return found
