# backend/app/modules/audit/context.py
"""Request-scoped metadata for durable audit rows (spec §30; PR #2
hardening pass 4a).

``AuditContext`` is the small value the ROUTER layer derives from the
HTTP request and the SERVICE layer threads into ``AuditLogWriter
.append`` — the correlation pair §30 asks beside the decision itself:

- ``request_id``: the caller-propagated ``X-Request-ID`` when one
  arrived and passed the propagation safety rule (the same
  length/charset check ``RequestIDMiddleware`` applies); otherwise
  ``None``. An id the rule rejects is treated as ABSENT, never stored:
  a hostile header must not overflow ``audit_logs.request_id`` or
  reach the audit table at all. The middleware-RESOLVED id (state/
  response) is deliberately not read here — a fabricated UUID would
  correlate an audit row with a request the client never made, and
  non-HTTP callers (workers) legitimately have no request at all.
- ``ip_address``: ``request.client.host`` — the address of the
  DIRECTLY connected peer. TRUST BOUNDARY: V1 runs direct-connect
  semantics; behind a reverse proxy this is the proxy's address, and
  a deployment that wants real client ips must rewrite them at the
  deployment layer (connection forwarding / proxy protocol) BEFORE
  the app sees them. The app itself never parses ``X-Forwarded-For``
  — a spoofable header is worse than a proxy ip on an audit row.

Both fields are ``None`` on non-HTTP callers (workers, service-level
tests): that is the documented default, not a gap (models.py's 0016
note). The dataclass is framework-free at runtime — only
``from_request`` touches starlette, so services and workers can hold
``AuditContext`` values without the web stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.observability import REQUEST_ID_HEADER, is_safe_request_id

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
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = (
            incoming if incoming is not None and is_safe_request_id(incoming) else None
        )
        ip_address = request.client.host if request.client is not None else None
        return cls(request_id=request_id, ip_address=ip_address)
