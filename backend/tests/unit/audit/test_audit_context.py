# backend/tests/unit/audit/test_audit_context.py
"""``AuditContext.from_request`` trust rules (spec §30; PR #2 hardening
pass 4a, corrected in pass 5b) — no database involved: the pair is
derived purely from the ASGI scope.

- ``request_id`` is the middleware-RESOLVED id from
  ``request.state.request_id`` — the id the server actually processed
  the request under (response header / error envelope carry the same
  value). The raw ``X-Request-ID`` header is never read here: the
  middleware has already kept a safe propagated id verbatim or
  replaced a hostile/absent one with a generated UUID hex, so the
  audit row correlates with the request the server really served
  (round-5 P1: the pass-4a raw-header read left request_id NULL for
  every request without a client-supplied header).
- a scope that never went through the middleware (hand-built request,
  non-HTTP caller) keeps ``None`` — the documented default, not a gap;
- ``ip_address`` is the directly connected peer, None when no client
  info exists (the reverse-proxy trust boundary lives in the module
  docstring and the deployment layer, not here).
"""

from __future__ import annotations

from uuid import uuid4

from starlette.requests import Request

from app.core.observability import resolve_request_id
from app.modules.audit.context import AuditContext


def _request(
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = None,
    state_request_id: str | None = None,
) -> Request:
    """A scope shaped like the middleware's output: ``state.request_id``
    carries the RESOLVED id (what ``RequestIDMiddleware`` stores), which
    may differ from the raw header when the header was unsafe/absent."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw,
        "query_string": b"",
        "client": client,
    }
    if state_request_id is not None:
        scope["state"] = {"request_id": state_request_id}
    return Request(scope)  # type: ignore[arg-type]


def test_resolved_request_id_is_kept_verbatim() -> None:
    """A safe propagated id survives the middleware verbatim, and the
    audit row carries that same resolved id."""
    context = AuditContext.from_request(
        _request(
            {"X-Request-ID": "abc-123_def.456"},
            ("203.0.113.9", 4200),
            state_request_id=resolve_request_id("abc-123_def.456"),
        )
    )
    assert context.request_id == "abc-123_def.456"
    assert context.ip_address == "203.0.113.9"


def test_generated_request_id_is_read_from_state() -> None:
    """No client header: the middleware generated a UUID hex and stored
    it on state — the audit row carries THAT id (the server's
    identifier for the request), not None (round-5 P1)."""
    generated = uuid4().hex
    context = AuditContext.from_request(
        _request(client=("203.0.113.9", 4200), state_request_id=generated)
    )
    assert context.request_id == generated


def test_hostile_header_resolves_to_the_replacement_not_the_header() -> None:
    """An unsafe header was replaced by the middleware before the
    handler ran: the audit row carries the replacement id — the hostile
    input never reaches the audit table (VARCHAR(128) / log surfaces),
    and the correlation still names the request the server served."""
    for hostile in ("x" * 200, "id with spaces", "id<script>", "id;rm -rf"):
        replaced = resolve_request_id(hostile)
        assert replaced != hostile
        context = AuditContext.from_request(
            _request(
                {"X-Request-ID": hostile},
                ("203.0.113.9", 4200),
                state_request_id=replaced,
            )
        )
        assert context.request_id == replaced, hostile


def test_scope_without_middleware_state_is_none() -> None:
    """A hand-built scope that never passed the middleware (unit
    harnesses, non-HTTP callers): None is the documented default."""
    context = AuditContext.from_request(_request(client=("203.0.113.9", 4200)))
    assert context.request_id is None


def test_missing_client_is_none_ip() -> None:
    context = AuditContext.from_request(
        _request({"X-Request-ID": "ok-1"}, state_request_id="ok-1")
    )
    assert context.request_id == "ok-1"
    assert context.ip_address is None
