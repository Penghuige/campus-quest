# backend/app/modules/audit/context.py
"""Request-scoped metadata for durable audit rows (spec §30; PR #2
hardening pass 4a, corrected in pass 5b).

``AuditContext`` is the small value the ROUTER layer derives from the
HTTP request and the SERVICE layer threads into ``AuditLogWriter
.append`` — the correlation pair §30 asks beside the decision itself:

- ``request_id``: the middleware-RESOLVED id from
  ``request.state.request_id`` (PR #2 round-5 P1). The raw header is
  never read here: ``RequestIDMiddleware`` has already applied the
  propagation safety rule (length cap, safe charset — a hostile value
  is replaced, never reflected), so ``state.request_id`` is by
  construction either the caller-propagated id that passed that rule
  or the server-generated UUID hex — both fit ``request_id``'s
  VARCHAR(128) and both mean "the server's identifier for THIS
  request", which is exactly what a §30 correlation wants: the audit
  row joins the request the server actually processed, response
  header ``X-Request-ID`` and error envelope included. Reading only
  the incoming header (the pass-4a shape) left the id NULL for every
  request without a client-supplied header, breaking the correlation
  for the common case. ``None`` survives only as the no-middleware
  fallback (a hand-built scope) and on non-HTTP callers (workers),
  which legitimately have no request at all — the documented default,
  not a gap (models.py's 0016 note).
- ``ip_address``: ``request.client.host`` — the address of the
  DIRECTLY connected peer. TRUST BOUNDARY: V1 runs direct-connect
  semantics; behind a reverse proxy this is the proxy's address, and
  a deployment that wants real client ips must rewrite them at the
  deployment layer (connection forwarding / proxy protocol) BEFORE
  the app sees them. The app itself never parses ``X-Forwarded-For``
  — a spoofable header is worse than a proxy ip on an audit row.

The dataclass is framework-free at runtime — only ``from_request``
touches starlette, so services and workers can hold ``AuditContext``
values without the web stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the runtime import surface stays web-free
    from starlette.requests import Request

__all__ = ["AuditContext"]


@dataclass(frozen=True, slots=True)
class AuditContext:
    """The request correlation pair an audit row carries when the
    sensitive action ran inside an HTTP request (spec §30)."""

    request_id: str | None
    ip_address: str | None

    @classmethod
    def from_request(cls, request: Request) -> AuditContext:
        """Derive the pair from the live request (see the module
        docstring for both fields' trust rules)."""
        # The middleware's resolved id — safe by construction (its only
        # writer): either the propagated id that passed the safety rule
        # or the generated UUID hex. Missing entirely only when the
        # scope never went through the middleware.
        request_id = getattr(request.state, "request_id", None)
        ip_address = request.client.host if request.client is not None else None
        return cls(request_id=request_id, ip_address=ip_address)
