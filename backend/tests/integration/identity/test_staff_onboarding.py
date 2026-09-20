# backend/tests/integration/identity/test_staff_onboarding.py
"""Staff invitation and mandatory TOTP 2FA onboarding against PostgreSQL.

Spec §5.6/§5.8: only Admin creates staff invitations (one-time, short-lived
links); accepting sets an Argon2id password and opens a PENDING session that
is not a management session until TOTP is confirmed; recovery codes are
shown once and stored as hashes; staff login = verified email + password +
valid TOTP (or one unused recovery code).

The six brief behaviors map to tests one-to-one: non-Admin cannot invite
(`test_non_admin_actor_cannot_create_invitation`), Admin invites Teacher
(`test_admin_invites_teacher_records_hashed_single_use_token`), token
expiry and single-use (`test_expired_invitation_cannot_be_accepted`,
`test_invitation_is_single_use`), no management session before TOTP
confirmation (`test_accept_before_totp_confirm_is_not_a_management_session`),
correct TOTP enables login (`test_correct_totp_enables_staff_login`),
recovery code works once (`test_recovery_code_works_once_only`).

backend-engineering §15/§16: invitation token, TOTP secret, otpauth URI,
and recovery codes never reach a log line; the TOTP secret rests encrypted
(Fernet), never plaintext.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import (
    AccessTokenCodec,
    hash_password,
    hash_refresh_token,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import (
    STAFF_INVITATION_ACCEPTED,
    STAFF_INVITATION_CREATED,
    TOTP_ENABLED,
    Actor,
    InMemoryEventCollector,
)
from app.modules.identity.models import (
    RecoveryCode,
    StaffInvitation,
    TotpCredential,
    User,
    UserSession,
)
from app.modules.identity.session_service import SessionService, SessionTokens
from app.modules.identity.staff_service import (
    PendingStaffSession,
    StaffService,
    TotpSetupRequiredError,
)

# Anchored to the real now: PyJWT validates ``exp`` against wall-clock time,
# so access tokens decoded in assertions must be minted "just now". Every
# other expiry (invitation TTL, refresh window) is FrozenClock-driven.
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_TEACHER_EMAIL = "Chen.Li@campus.example.edu.cn"
_TEACHER_EMAIL_NORMALIZED = "chen.li@campus.example.edu.cn"
_ACCESS_SECRET = "integration-test-access-token-secret-0123456789"
_ACCESS_TTL_MINUTES = 15
_REFRESH_TTL_DAYS = 30
_INVITATION_TTL_HOURS = 48
# One key for the whole module: it stands in for the Settings-sourced
# deployment key; rotation behavior needs no fresh key per test.
_FERNET_KEY = Fernet.generate_key()


async def _seed_user(
    db: AsyncSession,
    *,
    username: str,
    role: Role,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="种子用户",
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


def _make_service(
    clock: FrozenClock, events: InMemoryEventCollector | None = None
) -> StaffService:
    return StaffService(
        clock=clock,
        sessions=SessionService(
            clock=clock,
            access_codec=AccessTokenCodec(
                secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
            ),
            refresh_token_ttl_days=_REFRESH_TTL_DAYS,
        ),
        fernet=Fernet(_FERNET_KEY),
        events=events if events is not None else InMemoryEventCollector(),
        invitation_ttl_hours=_INVITATION_TTL_HOURS,
    )


def _actor(user: User, role: Role | None = None) -> Actor:
    return Actor(user_id=user.id, role=role if role is not None else Role(user.role))


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


def _code_for(secret: str, clock: FrozenClock) -> str:
    return pyotp.TOTP(secret).at(clock.now())


async def _onboard_confirmed_staff(
    db: AsyncSession, clock: FrozenClock, service: StaffService, *, role: Role
) -> tuple[User, TotpCredential, list[str], str]:
    """Drive invite -> accept -> begin -> confirm; return the ready account.

    Shared by the login and recovery-code tests so each still asserts its
    own behavior on top of a realistic, fully onboarded staff account.
    """
    admin = await _seed_user(db, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db, _actor(admin), _TEACHER_EMAIL, role
    )
    pending = await service.accept_staff_invitation(db, issued.token, _PASSWORD)
    setup = await service.begin_totp_setup(db, pending.user_id)
    codes = await service.confirm_totp_setup(
        db, pending.user_id, _code_for(setup.secret, clock)
    )
    credential = await db.get(TotpCredential, pending.user_id)
    assert credential is not None
    user = await db.get(User, pending.user_id)
    assert user is not None
    return user, credential, codes, setup.secret


@pytest.mark.integration
async def test_non_admin_actor_cannot_create_invitation(
    db_session: AsyncSession,
) -> None:
    # Brief behavior 1: ONLY Admin creates invitations — a Student or
    # Teacher actor is denied before any row is written (spec §5.8; the
    # Actor role is the server-side authorization input, never a
    # client-supplied value — backend-engineering §16).
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    for role in (Role.STUDENT, Role.TEACHER):
        actor_user = await _seed_user(
            db_session, username=f"actor-{role.value.lower()}", role=role
        )

        with pytest.raises(BusinessError) as exc_info:
            await service.create_staff_invitation(
                db_session, _actor(actor_user), _TEACHER_EMAIL, Role.TEACHER
            )

        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED
        assert exc_info.value.status_code == 403

    invitations = await db_session.scalar(
        select(func.count())
        .select_from(StaffInvitation)
        .where(StaffInvitation.email_normalized == _TEACHER_EMAIL_NORMALIZED)
    )
    assert invitations == 0


@pytest.mark.integration
async def test_admin_invites_teacher_records_hashed_single_use_token(
    db_session: AsyncSession,
) -> None:
    # Brief behavior 2: Admin can invite a Teacher. The server generates the
    # token, persists only its SHA-256 digest, and the expiry comes from the
    # invitation TTL (default 48h in Settings).
    clock = FrozenClock(_T0)
    events = InMemoryEventCollector()
    service = _make_service(clock, events)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)

    issued = await service.create_staff_invitation(
        db_session, _actor(admin), f"  {_TEACHER_EMAIL} ", Role.TEACHER
    )

    assert issued.token  # plaintext token exists only in the return value
    row = await db_session.scalar(
        select(StaffInvitation).where(
            StaffInvitation.token_hash == hash_refresh_token(issued.token)
        )
    )
    assert row is not None
    assert row.role == Role.TEACHER.value
    assert row.email_normalized == _TEACHER_EMAIL_NORMALIZED  # case/space normalized
    assert row.token_hash == hashlib.sha256(issued.token.encode()).hexdigest()
    assert issued.token not in row.token_hash
    assert row.accepted_at is None
    assert row.created_by == admin.id
    assert row.expires_at == _T0 + timedelta(hours=_INVITATION_TTL_HOURS)

    created = [e for e in events.events if e.event_type == STAFF_INVITATION_CREATED]
    assert len(created) == 1
    assert created[0].aggregate_type == "StaffInvitation"
    assert created[0].aggregate_id == row.id
    assert created[0].occurred_at == _T0
    assert created[0].payload["email"] == _TEACHER_EMAIL_NORMALIZED
    assert created[0].payload["role"] == Role.TEACHER.value
    assert created[0].payload["invited_by"] == str(admin.id)


@pytest.mark.integration
async def test_admin_cannot_invite_into_student_role(db_session: AsyncSession) -> None:
    # Staff invitations exist to create TEACHER/ADMIN accounts only; the
    # student path is whitelist self-registration (spec §5.8).
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)

    with pytest.raises(BusinessError) as exc_info:
        await service.create_staff_invitation(
            db_session, _actor(admin), _TEACHER_EMAIL, Role.STUDENT
        )

    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.status_code == 400
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StaffInvitation)
            .where(StaffInvitation.email_normalized == _TEACHER_EMAIL_NORMALIZED)
        )
        == 0
    )


@pytest.mark.integration
async def test_expired_invitation_cannot_be_accepted(db_session: AsyncSession) -> None:
    # Brief behavior 3 (expiry): past the TTL window the one-time link is
    # dead; the failed attempt consumes nothing and creates no account.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
    )

    _advance(clock, hours=_INVITATION_TTL_HOURS, seconds=1)

    with pytest.raises(BusinessError) as exc_info:
        await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    # No staff account for the invited email, and the failed attempt
    # consumed nothing (scoped assertions: the shared integration database
    # can carry unrelated rows from earlier debugging).
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email_normalized == _TEACHER_EMAIL_NORMALIZED)
        )
        == 0
    )
    row = await db_session.scalar(
        select(StaffInvitation).where(
            StaffInvitation.token_hash == hash_refresh_token(issued.token)
        )
    )
    assert row is not None and row.accepted_at is None


@pytest.mark.integration
async def test_invitation_accepted_at_ttl_boundary_still_works(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
    )

    _advance(clock, hours=_INVITATION_TTL_HOURS, seconds=-1)
    pending = await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)

    assert pending.must_setup_totp is True


@pytest.mark.integration
async def test_invitation_is_single_use(db_session: AsyncSession) -> None:
    # Brief behavior 3 (single-use): a second presentation of the same
    # token — with the correct password even — must fail and create no
    # second account.
    clock = FrozenClock(_T0)
    events = InMemoryEventCollector()
    service = _make_service(clock, events)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
    )

    first = await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)
    with pytest.raises(BusinessError) as exc_info:
        await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    users = (
        await db_session.scalars(
            select(User).where(User.email_normalized == _TEACHER_EMAIL_NORMALIZED)
        )
    ).all()
    assert len(users) == 1
    assert users[0].id == first.user_id
    accepted = [e for e in events.events if e.event_type == STAFF_INVITATION_ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0].aggregate_type == "User"
    assert accepted[0].aggregate_id == first.user_id
    assert accepted[0].payload["role"] == Role.TEACHER.value


async def _accept_on_own_session(
    engine: AsyncEngine, service: StaffService, token: str
) -> PendingStaffSession | BusinessError:
    """Run one invitation acceptance on an independent session.

    Real commits on the session's own engine connection (the rollback
    harness cannot express two racers), returning the pending session or
    the BusinessError so `asyncio.gather` results classify without losing
    either side — the T3 concurrent-registration pattern. Default
    ``expire_on_commit`` also regression-guards the service's pre-commit
    id-capture discipline: post-commit attribute access would raise
    MissingGreenlet here even though the harness hides it.
    """
    async with AsyncSession(engine) as session:
        try:
            return await service.accept_staff_invitation(session, token, _PASSWORD)
        except BusinessError as exc:
            return exc


async def _seed_committed_invitation(
    engine: AsyncEngine, service: StaffService, *, email: str, admin_name: str
) -> str:
    """Commit the admin and the invitation on one dedicated connection.

    Concurrent acceptors on other connections must see both rows (the
    rollback harness would hide uncommitted seeds).
    """
    async with AsyncSession(engine) as session:
        admin = User(
            username=admin_name,
            password_hash=hash_password(_PASSWORD),
            nickname="并发管理员",
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        await session.flush()
        issued = await service.create_staff_invitation(
            session, _actor(admin), email, Role.TEACHER
        )  # commits the admin and the invitation together
        return issued.token


async def _cleanup_committed_staff_rows(
    engine: AsyncEngine, *, usernames: set[str], email: str
) -> None:
    """Delete rows this test committed (registration's cleanup pattern).

    Deletion order respects the FKs: invitations reference their creating
    admin user, and sessions reference their user, so both go before the
    users themselves.
    """
    async with AsyncSession(engine) as session:
        await session.execute(
            delete(StaffInvitation).where(StaffInvitation.email_normalized == email)
        )
        users = (
            await session.scalars(select(User).where(User.username.in_(usernames)))
        ).all()
        for user in users:
            await session.execute(
                delete(UserSession).where(UserSession.user_id == user.id)
            )
            await session.delete(user)
        await session.commit()


@pytest.mark.integration
async def test_concurrent_accept_of_one_invitation_exactly_one_succeeds(
    db_engine: AsyncEngine,
) -> None:
    # Two independent sessions race the SAME single-use token with real
    # commits. Sequential-only coverage would degrade silently if the
    # ``FOR UPDATE`` row lock were ever refactored away (a plain-SELECT
    # re-check passes every sequential test); this pins the guarantee the
    # same way T3 pinned the unique-index races.
    suffix = uuid4().hex[:8]
    email = f"concurrent-{suffix}@campus.example.edu.cn"
    admin_name = f"conc-admin-{suffix}"
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    token = await _seed_committed_invitation(
        db_engine, service, email=email, admin_name=admin_name
    )
    try:
        results = await asyncio.gather(
            _accept_on_own_session(db_engine, service, token),
            _accept_on_own_session(db_engine, service, token),
        )

        wins = [result for result in results if isinstance(result, PendingStaffSession)]
        losses = [result for result in results if isinstance(result, BusinessError)]
        assert len(wins) == 1
        assert len(losses) == 1
        assert losses[0].code == ErrorCode.AUTHENTICATION_REQUIRED
        assert losses[0].status_code == 401

        async with AsyncSession(db_engine) as verifier:
            users = (
                await verifier.scalars(
                    select(User).where(User.email_normalized == email)
                )
            ).all()
            assert len(users) == 1
            assert users[0].id == wins[0].user_id
            invitation = await verifier.scalar(
                select(StaffInvitation).where(
                    StaffInvitation.token_hash == hash_refresh_token(token)
                )
            )
            assert invitation is not None
            assert invitation.accepted_at is not None  # consumed exactly once
            sessions = (
                await verifier.scalars(
                    select(UserSession).where(UserSession.user_id == users[0].id)
                )
            ).all()
            assert len(sessions) == 1  # only the winner minted one
    finally:
        await _cleanup_committed_staff_rows(
            db_engine, usernames={email, admin_name}, email=email
        )


@pytest.mark.integration
async def test_accept_before_totp_confirm_is_not_a_management_session(
    db_session: AsyncSession,
) -> None:
    # Brief behavior 4: acceptance proves identity (a pending session with
    # an explicit must-setup flag) but is NOT a management session — staff
    # login itself refuses the account until TOTP is confirmed (spec §5.8
    # step 3; Task 7 dependencies enforce the same two-factor state on
    # management endpoints).
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
    )

    pending = await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)

    assert isinstance(pending, PendingStaffSession)
    assert pending.must_setup_totp is True
    assert pending.role == Role.TEACHER
    user = await db_session.get(User, pending.user_id)
    assert user is not None
    assert user.status == UserStatus.ACTIVE  # staff have no phone: no PENDING_PHONE
    assert user.role == Role.TEACHER.value
    assert user.phone_e164 is None
    # V1 simplification (documented): the invitation email is treated as
    # verified on acceptance because the single-use link was delivered to
    # that address; staff login uses it as the identifier.
    assert user.email_normalized == _TEACHER_EMAIL_NORMALIZED
    assert user.email_verified_at == _T0
    assert user.username == _TEACHER_EMAIL_NORMALIZED

    with pytest.raises(TotpSetupRequiredError):
        await service.authenticate_staff(
            db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, "123456"
        )

    # Starting setup is not enough: until a valid code is confirmed, the
    # account still cannot log in.
    setup = await service.begin_totp_setup(db_session, pending.user_id)
    with pytest.raises(TotpSetupRequiredError):
        await service.authenticate_staff(
            db_session,
            _TEACHER_EMAIL_NORMALIZED,
            _PASSWORD,
            _code_for(setup.secret, clock),
        )

    credential = await db_session.get(TotpCredential, pending.user_id)
    assert credential is not None and credential.confirmed_at is None


@pytest.mark.integration
async def test_correct_totp_enables_staff_login(db_session: AsyncSession) -> None:
    # Brief behavior 5: confirming with one valid code enables TOTP, mints
    # 8 one-time recovery codes (returned exactly once, stored hashed), and
    # staff login then succeeds with a current TOTP code.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    user, credential, codes, secret = await _onboard_confirmed_staff(
        db_session, clock, service, role=Role.TEACHER
    )

    assert credential.confirmed_at == _T0
    # The resting bytes are the Fernet ciphertext, not the secret.
    assert credential.secret_encrypted != secret.encode()
    assert secret.encode() not in credential.secret_encrypted
    assert Fernet(_FERNET_KEY).decrypt(credential.secret_encrypted) == secret.encode()

    assert len(codes) == 8
    stored = (
        await db_session.scalars(
            select(RecoveryCode).where(RecoveryCode.user_id == user.id)
        )
    ).all()
    assert len(stored) == 8
    for code in codes:
        assert code not in {row.code_hash for row in stored}

    tokens = await service.authenticate_staff(
        db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, _code_for(secret, clock)
    )
    assert isinstance(tokens, SessionTokens)
    claims = AccessTokenCodec(
        secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
    ).decode(tokens.access_token)
    assert claims.sub == str(user.id)
    assert claims.role == Role.TEACHER.value


@pytest.mark.integration
async def test_wrong_totp_code_rejected_without_session(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    user, _, _, secret = await _onboard_confirmed_staff(
        db_session, clock, service, role=Role.TEACHER
    )

    wrong = _code_for(secret, clock)
    wrong = "000000" if wrong != "000000" else "111111"
    with pytest.raises(BusinessError) as exc_info:
        await service.authenticate_staff(
            db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, wrong
        )

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    # Wrong second factor must never open a session (wrong-TOTP attempts
    # are counted server-side via the rejection log; rate limiting is
    # documented as deferred). The pending onboarding session may exist;
    # the failed login attempt must not add to it.
    before = await db_session.scalar(
        select(func.count())
        .select_from(UserSession)
        .where(UserSession.user_id == user.id)
    )
    with pytest.raises(BusinessError):
        await service.authenticate_staff(
            db_session, _TEACHER_EMAIL_NORMALIZED, "wrong-horse-battery", "000000"
        )
    after = await db_session.scalar(
        select(func.count())
        .select_from(UserSession)
        .where(UserSession.user_id == user.id)
    )
    assert after == before


@pytest.mark.integration
async def test_recovery_code_works_once_only(db_session: AsyncSession) -> None:
    # Brief behavior 6: a recovery code substitutes for the TOTP code
    # exactly once; replaying it fails while the other codes stay usable.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    _, _, codes, secret = await _onboard_confirmed_staff(
        db_session, clock, service, role=Role.TEACHER
    )

    tokens = await service.authenticate_staff(
        db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, codes[0]
    )
    assert isinstance(tokens, SessionTokens)

    with pytest.raises(BusinessError) as replay_exc:
        await service.authenticate_staff(
            db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, codes[0]
        )
    assert replay_exc.value.code == ErrorCode.AUTHENTICATION_REQUIRED

    # Other codes remain usable, and the TOTP path still works alongside.
    again = await service.authenticate_staff(
        db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, codes[1]
    )
    assert isinstance(again, SessionTokens)
    via_totp = await service.authenticate_staff(
        db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, _code_for(secret, clock)
    )
    assert isinstance(via_totp, SessionTokens)


@pytest.mark.integration
async def test_confirm_requires_valid_code_before_enabling(
    db_session: AsyncSession,
) -> None:
    # One valid code is proof of possession; a wrong code leaves the
    # credential unconfirmed and issues no recovery codes (spec §5.8).
    clock = FrozenClock(_T0)
    events = InMemoryEventCollector()
    service = _make_service(clock, events)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
    )
    pending = await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)
    setup = await service.begin_totp_setup(db_session, pending.user_id)

    with pytest.raises(BusinessError) as exc_info:
        await service.confirm_totp_setup(db_session, pending.user_id, "000000")

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    credential = await db_session.get(TotpCredential, pending.user_id)
    assert credential is not None and credential.confirmed_at is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RecoveryCode)
            .where(RecoveryCode.user_id == pending.user_id)
        )
        == 0
    )
    assert not [e for e in events.events if e.event_type == TOTP_ENABLED]

    # A wrong attempt burns nothing: the correct code still confirms.
    codes = await service.confirm_totp_setup(
        db_session, pending.user_id, _code_for(setup.secret, clock)
    )
    assert len(codes) == 8
    enabled = [e for e in events.events if e.event_type == TOTP_ENABLED]
    assert len(enabled) == 1
    assert enabled[0].aggregate_type == "User"
    assert enabled[0].aggregate_id == pending.user_id


@pytest.mark.integration
async def test_begin_totp_setup_rotates_unconfirmed_secret(
    db_session: AsyncSession,
) -> None:
    # Re-running begin before confirm replaces the pending secret: the
    # displayed QR is always the credential that confirm will verify.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)
    issued = await service.create_staff_invitation(
        db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
    )
    pending = await service.accept_staff_invitation(db_session, issued.token, _PASSWORD)

    first = await service.begin_totp_setup(db_session, pending.user_id)
    second = await service.begin_totp_setup(db_session, pending.user_id)

    assert second.secret != first.secret
    codes = await service.confirm_totp_setup(
        db_session, pending.user_id, _code_for(second.secret, clock)
    )
    assert len(codes) == 8
    credential = await db_session.get(TotpCredential, pending.user_id)
    assert credential is not None
    assert Fernet(_FERNET_KEY).decrypt(credential.secret_encrypted) == (
        second.secret.encode()
    )


@pytest.mark.integration
async def test_staff_login_rejects_student_account_uniformly(
    db_session: AsyncSession,
) -> None:
    # A student account has no staff second factor by design; the staff
    # login must answer with the uniform authentication failure, never a
    # setup-required signal that would leak the account's role.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    await _seed_user(db_session, username="20250010002", role=Role.STUDENT)

    with pytest.raises(BusinessError) as exc_info:
        await service.authenticate_staff(db_session, "20250010002", _PASSWORD, "123456")

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401


@pytest.mark.integration
async def test_onboarding_flow_never_logs_secrets(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    # backend-engineering §15: the invitation token, password, TOTP secret,
    # otpauth URI, recovery codes, and session tokens never appear in any
    # log line across the whole onboarding flow.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    admin = await _seed_user(db_session, username="campus-admin", role=Role.ADMIN)

    with caplog.at_level(logging.INFO):
        issued = await service.create_staff_invitation(
            db_session, _actor(admin), _TEACHER_EMAIL, Role.TEACHER
        )
        pending = await service.accept_staff_invitation(
            db_session, issued.token, _PASSWORD
        )
        setup = await service.begin_totp_setup(db_session, pending.user_id)
        codes = await service.confirm_totp_setup(
            db_session, pending.user_id, _code_for(setup.secret, clock)
        )
        tokens = await service.authenticate_staff(
            db_session,
            _TEACHER_EMAIL_NORMALIZED,
            _PASSWORD,
            _code_for(setup.secret, clock),
        )
        # Failure paths inside the same capture: a recovery code works once
        # (its replay and a wrong TOTP code both fail), so the rejection
        # log lines are exercised too — and must still leak nothing.
        await service.authenticate_staff(
            db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, codes[0]
        )
        with pytest.raises(BusinessError):
            await service.authenticate_staff(
                db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, codes[0]
            )
        with pytest.raises(BusinessError):
            await service.authenticate_staff(
                db_session, _TEACHER_EMAIL_NORMALIZED, _PASSWORD, "000000"
            )

    secret_material = {
        issued.token,
        _PASSWORD,
        setup.secret,
        setup.otpauth_uri,
        tokens.access_token,
        tokens.refresh_token,
        *codes,
    }
    for record in caplog.records:
        message = record.getMessage()
        for secret in secret_material:
            assert secret not in message
