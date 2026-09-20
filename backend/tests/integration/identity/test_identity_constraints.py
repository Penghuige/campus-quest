# backend/tests/integration/identity/test_identity_constraints.py
"""Database-level identity constraints (spec §5, §31 invariants 1-2).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects duplicates even when the application forgets:
unique username, unique normalized phone, unique non-null normalized email,
whitelist/token uniqueness, enum-CHECK rejection, and string preservation
of student numbers with leading zeros.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import (
    StaffInvitation,
    StudentWhitelist,
    User,
    UserSession,
)

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


def _expires_soon() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


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


@pytest.mark.integration
async def test_duplicate_whitelist_student_number_rejected(
    db_session: AsyncSession,
) -> None:
    """Whitelist student numbers are globally unique (spec §5.1)."""
    db_session.add(StudentWhitelist(student_number="20250010001"))
    await db_session.flush()

    db_session.add(StudentWhitelist(student_number="20250010001"))
    with pytest.raises(IntegrityError, match="uq_student_whitelist_student_number"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_refresh_token_hash_rejected(
    db_session: AsyncSession,
) -> None:
    """One refresh-token hash maps to at most one session (spec §5.6)."""
    owner = _user()
    db_session.add(owner)
    await db_session.flush()

    db_session.add(
        UserSession(
            user_id=owner.id, refresh_token_hash="r" * 64, expires_at=_expires_soon()
        )
    )
    await db_session.flush()

    db_session.add(
        UserSession(
            user_id=owner.id, refresh_token_hash="r" * 64, expires_at=_expires_soon()
        )
    )
    with pytest.raises(IntegrityError, match="uq_user_sessions_refresh_token_hash"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_staff_invitation_token_hash_rejected(
    db_session: AsyncSession,
) -> None:
    """Invitation token hashes never collide across outstanding invites."""
    creator = _user()
    db_session.add(creator)
    await db_session.flush()

    db_session.add(
        StaffInvitation(
            email_normalized="teacher@pku.edu.cn",
            role=Role.TEACHER,
            token_hash="i" * 64,
            expires_at=_expires_soon(),
            created_by=creator.id,
        )
    )
    await db_session.flush()

    db_session.add(
        StaffInvitation(
            email_normalized="admin@pku.edu.cn",
            role=Role.ADMIN,
            token_hash="i" * 64,
            expires_at=_expires_soon(),
            created_by=creator.id,
        )
    )
    with pytest.raises(IntegrityError, match="uq_staff_invitations_token_hash"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_invalid_user_role_rejected(db_session: AsyncSession) -> None:
    user = _user()
    user.role = "UNDERCLASSMAN"  # type: ignore[assignment]
    db_session.add(user)

    with pytest.raises(IntegrityError, match="ck_users_role"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_invalid_user_status_rejected(db_session: AsyncSession) -> None:
    user = _user()
    user.status = "GHOST"  # type: ignore[assignment]
    db_session.add(user)

    with pytest.raises(IntegrityError, match="ck_users_status"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_invalid_staff_invitation_role_rejected(
    db_session: AsyncSession,
) -> None:
    creator = _user()
    db_session.add(creator)
    await db_session.flush()

    db_session.add(
        StaffInvitation(
            email_normalized="student@pku.edu.cn",
            role="STUDENT",  # staff invitations admit TEACHER/ADMIN only
            token_hash="s" * 64,
            expires_at=_expires_soon(),
            created_by=creator.id,
        )
    )
    with pytest.raises(IntegrityError, match="ck_staff_invitations_role"):
        await db_session.flush()
    await db_session.rollback()
