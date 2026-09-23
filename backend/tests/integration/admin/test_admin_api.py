# backend/tests/integration/admin/test_admin_api.py
"""The Plan 08 T9 admin operational APIs over real PostgreSQL: the
direct-URL privilege matrix (plan step 1), the store-backed
management-network wiring, the settings cross-key ruling, and the
assembled surfaces' behavior through the real app (``create_app()``)
with the rollback-harness session.

Coverage map:

- **Privilege matrix (plan step 1, G10):** every endpoint this wave
  mounts answers 403 ``PERMISSION_DENIED`` for an ACTIVE+TOTP Teacher
  and for a Student calling the admin URLs directly; the Admin passes
  the guards (the response may still be a typed 404/409/422 — the
  point is the GUARD verdict, not the handler outcome).
- **Management-network wiring (the W4 footgun closure):** the guard
  resolves its policy from the settings STORE per request — a stored
  ``MANAGEMENT_NETWORK_ENABLED=true`` + CIDR row refuses the default
  127.0.0.1 transport peer (no env var is set in this environment, so
  the refusal proves store-first resolution), while a peer inside the
  allowlist passes; the network refusal is 403 ``PERMISSION_DENIED``
  even for the Admin.
- **Cross-key ruling:** enabling the policy with an empty effective
  CIDR list is the typed 422 at WRITE time (both directions of the
  pair), and nothing is stored for a refused write.
- **PR #5 fix A:** the settings surface is EXEMPT from the network
  guard (the self-repair face — reachable while the policy refuses the
  peer, and the repair closes the loop), and an UNLOADABLE stored pair
  (historical residue, seeded by direct row inserts) fails closed as
  the 403 envelope, never a 500. The manual points adjustment is
  idempotent by the body's ``operation_id`` (replay returns the
  original entry; a reused id under a different decision is the typed
  409; a missing id is the 422).
- **The assembled surfaces:** whitelist preview -> confirm -> toggle
  with audit rows; the audit-log search with filters and pagination;
  account status transitions with the typed 409 ``CONFLICT``; reward
  catalogue create/patch/disable; the review-grant lifecycle; the
  manual points adjustment through the ledger; template create/patch/
  toggle; the repair commands' typed 404s; staff invitation issuance
  with its rate-limit bucket.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import StaffInvitation, TotpCredential, User
from app.modules.identity.providers import get_rate_limiter
from app.modules.identity.session_service import SessionService
from app.modules.points.enums import LedgerType
from app.modules.points.models import PointsLedger, PointWallet
from app.modules.system.models import SystemSetting
from app.modules.system.service import (
    MANAGEMENT_NETWORK_CIDRS,
    MANAGEMENT_NETWORK_ENABLED,
    MANAGEMENT_NETWORK_POLICY_LOCK,
    SYSTEM_SETTING_REGISTRY,
    SystemSettingService,
)
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_API = "/api/v1"


# --- fixtures -----------------------------------------------------------------------


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    fake_limiter: FakeRateLimiter,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


@pytest_asyncio.fixture
async def in_network_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose transport peer sits INSIDE the 10.0.0.0/8 allowlist
    the network tests seed — for tests that enable the stored policy and
    then still need to reach the admin surface."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, client=("10.1.2.3", 123)),
        base_url="http://test",
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
    db: AsyncSession,
    clock: FrozenClock,
    *,
    username: str,
    role: Role = Role.TEACHER,
) -> tuple[User, dict[str, str]]:
    """A staff account able to pass the §33.4 management gate (role +
    ACTIVE + a confirmed TOTP credential row); ``role=ADMIN`` yields an
    account that also passes ``require_admin_actor``."""
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


@pytest_asyncio.fixture
async def admin_world(
    db_session: AsyncSession, api_clock: FrozenClock
) -> dict[str, Any]:
    """The privilege-matrix world: an Admin, an ACTIVE+TOTP Teacher, and
    a Student, each with live bearer headers."""
    admin, admin_headers = await _management_account(
        db_session, api_clock, username="matrix-admin-0001", role=Role.ADMIN
    )
    teacher, teacher_headers = await _management_account(
        db_session, api_clock, username="matrix-teacher-0002"
    )
    student = await _seed_user(
        db_session, username="matrix-student-0003", role=Role.STUDENT
    )
    student_headers = await _headers(db_session, api_clock, student)
    return {
        "db": db_session,
        "clock": api_clock,
        "admin": admin,
        "admin_headers": admin_headers,
        "teacher": teacher,
        "teacher_headers": teacher_headers,
        "student": student,
        "student_headers": student_headers,
    }


def _endpoint_table(teacher_id: UUID, student_id: UUID, user_id: UUID) -> list[Any]:
    """(method, path-with-query, json body) for EVERY endpoint this wave
    mounts — the matrix's exhaustive surface list."""
    return [
        ("GET", f"{_API}/admin/audit-logs", None),
        (
            "POST",
            f"{_API}/admin/repairs/release-occupied-assignment",
            {"assignment_id": str(uuid4()), "reason": "dangling after crash"},
        ),
        (
            "POST",
            f"{_API}/admin/repairs/force-fail-delivery",
            {"delivery_id": str(uuid4()), "reason": "wedged sender"},
        ),
        ("GET", f"{_API}/admin/whitelist", None),
        ("POST", f"{_API}/admin/whitelist/preview", {"content": "30260001\n"}),
        (
            "POST",
            f"{_API}/admin/whitelist/confirm",
            {
                "confirm_token": "0" * 64,
                "enable": True,
                "student_numbers": ["30260001"],
            },
        ),
        (
            "PATCH",
            f"{_API}/admin/whitelist/30260001",
            {"enabled": False, "reason": "毕业离校"},
        ),
        ("GET", f"{_API}/admin/users", None),
        ("POST", f"{_API}/admin/users/{user_id}/suspend", {"reason": "调查中"}),
        ("POST", f"{_API}/admin/users/{user_id}/ban", {"reason": "舞弊"}),
        ("POST", f"{_API}/admin/users/{user_id}/reactivate", {"reason": "已解除"}),
        (
            "POST",
            f"{_API}/staff/invitations",
            {"email": "new-teacher@pku.edu.cn", "role": "TEACHER"},
        ),
        ("POST", f"{_API}/admin/rewards", {"name": "矩阵奖品", "point_cost": 100}),
        ("GET", f"{_API}/admin/rewards", None),
        ("PATCH", f"{_API}/admin/rewards/{uuid4()}", {"point_cost": 200}),
        ("POST", f"{_API}/admin/rewards/{uuid4()}/disable", {"reason": "库存清零"}),
        (
            "POST",
            f"{_API}/admin/reward-review-grants",
            {"teacher_id": str(teacher_id), "reason": "教务授权"},
        ),
        ("GET", f"{_API}/admin/reward-review-grants", None),
        ("DELETE", f"{_API}/admin/reward-review-grants/{teacher_id}?reason=收回", None),
        (
            "POST",
            f"{_API}/admin/users/{student_id}/points-adjustment",
            {
                "amount": 10,
                "reason": "人工补正",
                "operation_id": str(uuid4()),
            },
        ),
        ("GET", f"{_API}/admin/settings", None),
        ("PUT", f"{_API}/admin/settings/emoji-whitelist", {"value": ["🎓"]}),
        ("PUT", f"{_API}/admin/settings/abandon-daily-limit", {"value": 2}),
        (
            "PUT",
            f"{_API}/admin/settings/management-network-enabled",
            {"value": False},
        ),
        (
            "PUT",
            f"{_API}/admin/settings/management-network-cidrs",
            {"value": ["10.0.0.0/8"]},
        ),
        (
            "POST",
            f"{_API}/admin/notification-templates",
            {
                "event_type": "ACCOUNT_SECURITY",
                "channel": "IN_APP",
                "title": "账号安全",
                "template_body": "你好 {nickname}",
            },
        ),
        ("GET", f"{_API}/admin/notification-templates", None),
        (
            "PATCH",
            f"{_API}/admin/notification-templates/{uuid4()}",
            {"title": "账号安全", "template_body": "你好 {nickname}"},
        ),
        ("POST", f"{_API}/admin/notification-templates/{uuid4()}/enable", None),
        ("POST", f"{_API}/admin/notification-templates/{uuid4()}/disable", None),
    ]


