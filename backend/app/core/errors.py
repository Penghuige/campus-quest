# backend/app/core/errors.py
"""Stable business errors and the unified error envelope (spec §29).

Every business conflict raises `BusinessError` carrying a stable code from
the registry in docs/architecture/interfaces.md; handlers render the frozen
envelope `{"error": {code, message, details, request_id}}`. Unexpected
exceptions become a safe 500 `INTERNAL_ERROR` envelope with the traceback
logged server-side only (docs/quality/backend-engineering.md §10, §15).
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.observability import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE = "服务器内部错误"


class BusinessError(Exception):
    """Business conflict with a stable code and HTTP status."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
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

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        # Traceback goes to server logs only; the response stays generic.
        logger.exception(
            "Unhandled exception request_id=%s path=%s", request_id, request.url.path
        )
        return _envelope_response(
            500, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE, None, request_id
        )
