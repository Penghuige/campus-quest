# backend/tests/integration/identity/test_registration.py
"""Student registration transaction (spec §5.1-5.4; backend-engineering §5-7).

Covers the atomic `IdentityService.register_student` use case: whitelist
re-check, phone-uniqueness, validator wiring, and the database constraints
that adjudicate concurrent registrations (spec §5.4: 并发注册最终必须由
数据库约束兜底 — the friendly checks are UX, the constraints are the law).

Task-3 seams: password hashing arrives in Task 5, so the service accepts a
hasher callable and these tests inject a deterministic stub; the phone OTP
lifecycle arrives in Task 4, so registration consumes an already-verified
token through `PhoneVerificationPort` and these tests seed tokens into the
fake below. Concurrent tests use two independent sessions bound to their
own connections with real commits (backend-engineering §7), so they clean
up the rows they created instead of relying on the rollback harness.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import StudentWhitelist, User
from app.modules.identity.ports import VerifiedPhone
from app.modules.identity.schemas import RegisterStudent, UserPublic
from app.modules.identity.service import IdentityService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_STUDENT_A = "20250010001"
_STUDENT_B = "20250010002"
_STUDENT_LEADING_ZEROS = "000123456"
_PHONE_A = "+8613800138000"
_PHONE_B = "+8613800138001"
_TOKEN_A = "phone-token-a"
_TOKEN_B = "phone-token-b"
_PASSWORD = "correct-horse-battery"
_STUB_HASH_PREFIX = "stub-hash$"


def _stub_hash(password: str) -> str:
    """Deterministic Task-5 stand-in for the Argon2id hasher port."""

    return f"{_STUB_HASH_PREFIX}{password}"


class FakePhoneVerification:
    """Deterministic `PhoneVerificationPort` (Task 4 ships the real one).

    `tokens` maps token -> already-normalized E.164 phone. Single-use
    enforcement belongs to the real challenge lifecycle, not this fake:
    the same-phone concurrency test deliberately uses two distinct tokens
    that resolve to one phone, which is the race the DB constraint closes.
    """

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = dict(tokens)
        self.verified: list[str] = []

    async def verify_phone_token(self, token: str) -> VerifiedPhone:
        try:
            phone_e164 = self._tokens[token]
        except KeyError:
            raise ValueError(f"unknown phone verification token: {token!r}") from None
        self.verified.append(token)
        return VerifiedPhone(phone_e164=phone_e164)


def _make_service(tokens: dict[str, str]) -> IdentityService:
    return IdentityService(
        password_hasher=_stub_hash,
        phone_verification=FakePhoneVerification(tokens),
    )


def _command(
    *,
    student_number: str = _STUDENT_A,
    nickname: str = "  测试同学  ",
    phone_token: str = _TOKEN_A,
    password: str = _PASSWORD,
) -> RegisterStudent:
    return RegisterStudent(
        student_number=student_number,
        nickname=nickname,
        phone_token=phone_token,
        password=password,
    )


async def _seed_whitelist(
    db_session: AsyncSession, *student_numbers: str, enabled: bool = True
) -> None:
    for number in student_numbers:
        db_session.add(StudentWhitelist(student_number=number, enabled=enabled))
    await db_session.flush()


async def _count_users(db_session: AsyncSession, username: str) -> int:
    return int(
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.username == username)
        )
        or 0
    )


@pytest.mark.integration
async def test_register_whitelisted_student_succeeds(db_session: AsyncSession) -> None:
    await _seed_whitelist(db_session, _STUDENT_A)

    user = await _make_service({_TOKEN_A: _PHONE_A}).register_student(
        db_session, _command()
    )

    assert user.username == _STUDENT_A
    assert user.role == Role.STUDENT
    assert user.status == UserStatus.ACTIVE
    assert user.phone_e164 == _PHONE_A
    assert user.nickname == "测试同学"  # normalized: trimmed by T2 validator
    assert user.password_hash == _stub_hash(_PASSWORD)
    assert user.password_hash != _PASSWORD

    db_session.expunge_all()
    persisted = await db_session.scalar(select(User).where(User.username == _STUDENT_A))
    assert persisted is not None
    assert persisted.status == UserStatus.ACTIVE
    assert persisted.phone_e164 == _PHONE_A


@pytest.mark.integration
async def test_register_absent_whitelist_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(BusinessError) as exc_info:
        await _make_service({_TOKEN_A: _PHONE_A}).register_student(
            db_session, _command()
        )

    assert exc_info.value.code == ErrorCode.STUDENT_NOT_WHITELISTED
    assert exc_info.value.status_code < 500
    assert await _count_users(db_session, _STUDENT_A) == 0


@pytest.mark.integration
async def test_register_disabled_whitelist_rejected(db_session: AsyncSession) -> None:
    await _seed_whitelist(db_session, _STUDENT_A, enabled=False)

    with pytest.raises(BusinessError) as exc_info:
        await _make_service({_TOKEN_A: _PHONE_A}).register_student(
            db_session, _command()
        )

    assert exc_info.value.code == ErrorCode.STUDENT_NOT_WHITELISTED
    assert exc_info.value.status_code < 500
    assert await _count_users(db_session, _STUDENT_A) == 0


@pytest.mark.integration
async def test_register_rejects_invalid_student_number_format(
    db_session: AsyncSession,
) -> None:
    # The whitelist entry exists: format validation must still reject first.
    await _seed_whitelist(db_session, _STUDENT_A)

    with pytest.raises(BusinessError) as exc_info:
        await _make_service({_TOKEN_A: _PHONE_A}).register_student(
            db_session, _command(student_number="2025-001")
        )

    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.status_code < 500
    assert await _count_users(db_session, _STUDENT_A) == 0


@pytest.mark.integration
async def test_register_password_outside_band_rejected(
    db_session: AsyncSession,
) -> None:
    # Spec §5.6 band (10-128, no composition rules) enforced at the
    # registration boundary through the Task-5 security constants, and
    # before the single-use phone token is resolved (ordering asserted:
    # the fake records every consumed token).
    await _seed_whitelist(db_session, _STUDENT_A)
    fake_verification = FakePhoneVerification({_TOKEN_A: _PHONE_A})
    service = IdentityService(
        password_hasher=_stub_hash, phone_verification=fake_verification
    )

    for password in ("short", "a" * 129):
        with pytest.raises(BusinessError) as exc_info:
            await service.register_student(db_session, _command(password=password))
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert exc_info.value.status_code < 500
        assert await _count_users(db_session, _STUDENT_A) == 0

    assert fake_verification.verified == []


@pytest.mark.integration
async def test_register_phone_already_bound_rejected(
    db_session: AsyncSession,
) -> None:
    await _seed_whitelist(db_session, _STUDENT_A, _STUDENT_B)
    db_session.add(
        User(
            username="20240010001",
            password_hash=_stub_hash("existing"),
            nickname="已注册同学",
            phone_e164=_PHONE_A,
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
    )
    await db_session.flush()

    with pytest.raises(BusinessError) as exc_info:
        await _make_service({_TOKEN_A: _PHONE_A}).register_student(
            db_session, _command(student_number=_STUDENT_B)
        )

    assert exc_info.value.code == ErrorCode.PHONE_ALREADY_BOUND
    assert exc_info.value.status_code < 500
    assert await _count_users(db_session, _STUDENT_B) == 0


@pytest.mark.integration
async def test_register_leading_zero_student_number_round_trips(
    db_session: AsyncSession,
) -> None:
    await _seed_whitelist(db_session, _STUDENT_LEADING_ZEROS)

    user = await _make_service({_TOKEN_A: _PHONE_A}).register_student(
        db_session, _command(student_number=_STUDENT_LEADING_ZEROS)
    )

    db_session.expunge_all()
    persisted = await db_session.scalar(
        select(User).where(User.username == _STUDENT_LEADING_ZEROS)
    )
    assert persisted is not None
    assert persisted.username == _STUDENT_LEADING_ZEROS
    assert isinstance(persisted.username, str)
    assert user.username == _STUDENT_LEADING_ZEROS


def test_user_public_excludes_private_fields() -> None:
    # Privacy by construction (spec §40): the DTO enumerates its fields, so
    # the hash, the raw phone, and every other internal column can never
    # leak into a response even if the ORM model grows them.
    exposed = set(UserPublic.model_fields)
    assert exposed == {"id", "username", "nickname", "role", "status"}
    assert "password_hash" not in exposed
    assert "phone_e164" not in exposed
    assert "email_normalized" not in exposed


async def _register_on_own_session(
    engine: AsyncEngine, tokens: dict[str, str], command: RegisterStudent
) -> User | BusinessError:
    """Run one registration on an independent session with a real commit.

    Returns the created User or the BusinessError the service raised, so
    `asyncio.gather` results can be classified without losing either side.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        service = _make_service(tokens)
        try:
            return await service.register_student(session, command)
        except BusinessError as exc:
            return exc


