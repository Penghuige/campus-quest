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
- ``get_display_profile`` resolves the display-honor title through the
  ``users.display_honor_id`` pointer (Plan 05 Task 7's honors module
  owns the choice). The honors table is rankings-owned, so the name
  rides a typed Core light table — identity never imports rankings
  models, mirroring the tasks module's ``_USERS_LOCK`` seam in reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import String, Uuid, column, select, table
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

# The display-profile read's seam over users: nickname plus the
# display-honor pointer in ONE fresh SELECT. A Core light table rather
# than the identity-mapped ORM row on purpose — the pointer is written
# by rankings' own Core UPDATE (honor_service.set_display_honor over
# ITS light table), which the identity map would otherwise mask until
# expiry.
_USERS_PROFILE = table(
    "users",
    column("id", Uuid),
    column("nickname", String),
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

        The honor title is the user's CHOSEN display honor
        (``users.display_honor_id``, migration 0008) — not "any honor
        they own" — and stays ``None`` while unset or pointing at a
        row the light-table read cannot find. Both columns ride fresh
        Core SELECTs (see ``_USERS_PROFILE``): the pointer is written
        cross-module by rankings' Core UPDATE, so an identity-mapped
        ORM read could serve a stale choice.
        """
        row = (
            await session.execute(
                select(
                    _USERS_PROFILE.c.nickname, _USERS_PROFILE.c.display_honor_id
                ).where(_USERS_PROFILE.c.id == user_id)
            )
        ).one_or_none()
        if row is None:
            return None
        title: str | None = None
        if row.display_honor_id is not None:
            title = await session.scalar(
                select(_HONORS_TITLE.c.name).where(
                    _HONORS_TITLE.c.id == row.display_honor_id
                )
            )
        return DisplayProfile(nickname=row.nickname, display_honor_title=title)