async def _request(
    client: httpx.AsyncClient, method: str, path: str, json_body: Any, headers: Any
) -> httpx.Response:
    if method == "GET":
        return await client.get(path, headers=headers)
    if method == "DELETE":
        return await client.delete(path, headers=headers)
    if method == "PATCH":
        return await client.patch(path, json=json_body, headers=headers)
    if method == "PUT":
        return await client.put(path, json=json_body, headers=headers)
    return await client.post(path, json=json_body, headers=headers)


# --- the privilege matrix (plan step 1) ----------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _endpoint_table(UUID(int=1), UUID(int=2), UUID(int=3)),
)
async def test_teacher_direct_call_is_permission_denied(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
    method: str,
    path: str,
    body: Any,
) -> None:
    """An ACTIVE+TOTP Teacher gains nothing by calling the Admin URLs
    directly (plan review focus 2 / G10)."""
    response = await _request(
        client, method, path, body, admin_world["teacher_headers"]
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _endpoint_table(UUID(int=1), UUID(int=2), UUID(int=3)),
)
async def test_student_direct_call_is_permission_denied(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
    method: str,
    path: str,
    body: Any,
) -> None:
    response = await _request(
        client, method, path, body, admin_world["student_headers"]
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _endpoint_table(UUID(int=1), UUID(int=2), UUID(int=3)),
)
async def test_admin_passes_the_guards(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
    method: str,
    path: str,
    body: Any,
) -> None:
    """The Admin passes both guards on every endpoint: the response is a
    real handler outcome (200, or a typed 404/409/422), never the 401/403
    a wrong-role or wrong-network caller would see."""
    response = await _request(client, method, path, body, admin_world["admin_headers"])
    assert response.status_code not in (401, 403), response.text


