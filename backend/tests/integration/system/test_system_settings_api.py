# backend/tests/integration/system/test_system_settings_api.py
"""The admin system-settings API over real PostgreSQL (PR #2 hardening
step 8: the audited, admin-configurable CURRENT_ACADEMIC_TERM).

Drives the real app (``create_app()`` — the system router mounted under
/api/v1) with the rollback-harness session:

- ``GET /admin/settings/current-academic-term``: the effective term —
  the deployment seed ("2026-fall") while no row exists, the stored row
  once an admin has set one;
- ``PUT /admin/settings/current-academic-term``: stores the term and
  commits ONE ``SYSTEM_SETTING_UPDATED`` audit row in the same
  transaction (actor/target, the value migration on the §30 snapshot
  pair — ``before_snapshot.value`` is the previous value, ``None`` on
  the first write), with the row's ``updated_by_user_id`` naming the
  actor;
- the shared term validation semantics: the value is stored stripped,
  and blank/empty/over-length values answer the §29 VALIDATION_ERROR
  envelope (422), never a 500;
- the guard: ``require_admin_actor`` — an ACTIVE+TOTP Teacher is 403
  PERMISSION_DENIED on both surfaces (the redemption-review family's
  Admin-only narrowing), an unauthenticated call is 401.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.system.models import SystemSetting
from app.modules.system.service import CURRENT_ACADEMIC_TERM, SYSTEM_SETTING_UPDATED

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_TERM_PATH = "/api/v1/admin/settings/current-academic-term"
# Settings.current_academic_term's dev default — the integration gate
# environment sets no CURRENT_ACADEMIC_TERM, so the seed is this.
_SEED_TERM = "2026-fall"


# --- fixtures -----------------------------------------------------------------------


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def api_app(db_session: AsyncSession, api_clock: FrozenClock) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


# --- seeding helpers ----------------------------------------------------------------


async def _seed_user(db: AsyncSession, *, username: str, role: Role) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _headers(db: AsyncSession, clock: FrozenClock, user: User) -> dict[str, str]:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def _management_account(
    db: AsyncSession, clock: FrozenClock, *, username: str, role: Role
) -> tuple[User, dict[str, str]]:
    """A staff account able to pass ``require_admin_actor``'s shared
    checks (role + ACTIVE + a confirmed TOTP credential row) — with
    ``role=ADMIN`` it passes; with ``role=TEACHER`` it fails only the
    role check, which is exactly the boundary under test."""
    user = await _seed_user(db, username=username, role=role)
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return user, await _headers(db, clock, user)


async def _admin(db: AsyncSession, clock: FrozenClock) -> tuple[User, dict[str, str]]:
    return await _management_account(
        db, clock, username="syscfg-admin-0001", role=Role.ADMIN
    )


async def _setting_row(db: AsyncSession) -> SystemSetting | None:
    """The CURRENT_ACADEMIC_TERM row, refreshed so server-generated
    columns (``updated_at``) are loaded — the identity map may hold the
    instance the write path inserted without its defaults."""
    row = await db.scalar(
        select(SystemSetting).where(SystemSetting.key == CURRENT_ACADEMIC_TERM)
    )
    if row is not None:
        await db.refresh(row)
    return row


async def _term_audit_rows(db: AsyncSession) -> list[AuditLog]:
    result = await db.scalars(
        select(AuditLog)
        .where(AuditLog.target_id == CURRENT_ACADEMIC_TERM)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list(result)


# --- GET: the effective term ---------------------------------------------------------


async def test_get_returns_the_seed_term_before_any_configuration(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
) -> None:
    # No row exists: the deployment seed is the answer (G7: the env var
    # is the initial seed until an admin configures the audited row).
    _, headers = await _admin(db_session, api_clock)
    assert await _setting_row(db_session) is None

    response = await client.get(_TERM_PATH, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"value": _SEED_TERM}


# --- PUT: value + audit row in one transaction ----------------------------------------


async def test_put_stores_the_term_writes_audit_and_get_returns_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
) -> None:
    admin, headers = await _admin(db_session, api_clock)

    response = await client.put(
        _TERM_PATH, json={"value": "2027-spring"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"value": "2027-spring"}

    row = await _setting_row(db_session)
    assert row is not None
    assert row.value == "2027-spring"
    assert row.updated_by_user_id == admin.id
    assert row.updated_at is not None

    audits = await _term_audit_rows(db_session)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.actor_user_id == admin.id
    assert audit.actor_role == Role.ADMIN.value
    assert audit.action == SYSTEM_SETTING_UPDATED
    assert audit.target_type == "system_setting"
    assert audit.target_id == CURRENT_ACADEMIC_TERM
    # 0016: the value migration rides the §30 snapshot pair.
    assert audit.details is None
    assert audit.before_snapshot == {"value": None}
    assert audit.after_snapshot == {"value": "2027-spring"}

    # GET now answers the configured row, not the seed (G7: the row is
    # the fact).
    get_response = await client.get(_TERM_PATH, headers=headers)
    assert get_response.status_code == 200
    assert get_response.json() == {"value": "2027-spring"}


async def test_second_put_audits_the_previous_value(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
) -> None:
    admin, headers = await _admin(db_session, api_clock)
    await client.put(_TERM_PATH, json={"value": "2027-spring"}, headers=headers)
    await client.put(_TERM_PATH, json={"value": "2027-summer"}, headers=headers)

    row = await _setting_row(db_session)
    assert row is not None and row.value == "2027-summer"
    audits = await _term_audit_rows(db_session)
    # Both writes land inside the rollback harness's ONE outer
    # transaction, so ``created_at`` (transaction time) ties and no
    # insertion order is observable — compare per written value, not
    # row order.
    assert {
        audit.after_snapshot["value"]: (
            audit.before_snapshot,
            audit.after_snapshot,
        )
        for audit in audits
    } == {
        "2027-spring": ({"value": None}, {"value": "2027-spring"}),
        "2027-summer": ({"value": "2027-spring"}, {"value": "2027-summer"}),
    }
    assert all(audit.actor_user_id == admin.id for audit in audits)


# --- PUT: the shared validation semantics ---------------------------------------------


async def test_put_stores_the_stripped_value(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
) -> None:
    _, headers = await _admin(db_session, api_clock)

    response = await client.put(
        _TERM_PATH, json={"value": " 2028-spring "}, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"value": "2028-spring"}
    row = await _setting_row(db_session)
    assert row is not None and row.value == "2028-spring"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65])
async def test_put_rejects_unusable_values_with_validation_error(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    bad: str,
) -> None:
    """Empty/blank/over-length values answer the §29 VALIDATION_ERROR
    envelope (422): empty and over-length at the request schema, blank
    at the service gate — one code for the whole family."""
    _, headers = await _admin(db_session, api_clock)

    response = await client.put(_TERM_PATH, json={"value": bad}, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    # Nothing was stored and nothing was audited: a rejected write is
    # not a configuration change.
    assert await _setting_row(db_session) is None
    assert await _term_audit_rows(db_session) == []


# --- the guard ------------------------------------------------------------------------


async def test_teacher_is_forbidden_on_both_surfaces(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
) -> None:
    """An ACTIVE+TOTP Teacher fails only the role check — the same
    Admin-only narrowing the redemption review family carries (PR #2
    hardening ruling), and the rejection writes nothing."""
    _, teacher_headers = await _management_account(
        db_session, api_clock, username="syscfg-teacher-0002", role=Role.TEACHER
    )

    get_response = await client.get(_TERM_PATH, headers=teacher_headers)
    put_response = await client.put(
        _TERM_PATH, json={"value": "2027-spring"}, headers=teacher_headers
    )

    for response in (get_response, put_response):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert await _setting_row(db_session) is None
    assert await _term_audit_rows(db_session) == []


async def test_unauthenticated_call_is_authentication_required(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(_TERM_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
