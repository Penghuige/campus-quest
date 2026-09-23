# backend/tests/unit/admin/test_network_policy.py
"""The optional management-network restriction (Plan 08 T8 step 4,
migrated to the audited settings store in T5) as pure units:
standard-library CIDR parsing (fail-loud on any malformed entry, host
bits included), the enabled/empty-allowlist guard, the allow matrix
over both IP families, the FastAPI dependency factory's
enabled/disabled matrix (sync AND async loaders — the store-reading
shape), and the Plan 08 T5 resolution contract — settings-store rows
first, per-key fallback to the deprecated env fields.

The integration wiring (the guard mounted beside the management actors
on real routes) is Plan 08 T9; here the guard is driven directly with
constructed starlette requests, so no app or database is involved.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request

from app.core.admin_network_policy import (
    ManagementNetworkPolicy,
    load_management_network_policy,
    parse_management_networks,
    require_management_network,
)
from app.core.config import Settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError

_OFFICE_V4 = "10.20.0.0/16"
_VPN_V6 = "2001:db8:100::/48"


def _request(host: str | None, headers: dict[str, str] | None = None) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/admin/ops",
        "query_string": b"",
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
    }
    if host is not None:
        scope["client"] = (host, 51234)
    return Request(scope)


def _enabled_policy() -> ManagementNetworkPolicy:
    return ManagementNetworkPolicy(
        enabled=True,
        networks=parse_management_networks(f" {_OFFICE_V4},{_VPN_V6},"),
    )


# --- parsing: standard library, fail loud --------------------------------------------


def test_parses_mixed_families_with_whitespace_tolerance() -> None:
    networks = parse_management_networks(f" {_OFFICE_V4} ,\t{_VPN_V6}\n,10.0.0.7,")
    assert [str(network) for network in networks] == [
        _OFFICE_V4,
        _VPN_V6,
        "10.0.0.7/32",  # a bare IP is a one-host allowlist entry
    ]


def test_empty_list_parses_to_no_networks() -> None:
    assert parse_management_networks("") == ()
    assert parse_management_networks(" , ") == ()


@pytest.mark.parametrize(
    "cidrs",
    [
        "10.0.0.0/33",  # invalid prefix length
        "300.1.1.0/24",  # invalid octet
        "not-a-cidr",
        "10.0.0.5/24",  # host bits set: strict parsing refuses
        "2001:db8::/129",
    ],
)
def test_invalid_entries_fail_loud_at_parse_time(cidrs: str) -> None:
    with pytest.raises(ValueError):
        parse_management_networks(cidrs)


def test_enabled_allowlist_may_not_be_empty() -> None:
    # Enabled + zero networks is indistinguishable from a half-entered
    # configuration; refuse it at construction, not on the first request.
    with pytest.raises(ValueError, match="management_network_cidrs"):
        ManagementNetworkPolicy(enabled=True, networks=())


def test_disabled_allowlist_may_be_empty() -> None:
    policy = ManagementNetworkPolicy(enabled=False, networks=())
    assert policy.enabled is False


# --- the allow matrix ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("client_ip", "allowed"),
    [
        ("10.20.3.4", True),  # inside the office /16
        ("10.21.0.1", False),  # outside it (the /16 boundary)
        ("192.0.2.9", False),  # far outside
        ("2001:db8:100::1", True),  # inside the VPN /48
        ("2001:db8:200::1", False),  # outside it
        ("not-an-ip", False),  # malformed: fail closed
    ],
)
def test_allows_matrix_over_both_families(client_ip: str, allowed: bool) -> None:
    assert _enabled_policy().allows(client_ip) is allowed


# --- the dependency factory matrix --------------------------------------------------


async def test_enabled_guard_passes_in_network_client() -> None:
    guard = require_management_network(lambda: _enabled_policy())
    await guard(_request("10.20.30.40"))  # no raise == allowed


async def test_disabled_guard_passes_everyone_through() -> None:
    # Disabled is pure pass-through: an out-of-network (and even
    # unparseable) peer reaches the route; 2FA/RBAC still apply there.
    policy = ManagementNetworkPolicy(
        enabled=False, networks=parse_management_networks(_OFFICE_V4)
    )
    guard = require_management_network(lambda: policy)
    await guard(_request("203.0.113.50"))
    await guard(_request("garbage"))


async def test_enabled_guard_denies_out_of_network_client() -> None:
    guard = require_management_network(lambda: _enabled_policy())
    with pytest.raises(BusinessError) as exc_info:
        await guard(_request("203.0.113.50"))
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED


async def test_forwarded_header_grants_nothing() -> None:
    # Trust boundary: only request.client.host counts. A spoofed
    # X-Forwarded-For naming an in-network address must not pass an
    # out-of-network peer (the AuditContext discipline, verbatim).
    guard = require_management_network(lambda: _enabled_policy())
    with pytest.raises(BusinessError):
        await guard(
            _request(
                "203.0.113.50",
                headers={
                    "x-forwarded-for": "10.20.0.1",
                    "x-real-ip": "10.20.0.1",
                },
            )
        )


async def test_enabled_guard_denies_when_no_client_address() -> None:
    guard = require_management_network(lambda: _enabled_policy())
    with pytest.raises(BusinessError):
        await guard(_request(None))


# --- the policy loader (Plan 08 T5: store first, env fallback) -----------------------


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def _env_settings(
    monkeypatch: pytest.MonkeyPatch, *, enabled: str, cidrs: str
) -> Settings:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MANAGEMENT_NETWORK_ENABLED", enabled)
    monkeypatch.setenv("MANAGEMENT_NETWORK_CIDRS", cidrs)
    return Settings()


def test_store_rows_win_over_env_values(monkeypatch) -> None:
    settings = _env_settings(monkeypatch, enabled="false", cidrs="203.0.113.0/24")

    policy = load_management_network_policy(
        stored_enabled="true",
        stored_cidrs=f"{_OFFICE_V4},{_VPN_V6}",
        settings=settings,
    )

    assert policy.enabled is True
    assert [str(network) for network in policy.networks] == [_OFFICE_V4, _VPN_V6]


def test_absent_store_keys_fall_back_per_key_to_env(monkeypatch) -> None:
    # Per-key fallback (the transition contract): a row decides its own
    # key only — here the stored ENABLED flag pairs with the env CIDRs.
    settings = _env_settings(monkeypatch, enabled="false", cidrs=_OFFICE_V4)

    enabled_only = load_management_network_policy(
        stored_enabled="true", stored_cidrs=None, settings=settings
    )
    assert enabled_only.enabled is True
    assert [str(network) for network in enabled_only.networks] == [_OFFICE_V4]

    cidrs_only = load_management_network_policy(
        stored_enabled=None, stored_cidrs=_VPN_V6, settings=settings
    )
    assert cidrs_only.enabled is False
    assert [str(network) for network in cidrs_only.networks] == [_VPN_V6]


def test_no_store_rows_is_the_pure_env_policy(monkeypatch) -> None:
    settings = _env_settings(monkeypatch, enabled="true", cidrs=_OFFICE_V4)

    policy = load_management_network_policy(
        stored_enabled=None, stored_cidrs=None, settings=settings
    )

    assert policy.enabled is True
    assert [str(network) for network in policy.networks] == [_OFFICE_V4]


def test_no_store_rows_default_to_disabled_pass_through(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    settings = Settings()

    policy = load_management_network_policy(
        stored_enabled=None, stored_cidrs=None, settings=settings
    )

    assert policy.enabled is False
    assert policy.networks == ()


def test_loader_fails_loud_on_invalid_stored_cidrs(monkeypatch) -> None:
    settings = _env_settings(monkeypatch, enabled="true", cidrs=_OFFICE_V4)

    with pytest.raises(ValueError):
        load_management_network_policy(
            stored_enabled="true", stored_cidrs="10.0.0.0/33", settings=settings
        )


def test_loader_fails_loud_on_unparseable_stored_flag(monkeypatch) -> None:
    # A corrupted store value is a configuration error, never a
    # silently-different policy (G5).
    settings = _env_settings(monkeypatch, enabled="true", cidrs=_OFFICE_V4)

    with pytest.raises(ValueError, match="MANAGEMENT_NETWORK_ENABLED"):
        load_management_network_policy(
            stored_enabled="yes", stored_cidrs=_OFFICE_V4, settings=settings
        )


# --- the dependency factory: async loaders (the store-reading shape) ------------------


async def test_guard_awaits_an_async_store_reading_loader() -> None:
    # The T9 composition shape: the loader reads the audited store per
    # request (a DB round-trip — necessarily async); the guard must
    # await it rather than treat the coroutine as a policy.
    async def _store_reading_loader() -> ManagementNetworkPolicy:
        return _enabled_policy()

    guard = require_management_network(_store_reading_loader)
    await guard(_request("10.20.30.40"))  # in-network: no raise
    with pytest.raises(BusinessError) as exc_info:
        await guard(_request("203.0.113.50"))
    assert exc_info.value.status_code == 403


async def test_guard_still_accepts_sync_loaders() -> None:
    guard = require_management_network(lambda: _enabled_policy())
    await guard(_request("10.20.30.40"))
    with pytest.raises(BusinessError):
        await guard(_request("203.0.113.50"))