async def test_unauthenticated_admin_call_is_401(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(f"{_API}/admin/users")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED


# --- the store-backed management-network guard (W4 footgun closure) -------------------


async def _seed_policy(
    db: AsyncSession, actor_user_id: Any, *, cidrs: list[str], enabled: bool
) -> None:
    """Seed a LEGAL policy pair through the pair's one write path (PR #5
    fix A): CIDRs first (the env flag is disabled in this environment,
    so each intermediate pair stays loadable), then the flag."""
    from app.modules.identity.events import Actor

    service = SystemSettingService()
    await service.set_management_network_policy(
        db, actor=Actor(user_id=actor_user_id, role=Role.ADMIN), cidrs=cidrs
    )
    await service.set_management_network_policy(
        db, actor=Actor(user_id=actor_user_id, role=Role.ADMIN), enabled=enabled
    )


async def _seed_residue_setting_rows(
    db: AsyncSession, actor_user_id: Any, *, enabled: str, cidrs: str
) -> None:
    """Insert the two policy rows DIRECTLY, bypassing the write path —
    the historical-residue shape the aggregate lock made unwritable but
    the loader must still survive (fail closed, never 500)."""
    for key, value in (
        (MANAGEMENT_NETWORK_ENABLED, enabled),
        (MANAGEMENT_NETWORK_CIDRS, cidrs),
    ):
        db.add(SystemSetting(key=key, value=value, updated_by_user_id=actor_user_id))
    await db.flush()


async def test_enabled_network_policy_refuses_out_of_network_admin(
    api_app: FastAPI,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    """Store-first resolution: no env var names a policy here, so a
    stored enabled=true + 10.0.0.0/8 row is what the guard resolves —
    the default 127.0.0.1 transport peer is OUT of network and the
    Admin gets the network 403, while an in-network peer passes."""
    await _seed_policy(
        db_session, admin_world["admin"].id, cidrs=["10.0.0.0/8"], enabled=True
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, client=("203.0.113.9", 123)),
        base_url="http://test",
    ) as outside:
        response = await outside.get(
            f"{_API}/admin/users", headers=admin_world["admin_headers"]
        )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED
    assert response.json()["error"]["message"] == "当前网络不允许访问管理功能"

    # The default transport peer (127.0.0.1) is equally out of network.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as local:
        response = await local.get(
            f"{_API}/admin/users", headers=admin_world["admin_headers"]
        )
    assert response.status_code == 403

    # An in-network peer passes the guard and reaches the handler.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, client=("10.1.2.3", 123)),
        base_url="http://test",
    ) as inside:
        response = await inside.get(
            f"{_API}/admin/users", headers=admin_world["admin_headers"]
        )
    assert response.status_code == 200, response.text


async def test_disabled_network_policy_passes_through(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    await _seed_policy(
        db_session, admin_world["admin"].id, cidrs=["10.0.0.0/8"], enabled=False
    )
    response = await client.get(
        f"{_API}/admin/users", headers=admin_world["admin_headers"]
    )
    assert response.status_code == 200


# --- the settings surface: the policy's self-repair face (PR #5 fix A) ----------------


async def test_settings_surface_is_exempt_from_the_network_guard(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    """A policy that refuses this client's network still answers the
    settings surface (the terminal-review ruling: the Admin must reach
    "the API needed to repair the setting") — while the GUARDED admin
    family keeps refusing the same peer. The §33.4 Admin gate still
    applies to the settings routes (the teacher matrix above pins the
    403 on every settings URL)."""
    # 203.0.113.0/24 excludes the default 127.0.0.1 transport peer.
    await _seed_policy(
        db_session, admin_world["admin"].id, cidrs=["203.0.113.0/24"], enabled=True
    )

    guarded = await client.get(
        f"{_API}/admin/users", headers=admin_world["admin_headers"]
    )
    assert guarded.status_code == 403

    settings = await client.get(
        f"{_API}/admin/settings", headers=admin_world["admin_headers"]
    )
    assert settings.status_code == 200, settings.text
    # The repair itself: readmit this peer's network through the exempt
    # surface, then the guarded family admits it again — the self-repair
    # loop closes end to end.
    repair = await client.put(
        f"{_API}/admin/settings/management-network-cidrs",
        json={"value": ["127.0.0.0/8", "203.0.113.0/24"]},
        headers=admin_world["admin_headers"],
    )
    assert repair.status_code == 200, repair.text
    recovered = await client.get(
        f"{_API}/admin/users", headers=admin_world["admin_headers"]
    )
    assert recovered.status_code == 200, recovered.text


async def test_unloadable_residue_fails_closed_but_settings_stay_repairable(
    api_app: FastAPI,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    """Historical residue (an unloadable pair stored before the
    aggregate lock — seeded by direct row inserts): the guard FAILS
    CLOSED — the 403 PERMISSION_DENIED envelope to every caller,
    in-network included, never a 500 — while the exempt settings
    surface stays reachable and can repair the pair (PR #5 fix A's
    two halves in one loop)."""
    await _seed_residue_setting_rows(
        db_session, admin_world["admin"].id, enabled="true", cidrs=""
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, client=("10.1.2.3", 123)),
        base_url="http://test",
    ) as inside:
        guarded = await inside.get(
            f"{_API}/admin/users", headers=admin_world["admin_headers"]
        )
    assert guarded.status_code == 403, guarded.text
    assert guarded.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED
    assert "不可加载" in guarded.json()["error"]["message"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as local:
        settings = await local.get(
            f"{_API}/admin/settings", headers=admin_world["admin_headers"]
        )
        assert settings.status_code == 200, settings.text
        # The repair goes through the aggregate write path and is judged
        # against the residue's own values (enabled=true + the new list).
        repair = await local.put(
            f"{_API}/admin/settings/management-network-cidrs",
            json={"value": ["10.0.0.0/8"]},
            headers=admin_world["admin_headers"],
        )
        assert repair.status_code == 200, repair.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, client=("10.1.2.3", 123)),
        base_url="http://test",
    ) as inside_after:
        recovered = await inside_after.get(
            f"{_API}/admin/users", headers=admin_world["admin_headers"]
        )
    assert recovered.status_code == 200, recovered.text


# --- the settings cross-key ruling ----------------------------------------------------


async def test_settings_face_stays_reachable_while_the_policy_lock_is_held(
    api_app: FastAPI,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    admin_world: dict[str, Any],
) -> None:
    """PR #5 fix A: while another real connection holds the policy's
    aggregate advisory lock, the settings face stays REACHABLE — its
    reads take no policy lock (GET answers 200) — while a policy WRITE
    serializes behind the holder and completes only after it releases
    (observed without cancelling the request: the write task stays
    pending until the holder's transaction ends)."""
    holder = await db_engine.connect()
    try:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:lock)"),
            {"lock": MANAGEMENT_NETWORK_POLICY_LOCK},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as local:
            reachable = await local.get(
                f"{_API}/admin/settings", headers=admin_world["admin_headers"]
            )
            assert reachable.status_code == 200, reachable.text

            write = asyncio.create_task(
                local.put(
                    f"{_API}/admin/settings/management-network-enabled",
                    json={"value": False},
                    headers=admin_world["admin_headers"],
                )
            )
            done, pending = await asyncio.wait({write}, timeout=0.5)
            assert not done, "the policy write completed while the lock was held"
            assert pending == {write}

            await holder.rollback()  # ends the holder's transaction: release
            response = await write
            assert response.status_code == 200, response.text
    finally:
        await holder.close()


