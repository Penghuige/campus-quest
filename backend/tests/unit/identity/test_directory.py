# backend/tests/unit/identity/test_directory.py
"""Unit tests for the frozen cross-module identity read port.

docs/architecture/interfaces.md freezes `UserDirectory` as the ONLY way
later modules (points, community, ranking, the admin surface) may read
account facts — they consume the port, never identity ORM models.
`UserSummary` is the whole contract: identity plus role/status, NO contact
fields (phone and email stay inside the identity module).

The concrete adapter is a thin delegation over `UserRepository`; these
tests drive it with a stub repository (no database) to pin the mapping, the
email normalization, and the frozen field set.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.modules.identity.directory import (
    SqlAlchemyUserDirectory,
    UserDirectory,
    UserSummary,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User

_STAFF_ID = uuid4()
_STUDENT_ID = uuid4()


@dataclass
class _Lookup:
    """One recorded repository call (method name + argument)."""

    method: str
    argument: Any


class _StubUserRepository:
    """Records repository calls; answers from a canned user table."""

    def __init__(self, users: list[User]) -> None:
        self._users = users
        self.calls: list[_Lookup] = []

    async def find_by_username(self, session: Any, username: str) -> User | None:
        self.calls.append(_Lookup("find_by_username", username))
        return next((u for u in self._users if u.username == username), None)

    async def find_by_email_normalized(
        self, session: Any, email_normalized: str
    ) -> User | None:
        self.calls.append(_Lookup("find_by_email_normalized", email_normalized))
        return next(
            (u for u in self._users if u.email_normalized == email_normalized), None
        )

    async def find_by_id(self, session: Any, user_id: UUID) -> User | None:
        self.calls.append(_Lookup("find_by_id", user_id))
        return next((u for u in self._users if u.id == user_id), None)


def _make_user(**overrides: Any) -> User:
    defaults: dict[str, Any] = {
        "id": _STUDENT_ID,
        "username": "20250010001",
        "password_hash": "never-read-through-the-port",
        "nickname": "测试同学",
        "email_normalized": "student@school.edu",
        "phone_e164": "+8613800000000",
        "role": Role.STUDENT,
        "status": UserStatus.ACTIVE,
    }
    defaults.update(overrides)
    return User(**defaults)


def _make_directory(
    users: list[User],
) -> tuple[SqlAlchemyUserDirectory, _StubUserRepository]:
    stub = _StubUserRepository(users)
    return SqlAlchemyUserDirectory(users=stub), stub


def _run(coro: Awaitable[Any]) -> Any:
    # The stub repository never touches a real event loop resource, so a
    # fresh loop per call is deterministic and dependency-free.
    import asyncio

    return asyncio.run(coro)


def test_directory_satisfies_the_frozen_protocol():
    # Structural conformance: the concrete adapter is a UserDirectory.
    assert isinstance(_make_directory([])[0], UserDirectory)


def test_summary_carries_exactly_the_frozen_fields():
    directory, _ = _make_directory([_make_user()])

    summary = _run(directory.find_by_username(None, "20250010001"))

    assert isinstance(summary, UserSummary)
    assert {f.name for f in dataclasses.fields(summary)} == {
        "user_id",
        "username",
        "role",
        "status",
    }


def test_find_by_username_maps_role_and_status_enums():
    directory, _ = _make_directory(
        [_make_user(role=Role.TEACHER, status=UserStatus.SUSPENDED)]
    )

    summary = _run(directory.find_by_username(None, "20250010001"))

    assert summary is not None
    assert summary.user_id == _STUDENT_ID
    assert summary.role is Role.TEACHER
    assert summary.status is UserStatus.SUSPENDED


def test_find_by_email_normalizes_before_delegating():
    directory, stub = _make_directory(
        [_make_user(id=_STAFF_ID, email_normalized="staff@school.edu")]
    )

    summary = _run(directory.find_by_email(None, "  Staff@School.EDU  "))

    # The caller may pass raw casing; the port normalizes (strip + lower)
    # exactly once, mirroring how identity itself normalizes emails.
    assert stub.calls == [_Lookup("find_by_email_normalized", "staff@school.edu")]
    assert summary is not None
    assert summary.user_id == _STAFF_ID


def test_get_role_answers_enum_or_none():
    directory, _ = _make_directory([_make_user(role=Role.ADMIN)])

    assert _run(directory.get_role(None, _STUDENT_ID)) is Role.ADMIN
    assert _run(directory.get_role(None, uuid4())) is None


def test_unknown_lookups_return_none():
    directory, _ = _make_directory([_make_user()])

    assert _run(directory.find_by_username(None, "20990099999")) is None
    assert _run(directory.find_by_email(None, "nobody@school.edu")) is None
