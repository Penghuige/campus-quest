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
- ``get_display_profile`` keeps the repository delegation for nickname
  and user existence; the display-honor title (Plan 05 Task 7's
  ``users.display_honor_id`` pointer) resolves through a
  constructor-injectable resolver whose default is one fresh Core
  SELECT over the rankings-owned ``honors`` table — identity never
  imports rankings models, mirroring the tasks module's ``_USERS_LOCK``
  seam in reverse.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.repository import UserRepository

__all__ = [
    "DisplayHonorTitleResolver",
    "DisplayProfile",
    "SqlAlchemyUserDirectory",
    "UserDirectory",
    "UserSummary",
    "resolve_display_honor_title",
]

# Lock/verify seam for the rankings-owned honors table (see module
# docstring): a typed Core-level light table, NOT the rankings ORM
# model — the Python-level dependency direction of interfaces.md stays
# identity <- rankings, and only the two columns the display read needs
# ride the join.
_HONORS_TITLE = table(
    "honors",
    column("id", Uuid),
    column("name", String),
)

# The display-honor pointer's seam over users: a Core light table on
# purpose — the pointer is WRITTEN cross-module by rankings' own Core
# UPDATE (honor_service.set_display_honor over ITS light table), which
# the identity map would otherwise mask until expiry, so the read must
# not go through the identity-mapped ORM row.
_USERS_POINTER = table(
    "users",
    column("id", Uuid),
    column("display_honor_id", Uuid),
)


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
    (student number), no ids, no contact fields. The title is the ONE
    honor the user chose to display (``users.display_honor_id``; the
    Plan 05 Task 7 honors module owns the choice) and stays ``None``
    while unset.
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


# The injectable display-honor title read: (session, user_id) -> the
# chosen honor's name, or None while unset. Unit tests fake it; the
# production default below joins the light tables.
DisplayHonorTitleResolver = Callable[[AsyncSession, UUID], Awaitable[str | None]]


async def resolve_display_honor_title(
    session: AsyncSession, user_id: UUID
) -> str | None:
    """The user's CHOSEN display honor's name, in ONE fresh Core SELECT.

    ``users.display_honor_id`` JOIN ``honors.name`` over the two light
    tables: fresh on purpose, because the pointer is written by
    rankings' cross-module Core UPDATE, which an identity-mapped ORM
    read would mask until expiry. ``None`` while unset (or pointing at
    a row the join cannot find).
    """
    stmt = (
        select(_HONORS_TITLE.c.name)
        .select_from(
            _USERS_POINTER.join(
                _HONORS_TITLE,
                _USERS_POINTER.c.display_honor_id == _HONORS_TITLE.c.id,
            )
        )
        .where(_USERS_POINTER.c.id == user_id)
    )
    return await session.scalar(stmt)


class SqlAlchemyUserDirectory:
    """The identity module's concrete ``UserDirectory`` implementation.

    Constructor-injected repository and display-honor title resolver
    (both default to the real ones) so unit tests substitute stubs;
    production wiring needs no arguments.
    """

    def __init__(
        self,
        users: UserRepository | None = None,
        honor_title_resolver: DisplayHonorTitleResolver | None = None,
    ) -> None:
        self._users: UserRepository = users if users is not None else UserRepository()
        self._honor_title: DisplayHonorTitleResolver = (
            honor_title_resolver
            if honor_title_resolver is not None
            else resolve_display_honor_title
        )

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

        Nickname and user existence delegate to the repository
        (``find_by_id``); the honor title — the user's CHOSEN display
        honor (``users.display_honor_id``, migration 0008), not "any
        honor they own" — comes from the injected resolver, whose
        production default is a fresh Core SELECT over the honors light
        table (see ``resolve_display_honor_title`` for why fresh).
        """
        user = await self._users.find_by_id(session, user_id)
        if user is None:
            return None
        title = await self._honor_title(session, user_id)
        return DisplayProfile(nickname=user.nickname, display_honor_title=title)