async def test_enabling_network_policy_without_cidrs_is_refused_at_write_time(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    """enabled=true with an empty effective CIDR list is the typed 422
    BEFORE any write (no runtime 500 can be stored)."""
    response = await client.put(
        f"{_API}/admin/settings/management-network-enabled",
        json={"value": True},
        headers=admin_world["admin_headers"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR
    # Nothing was stored: the key still has no row.
    assert (
        await db_session.scalar(
            select(SystemSetting).where(SystemSetting.key == MANAGEMENT_NETWORK_ENABLED)
        )
        is None
    )


async def test_emptying_cidrs_while_enabled_is_refused_at_write_time(
    in_network_client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    await _seed_policy(
        db_session, admin_world["admin"].id, cidrs=["10.0.0.0/8"], enabled=True
    )

    response = await in_network_client.put(
        f"{_API}/admin/settings/management-network-cidrs",
        json={"value": []},
        headers=admin_world["admin_headers"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR
    # The stored CIDR row is untouched.
    row = await db_session.scalar(
        select(SystemSetting).where(SystemSetting.key == MANAGEMENT_NETWORK_CIDRS)
    )
    assert row is not None and row.value == "10.0.0.0/8"


async def test_settings_write_and_list_round_trip(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
) -> None:
    """The full-key surface: valid writes store canonical values with
    per-key versions, and the listing answers every registered key."""
    response = await client.put(
        f"{_API}/admin/settings/abandon-daily-limit",
        json={"value": 2},
        headers=admin_world["admin_headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"] == "ABANDON_DAILY_LIMIT"
    assert body["value"] == "2"
    assert body["version"] == 1

    listed = await client.get(
        f"{_API}/admin/settings", headers=admin_world["admin_headers"]
    )
    assert listed.status_code == 200
    items = {item["key"]: item for item in listed.json()["items"]}
    assert set(items) == set(SYSTEM_SETTING_REGISTRY)
    assert items["ABANDON_DAILY_LIMIT"]["value"] == "2"
    assert items["ABANDON_DAILY_LIMIT"]["version"] == 1
    # No row yet for the untouched keys: null value/version, not a seed.
    assert items["EMOJI_WHITELIST"]["value"] is None
    assert items["MANAGEMENT_NETWORK_ENABLED"]["version"] is None

    # A registry-invalid value keeps the typed 422 shape.
    bad = await client.put(
        f"{_API}/admin/settings/abandon-daily-limit",
        json={"value": -1},
        headers=admin_world["admin_headers"],
    )
    assert bad.status_code == 422


# --- whitelist administration ---------------------------------------------------------


async def test_whitelist_preview_confirm_toggle_round_trip(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    preview = await client.post(
        f"{_API}/admin/whitelist/preview",
        json={"content": "40260001\n40260002\n４０２６０００３\n"},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total_rows"] == 3
    assert body["counts"]["importable"] == 2
    assert body["counts"]["full_width_digits"] == 1
    assert body["importable"] == ["40260001", "40260002"]

    confirmed = await client.post(
        f"{_API}/admin/whitelist/confirm",
        json={
            "confirm_token": body["confirm_token"],
            "enable": True,
            "student_numbers": body["importable"],
        },
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"created": 2, "enable": True}
    assert (
        await _audit_count(
            db_session, admin_world["admin"].id, "WHITELIST_IMPORT_CONFIRMED"
        )
        == 1
    )

    listed = await client.get(f"{_API}/admin/whitelist", headers=headers)
    assert listed.status_code == 200
    numbers = {item["student_number"] for item in listed.json()["items"]}
    assert {"40260001", "40260002"} <= numbers

    toggled = await client.patch(
        f"{_API}/admin/whitelist/40260001",
        json={"enabled": False, "reason": "毕业离校"},
        headers=headers,
    )
    assert toggled.status_code == 200
    assert toggled.json() == {"student_number": "40260001", "toggled": True}
    assert (
        await _audit_count(
            db_session, admin_world["admin"].id, "WHITELIST_ENTRY_TOGGLED"
        )
        == 1
    )

    # A confirm whose payload was never previewed is the typed 409
    # CONFLICT (the replay check).
    mismatch = await client.post(
        f"{_API}/admin/whitelist/confirm",
        json={
            "confirm_token": "0" * 64,
            "enable": True,
            "student_numbers": ["40260009"],
        },
        headers=headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == ErrorCode.CONFLICT


# --- audit search ---------------------------------------------------------------------


async def test_audit_log_search_filters_and_paginates(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    # Seed two distinct audit actions through the real write paths. A
    # third harness row makes the page assertions self-sufficient: the
    # shared database's COMMITTED audit rows are also visible to the
    # search (read-committed), and a scrubbed database must not fail
    # the ">= 3" floor.
    put = await client.put(
        f"{_API}/admin/settings/abandon-daily-limit",
        json={"value": 1},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    await client.post(
        f"{_API}/admin/whitelist/preview",
        json={"content": "50260001\n"},
        headers=headers,
    )
    await _append_audit_row(db_session, admin_world["admin"], "PROBE_ACTION")
    # A DISTINCT action name: the actor+action filter below pins exactly
    # the first probe row.
    await _append_audit_row(db_session, admin_world["admin"], "PROBE_ACTION_2")

    unfiltered = await client.get(f"{_API}/admin/audit-logs", headers=headers)
    assert unfiltered.status_code == 200
    page = unfiltered.json()
    assert page["limit"] == 20 and page["offset"] == 0
    assert page["total"] >= 3
    # Newest first.
    created = [item["created_at"] for item in page["items"]]
    assert created == sorted(created, reverse=True)

    by_action = await client.get(
        f"{_API}/admin/audit-logs",
        params={
            "action": "SYSTEM_SETTING_UPDATED",
            "actor_user_id": str(admin_world["admin"].id),
        },
        headers=headers,
    )
    assert by_action.status_code == 200
    actions = {item["action"] for item in by_action.json()["items"]}
    assert actions == {"SYSTEM_SETTING_UPDATED"}
    # The filter kept exactly the write this test made (target = the key).
    assert {item["target_id"] for item in by_action.json()["items"]} == {
        "ABANDON_DAILY_LIMIT"
    }

    by_target = await client.get(
        f"{_API}/admin/audit-logs",
        params={"target_type": "system_setting", "limit": 1},
        headers=headers,
    )
    assert by_target.status_code == 200
    assert len(by_target.json()["items"]) == 1
    assert by_target.json()["limit"] == 1

    by_actor = await client.get(
        f"{_API}/admin/audit-logs",
        params={
            "actor_user_id": str(admin_world["admin"].id),
            "action": "PROBE_ACTION",
        },
        headers=headers,
    )
    assert by_actor.status_code == 200
    assert [item["action"] for item in by_actor.json()["items"]] == ["PROBE_ACTION"]

    # The DTO carries the stored correlation pair (this row has none —
    # it was written by the test harness, not a request).
    probe = by_actor.json()["items"][0]
    assert probe["actor_role"] == "ADMIN"
    assert set(probe) == {
        "id",
        "actor_user_id",
        "actor_role",
        "action",
        "target_type",
        "target_id",
        "reason",
        "details",
        "before_snapshot",
        "after_snapshot",
        "ip_address",
        "request_id",
        "created_at",
    }

    # Over-cap limits are the framework 422 (le=50), never an unbounded read.
    over = await client.get(
        f"{_API}/admin/audit-logs", params={"limit": 51}, headers=headers
    )
    assert over.status_code == 422


# --- account status governance --------------------------------------------------------


async def test_account_suspend_then_conflict_then_reactivate(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    student = admin_world["student"]

    suspended = await client.post(
        f"{_API}/admin/users/{student.id}/suspend",
        json={"reason": "调查中"},
        headers=headers,
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "SUSPENDED"
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "USER_SUSPENDED") == 1
    )

    # SUSPENDED -> SUSPENDED is outside spec §5.7's table: typed 409
    # CONFLICT with the observed/requested pair.
    replay = await client.post(
        f"{_API}/admin/users/{student.id}/suspend",
        json={"reason": "再次停用"},
        headers=headers,
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == ErrorCode.CONFLICT
    assert replay.json()["error"]["details"]["from"] == "SUSPENDED"

    reactivated = await client.post(
        f"{_API}/admin/users/{student.id}/reactivate",
        json={"reason": "已解除"},
        headers=headers,
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["status"] == "ACTIVE"

    unknown = await client.post(
        f"{_API}/admin/users/{uuid4()}/suspend",
        json={"reason": "不存在的账号"},
        headers=headers,
    )
    assert unknown.status_code == 404


async def test_user_directory_lists_and_filters(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    listed = await client.get(f"{_API}/admin/users", headers=headers)
    assert listed.status_code == 200
    page = listed.json()
    usernames = {item["username"] for item in page["items"]}
    assert {
        "matrix-admin-0001",
        "matrix-teacher-0002",
        "matrix-student-0003",
    } <= usernames
    # G11: governance facts only — no contact columns exist to leak.
    assert set(page["items"][0]) == {
        "id",
        "username",
        "nickname",
        "role",
        "status",
        "created_at",
    }

    teachers = await client.get(
        f"{_API}/admin/users", params={"role": "TEACHER"}, headers=headers
    )
    assert teachers.status_code == 200
    assert {item["role"] for item in teachers.json()["items"]} == {"TEACHER"}


# --- reward catalogue + review grants -------------------------------------------------


async def test_reward_catalogue_create_patch_disable(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    created = await client.post(
        f"{_API}/admin/rewards",
        json={"name": "平时成绩 +1", "point_cost": 400, "stock": 5},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]
    assert created.json()["enabled"] is True
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "REWARD_ITEM_CREATED")
        == 1
    )

    patched = await client.patch(
        f"{_API}/admin/rewards/{item_id}",
        json={"point_cost": 500, "stock": None},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["point_cost"] == 500
    assert patched.json()["stock"] is None  # explicit null cleared the bound
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "REWARD_ITEM_UPDATED")
        == 1
    )

    disabled = await client.post(
        f"{_API}/admin/rewards/{item_id}/disable",
        json={"reason": "库存清零"},
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "REWARD_ITEM_DISABLED")
        == 1
    )


async def test_admin_reward_listing_includes_disabled_and_paginates(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
) -> None:
    """T10's query gap-fill: ``GET /admin/rewards`` is the MANAGEMENT
    catalogue — disabled rows included (the student ``GET /rewards``
    stays the enabled-only shelf), offset-paginated with the family
    cap. Assertions are presence-based: the shared test database may
    carry catalogue rows from other suites' committed runs."""
    headers = admin_world["admin_headers"]
    suffix = uuid4().hex[:6]
    kept = await client.post(
        f"{_API}/admin/rewards",
        json={"name": f"列表在架{suffix}", "point_cost": 100},
        headers=headers,
    )
    assert kept.status_code == 200, kept.text
    dropped = await client.post(
        f"{_API}/admin/rewards",
        json={"name": f"列表下架{suffix}", "point_cost": 200},
        headers=headers,
    )
    assert dropped.status_code == 200, dropped.text
    disabled = await client.post(
        f"{_API}/admin/rewards/{dropped.json()['id']}/disable",
        json={"reason": "管理列表用例下架"},
        headers=headers,
    )
    assert disabled.status_code == 200

    listed = await client.get(f"{_API}/admin/rewards", headers=headers)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["limit"] == 20 and page["offset"] == 0
    assert page["total"] >= 2
    by_name = {item["name"]: item for item in page["items"]}
    assert by_name[f"列表在架{suffix}"]["enabled"] is True
    # The disabled row is exactly what distinguishes this listing from
    # the student shelf.
    assert by_name[f"列表下架{suffix}"]["enabled"] is False
    # The admin row shape: every business field (the student listing
    # omits enabled/fulfillment_instructions).
    assert set(page["items"][0]) == {
        "id",
        "name",
        "description",
        "point_cost",
        "stock",
        "per_user_term_limit",
        "available_from",
        "available_until",
        "enabled",
        "requires_manual_review",
        "fulfillment_instructions",
    }

    paged = await client.get(
        f"{_API}/admin/rewards", params={"limit": 1, "offset": 1}, headers=headers
    )
    assert paged.status_code == 200
    assert len(paged.json()["items"]) == 1
    assert paged.json()["limit"] == 1 and paged.json()["offset"] == 1
    assert paged.json()["total"] == page["total"]

    # The family cap is the framework 422 (le=50), never an unbounded read.
    over = await client.get(
        f"{_API}/admin/rewards", params={"limit": 51}, headers=headers
    )
    assert over.status_code == 422


async def test_review_grant_lifecycle(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    teacher_id = admin_world["teacher"].id

    granted = await client.post(
        f"{_API}/admin/reward-review-grants",
        json={"teacher_id": str(teacher_id), "reason": "教务授权"},
        headers=headers,
    )
    assert granted.status_code == 204
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "REWARD_REVIEW_GRANTED")
        == 1
    )

    duplicate = await client.post(
        f"{_API}/admin/reward-review-grants",
        json={"teacher_id": str(teacher_id), "reason": "重复授权"},
        headers=headers,
    )
    assert duplicate.status_code == 409
    # Outside Task 1's three replacement points: the grant duplicate keeps
    # the W3 VALIDATION_ERROR-at-409 shape (owner ruling pending; the wave
    # report lists it).
    assert duplicate.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR

    revoked = await client.delete(
        f"{_API}/admin/reward-review-grants/{teacher_id}",
        params={"reason": "收回"},
        headers=headers,
    )
    assert revoked.status_code == 204
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "REWARD_REVIEW_REVOKED")
        == 1
    )

    missing = await client.delete(
        f"{_API}/admin/reward-review-grants/{teacher_id}",
        params={"reason": "再次收回"},
        headers=headers,
    )
    assert missing.status_code == 404

    # Granting to a non-Teacher account is the typed 400.
    wrong_role = await client.post(
        f"{_API}/admin/reward-review-grants",
        json={"teacher_id": str(admin_world["student"].id), "reason": "错误目标"},
        headers=headers,
    )
    assert wrong_role.status_code == 400


async def test_reward_review_grant_listing_resolves_teacher_and_grantor(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
) -> None:
    """T10's query gap-fill: ``GET /admin/reward-review-grants`` answers
    the live grants — teacher id + display nickname (through the frozen
    directory port, never identity ORM) plus the granting Admin and
    time — newest first, offset-paginated with the family cap. Grant
    HISTORY stays in the audit stream: a revoked grant leaves the
    listing (presence-based assertions; the shared database may carry
    grants from other suites' committed runs)."""
    headers = admin_world["admin_headers"]
    admin = admin_world["admin"]
    teacher_id = admin_world["teacher"].id

    granted = await client.post(
        f"{_API}/admin/reward-review-grants",
        json={"teacher_id": str(teacher_id), "reason": "列表用例授权"},
        headers=headers,
    )
    assert granted.status_code == 204

    listed = await client.get(f"{_API}/admin/reward-review-grants", headers=headers)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["limit"] == 20 and page["offset"] == 0
    assert page["total"] >= 1
    row = next(item for item in page["items"] if item["teacher_id"] == str(teacher_id))
    # The nickname is the display fact the directory port resolves (the
    # seeding helper's 同学+用户名尾4位 form).
    assert row["nickname"] == "同学0002"
    assert row["granted_by"] == str(admin.id)
    assert row["granted_at"] is not None
    assert set(row) == {"teacher_id", "nickname", "granted_by", "granted_at"}
    # Newest first.
    granted_at = [item["granted_at"] for item in page["items"]]
    assert granted_at == sorted(granted_at, reverse=True)

    paged = await client.get(
        f"{_API}/admin/reward-review-grants", params={"limit": 1}, headers=headers
    )
    assert paged.status_code == 200
    assert len(paged.json()["items"]) == 1
    assert paged.json()["limit"] == 1

    over = await client.get(
        f"{_API}/admin/reward-review-grants", params={"limit": 51}, headers=headers
    )
    assert over.status_code == 422

    # Revocation removes the row from the current-state listing.
    revoked = await client.delete(
        f"{_API}/admin/reward-review-grants/{teacher_id}",
        params={"reason": "列表用例收回"},
        headers=headers,
    )
    assert revoked.status_code == 204
    after = await client.get(f"{_API}/admin/reward-review-grants", headers=headers)
    assert str(teacher_id) not in {item["teacher_id"] for item in after.json()["items"]}


# --- manual points adjustment ---------------------------------------------------------


async def test_points_adjustment_posts_through_the_ledger(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    student_id = admin_world["student"].id
    operation_id = uuid4()

    adjusted = await client.post(
        f"{_API}/admin/users/{student_id}/points-adjustment",
        json={
            "amount": 50,
            "reason": "人工补正",
            "operation_id": str(operation_id),
        },
        headers=headers,
    )
    assert adjusted.status_code == 200, adjusted.text
    entry = adjusted.json()
    assert entry["amount"] == 50

    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None and wallet.available_points == 50
    ledger_rows = list(
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student_id)
        )
    )
    assert [row.ledger_type for row in ledger_rows] == [LedgerType.ADMIN_ADJUSTMENT]
    # The caller's intent id IS the entry's source id (PR #5 fix A).
    assert ledger_rows[0].source_id == operation_id
    # Ranking-neutral by construction (plan review focus 4).
    assert ledger_rows[0].affects_balance is True
    assert ledger_rows[0].affects_ranking is False
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "ADMIN_POINTS_ADJUSTED")
        == 1
    )

    zero = await client.post(
        f"{_API}/admin/users/{student_id}/points-adjustment",
        json={
            "amount": 0,
            "reason": "零金额",
            "operation_id": str(uuid4()),
        },
        headers=headers,
    )
    assert zero.status_code == 422

    unknown = await client.post(
        f"{_API}/admin/users/{uuid4()}/points-adjustment",
        json={
            "amount": 5,
            "reason": "不存在的账号",
            "operation_id": str(uuid4()),
        },
        headers=headers,
    )
    assert unknown.status_code == 404


async def test_points_adjustment_is_idempotent_per_operation_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    """PR #5 fix A (P1, G8): the body's ``operation_id`` is the
    adjustment's idempotency key. A retry with the SAME id and intent
    replays the original entry — one ledger row, one balance move, one
    audit row; the same id under a DIFFERENT decision is the typed 409
    CONFLICT; a body without the id is the framework 422."""
    headers = admin_world["admin_headers"]
    student_id = admin_world["student"].id
    operation_id = uuid4()
    payload = {
        "amount": 7,
        "reason": "活动补偿",
        "operation_id": str(operation_id),
    }

    first = await client.post(
        f"{_API}/admin/users/{student_id}/points-adjustment",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"{_API}/admin/users/{student_id}/points-adjustment",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["ledger_entry_id"] == first.json()["ledger_entry_id"]

    rows = list(
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.source_id == operation_id)
        )
    )
    assert len(rows) == 1
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None and wallet.available_points == 7
    assert (
        await _audit_count(db_session, admin_world["admin"].id, "ADMIN_POINTS_ADJUSTED")
        == 1
    )

    conflict = await client.post(
        f"{_API}/admin/users/{student_id}/points-adjustment",
        json={
            "amount": 9,
            "reason": "另一个决定",
            "operation_id": str(operation_id),
        },
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == ErrorCode.CONFLICT
    assert conflict.json()["error"]["details"]["operation_id"] == str(operation_id)
    # The refused re-decision wrote nothing.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PointsLedger)
            .where(PointsLedger.user_id == student_id)
        )
        == 1
    )

    missing_id = await client.post(
        f"{_API}/admin/users/{student_id}/points-adjustment",
        json={"amount": 5, "reason": "缺少操作 ID"},
        headers=headers,
    )
    assert missing_id.status_code == 422


# --- notification templates -----------------------------------------------------------


async def test_notification_template_lifecycle(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    payload = {
        "event_type": "ACCOUNT_SECURITY",
        "channel": "IN_APP",
        "title": "账号安全通知",
        # The event's frozen placeholder vocabulary (templates.py):
        # event_summary/event_time — nickname is not an ACCOUNT_SECURITY
        # variable.
        "template_body": "{event_time}，{event_summary}。",
    }
    created = await client.post(
        f"{_API}/admin/notification-templates", json=payload, headers=headers
    )
    assert created.status_code == 200, created.text
    template = created.json()
    assert template["version"] == 1 and template["enabled"] is True
    assert (
        await _audit_count(
            db_session, admin_world["admin"].id, "NOTIFICATION_TEMPLATE_UPSERTED"
        )
        == 1
    )

    duplicate = await client.post(
        f"{_API}/admin/notification-templates", json=payload, headers=headers
    )
    assert duplicate.status_code == 409
    # Outside Task 1's three replacement points: the template conflict
    # keeps the W4 VALIDATION_ERROR-at-409 shape (owner ruling pending;
    # the wave report lists it).
    assert duplicate.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR

    patched = await client.patch(
        f"{_API}/admin/notification-templates/{template['id']}",
        json={"title": "账号安全提醒", "template_body": "{event_summary}。"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == 2

    disabled = await client.post(
        f"{_API}/admin/notification-templates/{template['id']}/disable",
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    reenabled = await client.post(
        f"{_API}/admin/notification-templates/{template['id']}/enable",
        headers=headers,
    )
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True

    # Unsafe markup is the write-time typed 422.
    unsafe = await client.post(
        f"{_API}/admin/notification-templates",
        json={
            "event_type": "ACCOUNT_SECURITY",
            "channel": "SMS",
            "title": "标题",
            "template_body": "{{event_summary}}",
        },
        headers=headers,
    )
    assert unsafe.status_code == 422


async def test_notification_template_listing_includes_disabled(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
) -> None:
    """T10's query gap-fill: ``GET /admin/notification-templates`` is
    the administration listing — disabled rows included, ordered by the
    UNIQUE (event_type, channel) pair, offset-paginated with the family
    cap (presence-based assertions; the shared database may carry
    template rows from other suites' committed runs)."""
    headers = admin_world["admin_headers"]
    created = await client.post(
        f"{_API}/admin/notification-templates",
        json={
            "event_type": "ACCOUNT_SECURITY",
            "channel": "SMS",
            "title": "列表用例模板",
            "template_body": "{event_time}，{event_summary}。",
        },
        headers=headers,
    )
    # The UNIQUE pair may survive from a committed prior run — a 409 is
    # tolerable (the listing below resolves the row either way, and the
    # disable then makes it the disabled row this test asserts on).
    assert created.status_code in (200, 409), created.text

    seeded = await client.get(f"{_API}/admin/notification-templates", headers=headers)
    assert seeded.status_code == 200
    template_id = next(
        item["id"]
        for item in seeded.json()["items"]
        if item["event_type"] == "ACCOUNT_SECURITY" and item["channel"] == "SMS"
    )
    disabled = await client.post(
        f"{_API}/admin/notification-templates/{template_id}/disable",
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    listed = await client.get(f"{_API}/admin/notification-templates", headers=headers)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["limit"] == 20 and page["offset"] == 0
    assert page["total"] >= 1
    # The disabled row rides the administration listing (the dispatch
    # read is not this surface's concern).
    row = next(
        item
        for item in page["items"]
        if item["event_type"] == "ACCOUNT_SECURITY" and item["channel"] == "SMS"
    )
    assert row["enabled"] is False
    assert set(row) == {
        "id",
        "event_type",
        "channel",
        "title",
        "template_body",
        "enabled",
        "version",
    }
    # Deterministic order: the (event_type, channel) pair.
    keys = [(item["event_type"], item["channel"]) for item in page["items"]]
    assert keys == sorted(keys)

    paged = await client.get(
        f"{_API}/admin/notification-templates", params={"limit": 1}, headers=headers
    )
    assert paged.status_code == 200
    assert len(paged.json()["items"]) == 1
    assert paged.json()["limit"] == 1

    over = await client.get(
        f"{_API}/admin/notification-templates",
        params={"limit": 51},
        headers=headers,
    )
    assert over.status_code == 422


# --- the repair commands --------------------------------------------------------------


async def test_repair_commands_answer_typed_not_found(
    client: httpx.AsyncClient,
    admin_world: dict[str, Any],
) -> None:
    headers = admin_world["admin_headers"]
    release = await client.post(
        f"{_API}/admin/repairs/release-occupied-assignment",
        json={"assignment_id": str(uuid4()), "reason": "悬挂占用量"},
        headers=headers,
    )
    assert release.status_code == 404
    assert release.json()["error"]["code"] == ErrorCode.NOT_FOUND

    fail = await client.post(
        f"{_API}/admin/repairs/force-fail-delivery",
        json={"delivery_id": str(uuid4()), "reason": "卡死投递"},
        headers=headers,
    )
    assert fail.status_code == 404


# --- staff invitation issuance --------------------------------------------------------


async def test_staff_invitation_issuance_is_rate_limited_and_audited(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    admin_world: dict[str, Any],
    fake_limiter: FakeRateLimiter,
) -> None:
    headers = admin_world["admin_headers"]
    issued = await client.post(
        f"{_API}/staff/invitations",
        json={"email": "New-Teacher@PKU.edu.cn", "role": "TEACHER"},
        headers=headers,
    )
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["role"] == "TEACHER"
    assert body["token"]  # the one-time capability, shown exactly once

    row = await db_session.scalar(select(StaffInvitation))
    assert row is not None
    assert row.email_normalized == "new-teacher@pku.edu.cn"
    assert row.token_hash != body["token"]  # the row stores only the hash
    assert (
        await _audit_count(
            db_session, admin_world["admin"].id, "STAFF_INVITATION_CREATED"
        )
        == 1
    )

    # The issuance bucket fired once, keyed by the inviting admin.
    assert [check.bucket for check in fake_limiter.checks] == ["staff:invitations"]
    assert fake_limiter.checks[-1].identifier == str(admin_world["admin"].id)

    # The role narrowing: STUDENT is not invitable through this surface
    # (probed before the limiter is programmed to fail — fail_on persists).
    bad_role = await client.post(
        f"{_API}/staff/invitations",
        json={"email": "someone@pku.edu.cn", "role": "STUDENT"},
        headers=headers,
    )
    assert bad_role.status_code == 400

    # An exhausted window is the 429 RATE_LIMITED envelope.
    fake_limiter.fail_on("staff:invitations")
    throttled = await client.post(
        f"{_API}/staff/invitations",
        json={"email": "another@pku.edu.cn", "role": "TEACHER"},
        headers=headers,
    )
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == ErrorCode.RATE_LIMITED


# --- helpers --------------------------------------------------------------------------


async def _audit_count(db: AsyncSession, actor_id: UUID, action: str) -> int:
    """Count the test's OWN audit rows: the shared test database carries
    committed audit rows from earlier runs of the concurrency suites, so
    every assertion scopes to this test's freshly-seeded actor."""
    return int(
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.actor_user_id == actor_id,
                AuditLog.action == action,
            )
        )
    )


async def _append_audit_row(db: AsyncSession, user: User, action: str) -> None:
    from app.modules.audit.service import AuditLogWriter
    from app.modules.identity.events import Actor

    await AuditLogWriter().append(
        db,
        actor=Actor(user_id=user.id, role=Role.ADMIN),
        action=action,
        target_type="probe",
        target_id="probe",
    )
    await db.commit()