async def _seed_committed_whitelist(engine: AsyncEngine, *student_numbers: str) -> None:
    # Committed on a dedicated connection: concurrent registrations on other
    # connections must see these rows (the rollback harness would hide them).
    async with AsyncSession(engine) as session:
        for number in student_numbers:
            session.add(StudentWhitelist(student_number=number))
        await session.commit()


async def _cleanup(engine: AsyncEngine, *, usernames: set[str]) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(delete(User).where(User.username.in_(usernames)))
        await session.execute(
            delete(StudentWhitelist).where(
                StudentWhitelist.student_number.in_(usernames)
            )
        )
        await session.commit()


@pytest.mark.integration
async def test_concurrent_same_username_exactly_one_succeeds(
    db_engine: AsyncEngine,
) -> None:
    await _seed_committed_whitelist(db_engine, _STUDENT_A)
    try:
        results = await asyncio.gather(
            _register_on_own_session(db_engine, {_TOKEN_A: _PHONE_A}, _command()),
            _register_on_own_session(
                db_engine, {_TOKEN_B: _PHONE_B}, _command(phone_token=_TOKEN_B)
            ),
        )

        successes = [result for result in results if isinstance(result, User)]
        failures = [result for result in results if isinstance(result, BusinessError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == ErrorCode.USERNAME_ALREADY_EXISTS
        assert failures[0].status_code < 500

        async with AsyncSession(db_engine) as verifier:
            assert await _count_users(verifier, _STUDENT_A) == 1
    finally:
        await _cleanup(db_engine, usernames={_STUDENT_A})


@pytest.mark.integration
async def test_concurrent_same_phone_exactly_one_succeeds(
    db_engine: AsyncEngine,
) -> None:
    # Two students, two separately verified tokens, one phone: the friendly
    # pre-check cannot save both, the partial unique index must (spec §5.4).
    await _seed_committed_whitelist(db_engine, _STUDENT_A, _STUDENT_B)
    tokens = {_TOKEN_A: _PHONE_A, _TOKEN_B: _PHONE_A}
    try:
        results = await asyncio.gather(
            _register_on_own_session(db_engine, tokens, _command()),
            _register_on_own_session(
                db_engine, tokens, _command(student_number=_STUDENT_B)
            ),
        )

        successes = [result for result in results if isinstance(result, User)]
        failures = [result for result in results if isinstance(result, BusinessError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == ErrorCode.PHONE_ALREADY_BOUND
        assert failures[0].status_code < 500
        assert {success.username for success in successes} <= {_STUDENT_A, _STUDENT_B}

        async with AsyncSession(db_engine) as verifier:
            bound = await verifier.scalars(
                select(User).where(User.phone_e164 == _PHONE_A)
            )
            assert len(list(bound)) == 1
    finally:
        await _cleanup(db_engine, usernames={_STUDENT_A, _STUDENT_B})


def test_user_public_maps_domain_user() -> None:
    user = User(
        id=uuid4(),  # allocated by the database on insert; set here for mapping
        username=_STUDENT_A,
        password_hash="secret-material",
        nickname="测试同学",
        phone_e164=_PHONE_A,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )

    public = UserPublic.from_user(user)

    assert public.username == _STUDENT_A
    assert public.nickname == "测试同学"
    assert public.role is Role.STUDENT
    assert public.status is UserStatus.ACTIVE
