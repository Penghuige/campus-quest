# backend/tests/integration/identity/test_account_status.py
"""Role and account-status guards over real PostgreSQL (spec §4, §5.6-5.8,
§33.4; backend-engineering §3, §16).

Real dependency wiring on a throwaway app: `create_app()` plus a few
one-off protected routes (the real routes arrive with T9). The guards run
for real — real JWT decode against settings, real user+session JOIN,
real TOTP lookup. Two seams are test overrides, not fakes of the guards:

- `get_db_session` -> the rollback-harness session (guards query the same
  transaction the test seeds, so nothing leaks between tests);
- `rbac.get_role_bearer` -> `get_actor`, exactly the composition-root
  wiring T9 installs (see app/core/rbac.py).

`TotpSetupRequiredError` is rendered by a THROWAWAY handler previewing
T9's doc-first §29 code; only the exception type is this task's contract.

Time is FrozenClock-driven where the guards compare business time
(session liveness), and anchored to the real now where PyJWT validates
token expiry against wall-clock time (backend-engineering §11).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rbac
from app.core.clock import Clock, FrozenClock
from app.core.error_codes import ErrorCode
from app.core.security import AccessTokenCodec, hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_actor,
    get_business_clock,
    require_active_actor,
    require_active_community_actor,
    require_admin_actor,
    require_staff_management_actor,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import TotpCredential, User, UserSession
from app.modules.identity.session_service import SessionService, SessionTokens
from app.modules.identity.staff_service import TotpSetupRequiredError

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
# >= 32 bytes so PyJWT does not warn about short HS256 keys (RFC 7518 §3.2).
_WRONG_SIGNING_SECRET = "integration-wrong-access-secret-0123456789ab"


async def _seed_user(
    db: AsyncSession,
    *,
    role: Role,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=uuid4().hex,
        password_hash=hash_password(_PASSWORD),
        nickname="测试用户",
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


async def _open_session(
    db: AsyncSession, user: User, *, codec: AccessTokenCodec, clock: Clock
) -> tuple[UserSession, SessionTokens]:
    """A real `UserSession` row plus its token pair, uncommitted.

    Routes are exercised through their dependency chain, not through
    login, so suspended/banned and not-yet-2FA accounts can hold the
    otherwise-valid tokens their guards must reject.
    """
    service = SessionService(clock=clock, access_codec=codec)
    return await service.issue_session(db, user=user, now=clock.now())


async def _seed_totp(db: AsyncSession, user: User, *, confirmed: bool) -> None:
    # The guards never decrypt the secret; only confirmed_at matters here.
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"integration-test-secret-not-decrypted",
            confirmed_at=_T0 if confirmed else None,
        )
    )
    await db.flush()


def _build_app(db: AsyncSession, clock: Clock) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: clock
    # The composition-root wiring for core's role-guard seam: the identity
    # actor dependency IS the bearer provider (see app/core/rbac.py).
    app.dependency_overrides[rbac.get_role_bearer] = get_actor

    @app.post("/test/student-op")
    async def student_op(
        actor: Annotated[Actor, Depends(require_active_actor)],
    ) -> dict[str, str]:
        # Stand-in for a state-changing student endpoint (claim/submit/
        # comment): the §5.7 SUSPENDED/BANNED gate.
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @app.post("/test/staff-op")
    async def staff_op(
        actor: Annotated[Actor, Depends(rbac.require_role(Role.TEACHER, Role.ADMIN))],
    ) -> dict[str, str]:
        # Stand-in for a Teacher-or-Admin operation.
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @app.post("/test/admin-op")
    async def admin_op(
        actor: Annotated[Actor, Depends(rbac.require_role(Role.ADMIN))],
    ) -> dict[str, str]:
        # Stand-in for an Admin-only operation.
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @app.post("/test/manage-op")
    async def manage_op(
        actor: Annotated[Actor, Depends(require_staff_management_actor)],
    ) -> dict[str, str]:
        # Stand-in for the guard on ALL management endpoints (§33.4).
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @app.post("/test/admin-guard-op")
    async def admin_guard_op(
        actor: Annotated[Actor, Depends(require_admin_actor)],
    ) -> dict[str, str]:
        # Stand-in for the PR #2 hardening Admin-only surfaces (the
        # points redemption review decisions, the notifications failure
        # query) until scoped delegation lands.
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @app.post("/test/community-op")
    async def community_op(
        actor: Annotated[Actor, Depends(require_active_community_actor)],
    ) -> dict[str, str]:
        # Stand-in for the ordinary community participant surfaces
        # (comments, votes, reactions, reports).
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @app.exception_handler(TotpSetupRequiredError)
    async def _totp_setup_required(
        request: Request, exc: TotpSetupRequiredError
    ) -> JSONResponse:
        # Throwaway mapping: T9 adds the doc-first §29 code; the type is
        # this task's contract.
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "TOTP_SETUP_REQUIRED", "message": str(exc)}},
        )

    return app


def _api(db: AsyncSession, clock: Clock) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build_app(db, clock)),
        base_url="http://test",
    )


def _auth(tokens: SessionTokens) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.access_token}"}


@pytest.mark.integration
async def test_active_student_passes_the_active_guard(db_session: AsyncSession) -> None:
    clock = FrozenClock(_T0)
    codec = get_access_token_codec()
    user = await _seed_user(db_session, role=Role.STUDENT)
    _, tokens = await _open_session(db_session, user, codec=codec, clock=clock)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/student-op", headers=_auth(tokens))

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user.id)
    assert body["role"] == Role.STUDENT.value


@pytest.mark.integration
async def test_student_denied_admin_only_operation(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.STUDENT)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/admin-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
async def test_teacher_and_admin_allowed_on_staff_operation(
    db_session: AsyncSession, role: Role
) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=role)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/staff-op", headers=_auth(tokens))

    assert response.status_code == 200
    assert response.json()["role"] == role.value


@pytest.mark.integration
async def test_student_denied_staff_operation(db_session: AsyncSession) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.STUDENT)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/staff-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
@pytest.mark.parametrize("status", [UserStatus.SUSPENDED, UserStatus.BANNED])
async def test_suspended_student_with_valid_token_denied_state_change(
    db_session: AsyncSession, status: UserStatus
) -> None:
    # Spec §5.7: SUSPENDED/BANNED 不能领取、提交、新增社区内容. The token
    # itself is fresh and valid — only the account state denies — and the
    # answer is the stable ACCOUNT_NOT_ACTIVE, not a generic 500.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.STUDENT, status=status)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/student-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.ACCOUNT_NOT_ACTIVE


@pytest.mark.integration
@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer not-a-jwt", "Basic dXNlcjpwYXNz"],
)
async def test_missing_or_malformed_credentials_rejected(
    db_session: AsyncSession, authorization: str | None
) -> None:
    clock = FrozenClock(_T0)
    headers = {"Authorization": authorization} if authorization else None

    async with _api(db_session, clock) as client:
        response = await client.post("/test/student-op", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_token_signed_with_wrong_secret_rejected(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.STUDENT)
    forger = AccessTokenCodec(secret=_WRONG_SIGNING_SECRET, ttl_minutes=15)
    forged = forger.encode(
        user_id=user.id, session_id=uuid4(), role=Role.STUDENT.value, now=_T0
    )

    async with _api(db_session, clock) as client:
        response = await client.post(
            "/test/student-op", headers={"Authorization": f"Bearer {forged}"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_expired_access_token_rejected(db_session: AsyncSession) -> None:
    # Signature is valid; exp is in the past (TTL 15 minutes, minted 16
    # minutes ago): AUTHENTICATION_REQUIRED, the client's refresh signal.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.STUDENT)
    stale = get_access_token_codec().encode(
        user_id=user.id,
        session_id=uuid4(),
        role=Role.STUDENT.value,
        now=_T0 - timedelta(minutes=16),
    )

    async with _api(db_session, clock) as client:
        response = await client.post(
            "/test/student-op", headers={"Authorization": f"Bearer {stale}"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_revoked_session_access_token_rejected_before_expiry(
    db_session: AsyncSession,
) -> None:
    # revoke_all (the §5.6 password-change mechanism) must kill the access
    # token NOW — the JWT's own exp is 15 minutes away.
    clock = FrozenClock(_T0)
    codec = get_access_token_codec()
    user = await _seed_user(db_session, role=Role.STUDENT)
    _, tokens = await _open_session(db_session, user, codec=codec, clock=clock)
    await SessionService(clock=clock, access_codec=codec).revoke_all(
        db_session, user.id
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/student-op", headers=_auth(tokens))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_rotated_session_kills_the_old_access_token(
    db_session: AsyncSession,
) -> None:
    # Refresh rotation replaced the session: the old access token's sid
    # names a row with replaced_by set, so it must stop authenticating
    # immediately (§5.6: two live tokens must never coexist).
    clock = FrozenClock(_T0)
    codec = get_access_token_codec()
    user = await _seed_user(db_session, role=Role.STUDENT)
    service = SessionService(clock=clock, access_codec=codec)
    _, tokens = await _open_session(db_session, user, codec=codec, clock=clock)
    await service.rotate_refresh(db_session, tokens.refresh_token)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/student-op", headers=_auth(tokens))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_expired_session_row_rejects_fresh_access_token(
    db_session: AsyncSession,
) -> None:
    # Session expiry is checked per request against the business clock, so
    # a row that expired while its JWT is still time-valid authenticates
    # nothing.
    clock = FrozenClock(_T0)
    codec = get_access_token_codec()
    user = await _seed_user(db_session, role=Role.STUDENT)
    session_row, tokens = await _open_session(
        db_session, user, codec=codec, clock=clock
    )
    session_row.expires_at = clock.now() - timedelta(seconds=1)
    await db_session.flush()

    async with _api(db_session, clock) as client:
        response = await client.post("/test/student-op", headers=_auth(tokens))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_role_comes_from_the_user_row_not_the_jwt_claim(
    db_session: AsyncSession,
) -> None:
    # Stale-role defense: the JWT still claims ADMIN, but the row was
    # demoted to STUDENT after login — the fresh row wins, so the old
    # claim authorizes nothing.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.ADMIN)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    user.role = Role.STUDENT
    await db_session.flush()

    async with _api(db_session, clock) as client:
        response = await client.post("/test/admin-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
async def test_confirmed_staff_passes_the_management_guard(
    db_session: AsyncSession, role: Role
) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=role)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    await _seed_totp(db_session, user, confirmed=True)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/manage-op", headers=_auth(tokens))

    assert response.status_code == 200
    assert response.json()["role"] == role.value


@pytest.mark.integration
@pytest.mark.parametrize("with_credential_row", [True, False])
async def test_staff_without_confirmed_totp_forced_into_setup(
    db_session: AsyncSession, with_credential_row: bool
) -> None:
    # Spec §5.8 step 3 / §33.4: the pending staff token is a NORMAL JWT
    # (identity was proven), but management stays closed until TOTP is
    # confirmed — gated server-side, with a distinct setup-forcing error
    # whether setup never started or is unconfirmed.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.TEACHER)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    if with_credential_row:
        await _seed_totp(db_session, user, confirmed=False)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/manage-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TOTP_SETUP_REQUIRED"


@pytest.mark.integration
async def test_student_denied_the_management_guard(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.STUDENT)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/manage-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
async def test_suspended_teacher_denied_the_management_guard(
    db_session: AsyncSession,
) -> None:
    # 2FA is confirmed, so account state is the only failing condition:
    # the role check passes, then status denies.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.TEACHER, status=UserStatus.SUSPENDED)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    await _seed_totp(db_session, user, confirmed=True)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/manage-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.ACCOUNT_NOT_ACTIVE


# --- the Admin-only guard (PR #2: Admin-only until scoped delegation) -----------------


@pytest.mark.integration
async def test_confirmed_admin_passes_the_admin_guard(db_session: AsyncSession) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.ADMIN)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    await _seed_totp(db_session, user, confirmed=True)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/admin-guard-op", headers=_auth(tokens))

    assert response.status_code == 200
    assert response.json()["role"] == Role.ADMIN.value


@pytest.mark.integration
async def test_confirmed_teacher_denied_the_admin_guard(
    db_session: AsyncSession,
) -> None:
    """The hardening flip: a TEACHER holding everything the management
    guard demands (ACTIVE + confirmed TOTP) is still the wrong role for
    the Admin-only surfaces."""
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.TEACHER)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    await _seed_totp(db_session, user, confirmed=True)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/admin-guard-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
async def test_admin_without_confirmed_totp_forced_into_setup(
    db_session: AsyncSession,
) -> None:
    # The Admin role does not waive §5.8 step 3: the 2FA gate the
    # management surfaces carry is inherited whole.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.ADMIN)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/admin-guard-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TOTP_SETUP_REQUIRED"


# --- the community participant guard (PR #2 hardening: Student + Teacher) -------------


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.STUDENT, Role.TEACHER])
async def test_student_and_teacher_pass_the_community_guard(
    db_session: AsyncSession, role: Role
) -> None:
    """Spec §4.2 "除普通社区能力外，可：": the participant family is
    Student + Teacher, and NO TOTP is demanded — community participation
    is not management (§33.4's 2FA gate guards the staff surfaces), so a
    Teacher with a plain session participates."""
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=role)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/community-op", headers=_auth(tokens))

    assert response.status_code == 200
    assert response.json()["role"] == role.value


@pytest.mark.integration
async def test_admin_denied_the_community_guard(db_session: AsyncSession) -> None:
    """Admin is NOT a participant (the hardening ruling): even with a
    confirmed TOTP credential, the ordinary community surfaces refuse —
    Admin's community powers are the governance surfaces."""
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.ADMIN)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )
    await _seed_totp(db_session, user, confirmed=True)

    async with _api(db_session, clock) as client:
        response = await client.post("/test/community-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
async def test_suspended_participant_denied_the_community_guard(
    db_session: AsyncSession,
) -> None:
    # Spec §5.7: the state gate travels with the widened family — a
    # suspended account participates in nothing.
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session, role=Role.TEACHER, status=UserStatus.SUSPENDED)
    _, tokens = await _open_session(
        db_session, user, codec=get_access_token_codec(), clock=clock
    )

    async with _api(db_session, clock) as client:
        response = await client.post("/test/community-op", headers=_auth(tokens))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.ACCOUNT_NOT_ACTIVE
