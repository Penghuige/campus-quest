# backend/tests/integration/identity/test_identity_constraints.py
"""Database-level identity constraints (spec §5, §31 invariants 1-2).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects duplicates even when the application forgets:
unique username, unique normalized phone, unique non-null normalized email,
and string preservation of student numbers with leading zeros.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User

_PHONE_A = "+8613800138000"
_PHONE_B = "+8613800138001"


def _user(
    *,
    username: str = "20250010001",
    phone_e164: str | None = _PHONE_A,
    email_normalized: str | None = None,
) -> User:
    """Minimal valid user row; identity keys vary per test."""
    return User(
        username=username,
        password_hash="$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG",
        nickname="测试同学",
        phone_e164=phone_e164,
        email_normalized=email_normalized,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


@pytest.mark.integration
async def test_duplicate_username_rejected(db_session: AsyncSession) -> None:
    db_session.add(_user(username="000123456", phone_e164=_PHONE_A))
    await db_session.flush()

    db_session.add(_user(username="000123456", phone_e164=_PHONE_B))
    with pytest.raises(IntegrityError, match="uq_users_username"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_normalized_phone_rejected(db_session: AsyncSession) -> None:
    db_session.add(_user(username="20250010001", phone_e164=_PHONE_A))
    await db_session.flush()

    db_session.add(_user(username="20250010002", phone_e164=_PHONE_A))
    with pytest.raises(IntegrityError, match="uq_users_phone_e164"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_non_null_email_rejected(db_session: AsyncSession) -> None:
    db_session.add(
        _user(
            username="20250010001",
            phone_e164=_PHONE_A,
            email_normalized="student@pku.edu.cn",
        )
    )
    await db_session.flush()

    db_session.add(
        _user(
            username="20250010002",
            phone_e164=_PHONE_B,
            email_normalized="student@pku.edu.cn",
        )
    )
    with pytest.raises(IntegrityError, match="uq_users_email_normalized"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_multiple_null_emails_allowed(db_session: AsyncSession) -> None:
    """NULL emails never collide: the unique index is partial (spec §5.5)."""
    db_session.add(_user(username="20250010001", email_normalized=None))
    db_session.add(
        _user(username="20250010002", phone_e164=_PHONE_B, email_normalized=None)
    )

    await db_session.flush()


@pytest.mark.integration
async def test_student_number_round_trips_as_string(db_session: AsyncSession) -> None:
    """Student numbers stay strings; leading zeros must survive (spec §5.2)."""
    db_session.add(_user(username="000123456", phone_e164=_PHONE_A))
    await db_session.flush()
    db_session.expunge_all()

    loaded = await db_session.scalar(select(User).where(User.username == "000123456"))

    assert loaded is not None
    assert loaded.username == "000123456"
    assert isinstance(loaded.username, str)
