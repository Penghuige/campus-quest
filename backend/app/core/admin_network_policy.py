# backend/app/core/admin_network_policy.py
"""Optional management-network IP allowlist for staff surfaces (spec §33.4).

Invariants:
- CIDRs are validated strictly (``ip_network(..., strict=True)``) at policy
  construction time; an enabled policy with an empty network list fails loud.
- Only the direct peer address (``request.client.host``) is checked —
  ``X-Forwarded-For`` is never trusted.
- Values resolve from ``system_settings`` (``MANAGEMENT_NETWORK_ENABLED`` /
  ``MANAGEMENT_NETWORK_CIDRS``) per request, falling back per key to the
  deprecated env fields until migrated. Denials raise ``PERMISSION_DENIED`` 403.
"""

from __future__ import annotations

import inspect
import ipaddress
from collections.abc import Awaitable, Callable, Coroutine
from functools import lru_cache
from typing import Any

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError

__all__ = [
    "IPAddress",
    "IPNetwork",
    "ManagementNetworkPolicy",
    "ManagementNetworkPolicyLoader",
    "load_management_network_policy",
    "parse_management_networks",
    "require_management_network",
]

# ipaddress' concrete types: hosts and networks for both families.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_PERMISSION_DENIED_MESSAGE = "当前网络不允许访问管理功能"

# A single IPv4/IPv6 network is VARCHAR-materializable; the store keeps
# them as comma-separated canonical CIDR text (system_settings, Plan 08
# T5), and this bound keeps the parsing vocabulary honest on both
# sides.
_MAX_NETWORKS = 64


def parse_management_networks(cidrs: str) -> tuple[IPNetwork, ...]:
    """Parse the comma-separated CIDR allowlist; raise ``ValueError`` on
    any malformed entry (construction-time fail-loud; see the module
    docstring). Empty entries and outer whitespace are tolerated — a
    trailing comma is a typo, not a policy."""
    networks: list[IPNetwork] = []
    for entry in cidrs.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        # strict=True (the default): host bits set -> ValueError. Bare
        # IPs ("10.0.0.1") parse as /32 networks — a valid one-host
        # allowlist entry, not an error.
        networks.append(ipaddress.ip_network(candidate))
    if len(networks) > _MAX_NETWORKS:
        raise ValueError(
            f"management_network_cidrs must list at most {_MAX_NETWORKS} networks"
        )
    return tuple(networks)


class ManagementNetworkPolicy:
    """The immutable, parsed form of the management-network settings."""

    def __init__(self, *, enabled: bool, networks: tuple[IPNetwork, ...]) -> None:
        if enabled and not networks:
            raise ValueError(
                "management_network_cidrs must list at least one network when "
                "management_network_enabled is true (an allowlist that allows "
                "nothing is a half-entered configuration; set enabled=false to "
                "leave the restriction off)"
            )
        self.enabled = enabled
        self.networks = networks

    def allows(self, client_ip: str) -> bool:
        """Whether the peer address falls inside the allowlist.

        An unparseable address string allows nothing (fail closed): a
        malformed peer address is not evidence of being in-network.
        """
        try:
            address: IPAddress = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(address in network for network in self.networks)


def _stored_enabled_flag(raw: str) -> bool:
    """Parse the stored ``MANAGEMENT_NETWORK_ENABLED`` value (the
    registry's canonical ``"true"``/``"false"``). Anything else fails
    loud at resolution time — a store that holds an unparseable flag is
    a corrupted configuration, never a silently-different policy (G5).
    """
    candidate = raw.strip().lower()
    if candidate == "true":
        return True
    if candidate == "false":
        return False
    raise ValueError(
        f"stored MANAGEMENT_NETWORK_ENABLED must be 'true' or 'false', got {raw!r}"
    )


def load_management_network_policy(
    *,
    stored_enabled: str | None = None,
    stored_cidrs: str | None = None,
    settings: Settings | None = None,
) -> ManagementNetworkPolicy:
    """Build the policy from the audited settings STORE first, falling
    back per key to the DEPRECATED env fields (the Plan 08 T5
    migration; see the module docstring).

    ``stored_enabled`` / ``stored_cidrs`` are the raw
    ``MANAGEMENT_NETWORK_ENABLED`` / ``MANAGEMENT_NETWORK_CIDRS`` row
    values (``None`` = no row — the env fallback decides that key);
    the composition root reads them through ``SystemSettingService.get``
    per request and hands them here. Invalid values raise ``ValueError``
    at resolution — the deployment fails at policy load, never as a
    silently-different allowlist at request time (G5).
    """
    source = settings if settings is not None else get_settings()
    enabled = (
        _stored_enabled_flag(stored_enabled)
        if stored_enabled is not None
        else source.management_network_enabled
    )
    cidrs = (
        stored_cidrs if stored_cidrs is not None else source.management_network_cidrs
    )
    return ManagementNetworkPolicy(
        enabled=enabled,
        networks=parse_management_networks(cidrs),
    )


@lru_cache(maxsize=1)
def _cached_settings_policy() -> ManagementNetworkPolicy:
    # TRANSITIONAL DEFAULT (Plan 08 T5): the env-only, process-cached
    # policy — used only when no loader is passed (tests and bare
    # factory calls); production goes through the T9 store-reading
    # async loader (read the MANAGEMENT_NETWORK_* rows per
    # request, resolve via ``load_management_network_policy``). Env
    # settings are process-frozen (get_settings is lru_cached), so the
    # parsed policy is too; tests inject policies through the factory
    # parameter instead of flipping this cache.
    return load_management_network_policy()


#: A policy source the guard evaluates per request: sync (returns the
#: policy) or async (returns an awaitable of it — the store-reading
#: loader's shape, since a DB read cannot be sync).
ManagementNetworkPolicyLoader = Callable[
    [], "ManagementNetworkPolicy | Awaitable[ManagementNetworkPolicy]"
]


def require_management_network(
    policy_loader: ManagementNetworkPolicyLoader | None = None,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """FastAPI dependency factory: when the restriction is enabled,
    reject requests from outside the configured networks (403).

    ``policy_loader`` is evaluated PER REQUEST and may be sync or async
    — the store-backed loader reads the ``MANAGEMENT_NETWORK_*`` rows
    from the audited settings store, so policy changes apply without a
    restart. The default is the TRANSITIONAL env-cached policy (see
    ``_cached_settings_policy``); production wiring passes the
    store-reading loader explicitly. Disabled policies pass through
    immediately — the 2FA/RBAC guards mounted beside this one are
    unaffected.
    """
    loader = policy_loader if policy_loader is not None else _cached_settings_policy

    async def management_network_guard(request: Request) -> None:
        policy = loader()
        if inspect.isawaitable(policy):
            policy = await policy
        if not policy.enabled:
            return
        # The DIRECTLY connected peer only — see the module docstring's
        # trust boundary (X-Forwarded-For is never consulted).
        client_ip = request.client.host if request.client is not None else None
        if client_ip is None or not policy.allows(client_ip):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _PERMISSION_DENIED_MESSAGE,
                status_code=403,
            )

    return management_network_guard
