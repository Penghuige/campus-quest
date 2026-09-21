# backend/app/core/observability.py
"""Request-id propagation.

Every HTTP response carries the resolved request id in the `X-Request-ID`
header, and the id is stored on `request.state.request_id` so exception
handlers can embed it in the unified error envelope (spec §29).
An incoming id is trusted only after validation (length cap, safe charset);
otherwise a fresh UUID hex is generated so hostile values are never reflected.
"""

import re
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_LENGTH = 128
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]+")


def is_safe_request_id(value: str) -> bool:
    """Whether an incoming request id may be propagated verbatim: the
    length cap and safe charset below. Shared by the middleware's
    resolution and the audit context's capture, so a value one layer
    accepts can never overflow or inject at the other."""
    return (
        len(value) <= MAX_REQUEST_ID_LENGTH
        and _SAFE_REQUEST_ID.fullmatch(value) is not None
    )


def resolve_request_id(incoming: str | None) -> str:
    """Return the incoming request id if safe, else a generated UUID hex."""
    if incoming is not None and is_safe_request_id(incoming):
        return incoming
    return uuid4().hex


class RequestIDMiddleware:
    """Pure-ASGI middleware: resolve, store on state, echo on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Request(scope).headers.get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Replace (not append) so handler-set values are not duplicated.
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)
