# backend/app/core/admin_network_policy.py
"""Optional management-network restriction for staff surfaces (Plan 08
T8 step 4; spec §33.4 adjacency).

Design decisions:

- **Standard-library parsing only:** CIDRs are parsed with
  :mod:`ipaddress` at POLICY-CONSTRUCTION time (``ip_network`` with the
  default ``strict=True`` — a CIDR with host bits set, e.g.
  ``10.0.0.5/24``, is a configuration error, not a network). An invalid
  entry raises ``ValueError`` immediately: fail loud at construction,
  never a silently-different allowlist at request time (G5).
- **Enabled with an empty network list is a construction error.** An
  allowlist that allows nothing is indistinguishable from a
  half-entered configuration; refusing it at load time names the
  mistake. Deployments that want the restriction off set
  ``management_network_enabled = false`` (the default) and leave the
  CIDR list empty.
- **The client IP is ``request.client.host`` — the DIRECTLY connected
  peer — and nothing else.** ``X-Forwarded-For`` (or any client-supplied
  header) is never read: it is spoofable, and a spoofable header is
  worse than a proxy address (the ``AuditContext`` trust-boundary
  discipline, verbatim). Behind a reverse proxy this compares the
  PROXY's address, so the CIDRs must list the proxy's network or — the
  correct fix — the deployment must rewrite addresses at the connection
  layer (proxy protocol / connection forwarding) BEFORE the app sees
  them; this module deliberately offers no trusted-proxy knob in V1.
- **Disabled means pass-through** (the factory returns without reading
  the request): 2FA/RBAC gates still apply because the route composes
  this guard BESIDE ``require_staff_management_actor`` /
  ``require_admin_actor``, not instead of them.
- **The guard is a FastAPI dependency factory** so a route family
  mounts it once::

      dependencies=[
          Depends(require_staff_management_actor),
          Depends(require_management_network()),
      ]

  The factory takes an optional ``policy_loader`` callable (evaluated
  per request, so the store can change policy without restart) — sync
  or ASYNC: a loader that reads the audited system_settings store needs
  a database round-trip per request, so the guard awaits an awaitable
  result and passes a plain value through (``inspect.isawaitable``).
  The DEFAULT loader is the process-cached env policy — TRANSITIONAL
  ONLY (see below): production wiring passes the store-reading loader
  explicitly (read the two ``MANAGEMENT_NETWORK_*`` rows through
  ``SystemSettingService.get`` per request and resolve them via
  ``load_management_network_policy``), which is the T9 composition
  step.
- **Store first, env fallback (Plan 08 T5 migration).** The policy's
  values live in the audited ``system_settings`` store under
  ``MANAGEMENT_NETWORK_ENABLED`` / ``MANAGEMENT_NETWORK_CIDRS``;
  ``load_management_network_policy`` resolves each key's value from the
  store when the row exists and falls back PER KEY to the typed
  ``Settings`` env fields ``management_network_enabled`` /
  ``management_network_cidrs`` (comma-separated) when it does not —
  deployments configured against the env vars keep working through the
  transition, and admins migrate by writing the store keys. The env
  fields are DEPRECATED transitional fallbacks and WILL BE REMOVED
  BEFORE THE V1 RELEASE (config.py marks them); after removal the
  loader's fallbacks go with them and the store is the only source.
  ``MANAGEMENT_NETWORK_ENABLED`` is stored ``"true"``/``"false"`` and
  ``MANAGEMENT_NETWORK_CIDRS`` comma-separated canonical CIDRs (the
  registry's canonical forms — see system/service.py).
- Denials are ``PERMISSION_DENIED`` 403 via ``BusinessError`` — the
  same envelope every guard raises, rendered with no per-route work.
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
    # policy — kept only until the T9 composition wires the
    # store-reading async loader (read the MANAGEMENT_NETWORK_* rows per
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
