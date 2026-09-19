# backend/app/modules/identity/repository.py
"""Persistence queries for the identity module (backend-engineering §5-7).

Repositories never commit or roll back: the service boundary owns the
transaction lifetime. Queries only read here; the service performs the
writes so the whole invariant stays visible in one place.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.models import StudentWhitelist, User

_STUDENT_NOT_WHITELISTED_MESSAGE = "该学号不在注册白名单中，无法注册"


class StudentWhitelistRepository:
    """Queries over pre-imported student numbers (spec §5.1)."""

    async def require_enabled(
        self, session: AsyncSession, student_number: str
    ) -> StudentWhitelist:
        """Return the entry authorizing ``student_number`` to register.

        Absent and disabled entries are deliberately indistinguishable in
        the rejection (both raise ``STUDENT_NOT_WHITELISTED``): the code is
        the frontend's stable branch and neither state may register now
        (spec §5.1, §29).

        The row is locked ``FOR UPDATE`` inside the caller's transaction so
        an admin disabling an entry cannot slip past a registration that is
        mid-flight (backend-engineering §6): the re-check and the insert
        serialize on the whitelist row itself.
        """
        entry = await session.scalar(
            select(StudentWhitelist)
            .where(StudentWhitelist.student_number == student_number)
            .with_for_update()
        )
        if entry is None or not entry.enabled:
            raise BusinessError(
                ErrorCode.STUDENT_NOT_WHITELISTED,
                _STUDENT_NOT_WHITELISTED_MESSAGE,
                status_code=403,
            )
        return entry


class UserRepository:
    """Read queries over ``users`` needed by registration."""

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
