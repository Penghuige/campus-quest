# backend/tests/unit/audit/test_audit_context.py
"""``AuditContext.from_request`` trust rules (spec §30; PR #2 hardening
pass 4a) — no database involved: the pair is derived purely from the
ASGI scope.

- the propagated ``X-Request-ID`` is kept only when it passes the
  propagation safety rule (length cap, safe charset): unsafe or absent
  means None, never the middleware's FABRICATED id (an audit row must
  correlate with a request the client actually made);
- ``ip_address`` is the directly connected peer, None when no client
  info exists (the reverse-proxy trust boundary lives in the module
  docstring and the deployment layer, not here).
"""

from __future__ import annotations

from starlette.requests import Request

from app.modules.audit.context import AuditContext


def _request(
    headers: dict[str, str] | None = None, client: tuple[str, int] | None = None
) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw,
        "query_string": b"",
        "client": client,
    }
    return Request(scope)


def test_safe_request_id_is_kept_verbatim() -> None:
    context = AuditContext.from_request(
        _request({"X-Request-ID": "abc-123_def.456"}, ("203.0.113.9", 4200))
    )
    assert context.request_id == "abc-123_def.456"
    assert context.ip_address == "203.0.113.9"


def test_missing_request_id_is_none_not_a_fabricated_uuid() -> None:
    context = AuditContext.from_request(_request(client=("203.0.113.9", 4200)))
    assert context.request_id is None


def test_unsafe_request_id_is_treated_as_absent() -> None:
    """Over-length and bad-charset ids never reach the audit table: the
    VARCHAR(128) column and the log surfaces must never see hostile
    input (the middleware's hostility rule, applied at capture)."""
    for hostile in ("x" * 200, "id with spaces", "id<script>", "id;rm -rf"):
        context = AuditContext.from_request(
            _request({"X-Request-ID": hostile}, ("203.0.113.9", 4200))
        )
        assert context.request_id is None, hostile


def test_missing_client_is_none_ip() -> None:
    context = AuditContext.from_request(_request({"X-Request-ID": "ok-1"}))
    assert context.request_id == "ok-1"
    assert context.ip_address is None
