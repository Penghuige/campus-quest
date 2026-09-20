# backend/app/modules/identity/directory.py
"""Frozen cross-module identity read port (docs/architecture/interfaces.md).

Modules outside identity (points, community, ranking, the admin surface)
read account facts ONLY through ``UserDirectory`` — never by importing
identity ORM models. The contract is deliberately minimal:

- ``UserSummary`` carries identity plus role/status and NOTHING else: no
  phone, no email, no password material. Contact data stays inside the
  identity module, so a consumer cannot even accidentally widen its
  dependency to contact handling.
- The adapter is a thin mapping over the existing ``UserRepository``; every
  lookup joins the caller's transaction (same session-in, answer-out shape
  as the other cross-module ports), so no second connection or commit ever
  appears inside a consumer's unit of work.
- ``find_by_email`` normalizes its argument (strip + lowercase) exactly
  once, mirroring how identity itself normalizes emails, so consumers may
  pass raw user input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.repository import UserRepository

__all__ = [
    "DisplayProfile",
    "SqlAlchemyUserDirectory",
    "UserDirectory",
    "UserSummary",
]


@dataclass(frozen=True, slots=True)
class UserSummary:
    """The frozen cross-module view of one account (no contact fields)."""

    user_id: UUID
    username: str
    role: Role
    status: UserStatus


@dataclass(frozen=True, slots=True)
class DisplayProfile:
    """The ranking-safe display facts of one account (spec §17/§40).

    Exactly nickname plus the optional display-honor title: no username
    (student number), no ids, no contact fields. The honor title stays a
    ``None``-safe placeholder until the honors tables land (Plan 05 Task 7
    joins ``UserHonor`` here); rankings reads never block on it.
    """

    nickname: str
    display_honor_title: str | None = None


@runtime_checkable
class UserDirectory(Protocol):
    """Read-only identity lookups for non-identity modules."""

    async def find_by_username(
        self, session: AsyncSession, username: str
    ) -> UserSummary | None: ...

    async def find_by_email(
        self, session: AsyncSession, email: str
    ) -> UserSummary | None: ...

    async def get_role(self, session: AsyncSession, user_id: UUID) -> Role | None: ...

    async def get_display_profile(
        self, session: AsyncSession, user_id: UUID
    ) -> DisplayProfile | None: ...


def _summary(user: User) -> UserSummary:
    return UserSummary(
        user_id=user.id,
        username=user.username,
        role=Role(user.role),
        status=UserStatus(user.status),
    )


class SqlAlchemyUserDirectory:
    """The identity module's concrete ``UserDirectory`` implementation.

    Constructor-injected repository (defaults to the real one) so unit
    tests substitute a stub; production wiring needs no arguments.
    """

    def __init__(self, users: UserRepository | None = None) -> None:
        self._users: UserRepository = users if users is not None else UserRepository()

    async def find_by_username(
        self, session: AsyncSession, username: str
    ) -> UserSummary | None:
        """The account holding exactly ``username`` (spec §5.2 match)."""
        user = await self._users.find_by_username(session, username)
        return None if user is None else _summary(user)

    async def find_by_email(
        self, session: AsyncSession, email: str
    ) -> UserSummary | None:
        """The account holding ``email`` (normalized here, spec §5.5)."""
        user = await self._users.find_by_email_normalized(
            session, email.strip().lower()
        )
        return None if user is None else _summary(user)

    async def get_role(self, session: AsyncSession, user_id: UUID) -> Role | None:
        """The account's role, or ``None`` when the id is unknown."""
        user = await self._users.find_by_id(session, user_id)
        return None if user is None else Role(user.role)

    async def get_display_profile(
        self, session: AsyncSession, user_id: UUID
    ) -> DisplayProfile | None:
        """The account's ranking display facts (spec §17: nickname + honor).

        The Plan 05 Task 6 shape reads ``users.nickname`` only; the display
        honor title arrives with the honors module (Task 7) as a join over
        ``UserHonor`` and is ``None`` until then, so a leaderboard read
        never blocks on a table that does not exist yet.
        """
        user = await self._users.find_by_id(session, user_id)
        if user is None:
            return None
        return DisplayProfile(nickname=user.nickname, display_honor_title=None)
