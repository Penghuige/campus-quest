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
  per request, so a future store can change policy without restart).
  TRANSITIONAL HOME (Plan 08 T5 alignment): the default loader reads the
  typed ``Settings`` env fields ``management_network_enabled`` /
  ``management_network_cidrs`` (comma-separated). T5 moves the values
  into the audited system_settings store; that wave replaces ONLY the
  default loader's body — the policy type, the parser, and the factory
  signature stay. The settings-based default is cached per process
  (``lru_cache``): env settings are frozen at startup anyway
  (``get_settings`` is cached), so per-request re-parsing would buy
  nothing.
- Denials are ``PERMISSION_DENIED`` 403 via ``BusinessError`` — the
  same envelope every guard raises, rendered with no per-route work.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Coroutine
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
    "load_management_network_policy",
    "parse_management_networks",
    "require_management_network",
]

# ipaddress' concrete types: hosts and networks for both families.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_PERMISSION_DENIED_MESSAGE = "当前网络不允许访问管理功能"

# A single IPv4/IPv6 network is VARCHAR-materializable; this module never
# persists them (T5 will, in system_settings), but the bound keeps the
# parsing vocabulary honest from day one.
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


def load_management_network_policy(
    settings: Settings | None = None,
) -> ManagementNetworkPolicy:
    """Build the policy from typed ``Settings`` fields (the T5
    transitional loader; see the module docstring). Invalid CIDRs raise
    ``ValueError`` here — the deployment fails at wiring/load, not at
    the first guarded request."""
    source = settings if settings is not None else get_settings()
    return ManagementNetworkPolicy(
        enabled=source.management_network_enabled,
        networks=parse_management_networks(source.management_network_cidrs),
    )


@lru_cache(maxsize=1)
def _cached_settings_policy() -> ManagementNetworkPolicy:
    # Env settings are process-frozen (get_settings is lru_cached), so
    # the parsed policy is too. Tests inject policies through the
    # factory parameter instead of flipping this cache.
    return load_management_network_policy()


def require_management_network(
    policy_loader: Callable[[], ManagementNetworkPolicy] | None = None,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """FastAPI dependency factory: when the restriction is enabled,
    reject requests from outside the configured networks (403).

    ``policy_loader`` is evaluated PER REQUEST so a future DB-backed
    store (T5) can change policy live; the default reads the (cached)
    typed settings. Disabled policies pass through immediately — the
    2FA/RBAC guards mounted beside this one are unaffected.
    """
    loader = policy_loader if policy_loader is not None else _cached_settings_policy

    async def management_network_guard(request: Request) -> None:
        policy = loader()
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
