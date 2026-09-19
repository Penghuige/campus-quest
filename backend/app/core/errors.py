# backend/app/core/errors.py
"""Stable business errors and the unified error envelope (spec §29).

Every business conflict raises `BusinessError` carrying a stable code from
the registry in docs/architecture/interfaces.md; handlers render the frozen
envelope `{"error": {code, message, details, request_id}}`. Framework
failures reuse the same envelope with SYSTEM codes: request-schema
validation (422 `VALIDATION_ERROR`), Starlette/FastAPI `HTTPException`
(404 `NOT_FOUND`, 405 `METHOD_NOT_ALLOWED`, anything else `HTTP_ERROR`),
and unexpected exceptions (safe 500 `INTERNAL_ERROR` with the traceback
logged server-side only) — docs/quality/backend-engineering.md §10, §15.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.error_codes import ErrorCode
from app.core.observability import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "服务器内部错误"
VALIDATION_ERROR_MESSAGE = "请求参数校验失败"

# Framework HTTP status -> envelope code/message. The mapping is fixed and
# status-derived: `HTTPException.detail` is deliberately NOT used as a code
# source or echoed in the response, because raisers may put internal paths
# or provider payloads in it (backend-engineering §10: leak nothing).
_HTTP_STATUS_CODES: dict[int, ErrorCode] = {
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
}
_HTTP_STATUS_MESSAGES: dict[int, str] = {
    404: "请求的资源不存在",
    405: "请求方法不被允许",
}
_HTTP_ERROR_MESSAGE = "请求处理失败"


class BusinessError(Exception):
    """Business conflict with a stable code and HTTP status.

    `code` is typed `ErrorCode | str`: call sites pass `ErrorCode` members
    from the frozen registry (docs/architecture/interfaces.md); plain
    strings remain accepted at the transport boundary only.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode | str = code
        self.message = message
        self.status_code = status_code
        self.details = details


def error_envelope(
    code: str,
    message: str,
    details: dict[str, Any] | None,
    request_id: str | None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id,
        }
    }


def _envelope_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None,
    request_id: str | None,
) -> JSONResponse:
    headers = {REQUEST_ID_HEADER: request_id} if request_id is not None else None
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(code, message, details, request_id),
        headers=headers,
    )


def _validation_field_errors(exc: RequestValidationError) -> dict[str, list[str]]:
    """Compact `field -> [messages]` mapping from pydantic error objects.

    Only `loc` (minus the body/query/path source segment) and `msg` are
    kept: `input`, `ctx`, and `url` can carry raw request payloads or
    provider internals that must not reach the response.
    """
    fields: dict[str, list[str]] = {}
    for error in exc.errors():
        loc = [
            str(part)
            for part in error.get("loc", ())
            if part not in ("body", "query", "path", "header", "cookie")
        ]
        field = ".".join(loc) or "body"
        fields.setdefault(field, []).append(str(error.get("msg", "invalid")))
    return fields


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the §29 envelope handlers to an app."""

    @app.exception_handler(BusinessError)
    async def handle_business_error(
        request: Request, exc: BusinessError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return _envelope_response(
            exc.status_code, exc.code, exc.message, exc.details, request_id
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return _envelope_response(
            422,
            ErrorCode.VALIDATION_ERROR,
            VALIDATION_ERROR_MESSAGE,
            _validation_field_errors(exc),
            request_id,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_framework_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # fastapi.HTTPException subclasses the Starlette one, so raising
        # either from a route lands here too.
        request_id = getattr(request.state, "request_id", None)
        code = _HTTP_STATUS_CODES.get(exc.status_code, ErrorCode.HTTP_ERROR)
        message = _HTTP_STATUS_MESSAGES.get(exc.status_code, _HTTP_ERROR_MESSAGE)
        return _envelope_response(exc.status_code, code, message, None, request_id)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        # Traceback goes to server logs only; the response stays generic.
        logger.exception(
            "Unhandled exception request_id=%s path=%s", request_id, request.url.path
        )
        return _envelope_response(
            500, ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE, None, request_id
        )
