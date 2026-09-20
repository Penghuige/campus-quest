# backend/tests/unit/core/test_errors.py
"""BusinessError envelope, framework-error envelopes, and request-id
propagation (spec §29, interfaces.md error-code registry)."""

import logging
import re

from fastapi import FastAPI
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.main import create_app

_GENERATED_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


def _boom_app() -> FastAPI:
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise BusinessError(
            code=ErrorCode.NO_ASSIGNMENT_AVAILABLE,
            message="当前没有可领取的任务",
            status_code=409,
            details={"task_id": "abc"},
        )

    return app


def test_business_error_has_stable_envelope():
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise BusinessError(
            code=ErrorCode.NO_ASSIGNMENT_AVAILABLE,
            message="当前没有可领取的任务",
            status_code=409,
            details={"task_id": "abc"},
        )

    response = TestClient(app).get("/boom", headers={"X-Request-ID": "req-1"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.NO_ASSIGNMENT_AVAILABLE
    assert response.json()["error"]["request_id"] == "req-1"


def test_business_error_envelope_has_frozen_shape_and_header():
    response = TestClient(_boom_app()).get("/boom", headers={"X-Request-ID": "req-1"})

    assert response.json() == {
        "error": {
            "code": ErrorCode.NO_ASSIGNMENT_AVAILABLE,
            "message": "当前没有可领取的任务",
            "details": {"task_id": "abc"},
            "request_id": "req-1",
        }
    }
    assert response.headers["X-Request-ID"] == "req-1"


def test_business_error_accepts_registry_constants_and_plain_strings():
    # Transport stays string-typed: both a registry constant and a plain
    # string serialize to the same wire value.
    assert (
        BusinessError(code=ErrorCode.VALIDATION_ERROR, message="x").code
        == "VALIDATION_ERROR"
    )
    assert (
        BusinessError(code="VALIDATION_ERROR", message="x").code
        == ErrorCode.VALIDATION_ERROR
    )


def test_business_error_defaults_to_400_and_null_details():
    exc = BusinessError(code=ErrorCode.VALIDATION_ERROR, message="参数错误")
    assert exc.status_code == 400
    assert exc.details is None


def test_oversized_request_id_is_replaced_not_reflected():
    oversized = "a" * 10_000
    response = TestClient(_boom_app()).get("/boom", headers={"X-Request-ID": oversized})

    assert response.status_code == 409
    request_id = response.json()["error"]["request_id"]
    assert request_id != oversized
    assert _GENERATED_REQUEST_ID.match(request_id)
    assert response.headers["X-Request-ID"] == request_id


def test_unsafe_charset_request_id_is_replaced():
    response = TestClient(_boom_app()).get(
        "/boom", headers={"X-Request-ID": "req-1<script>"}
    )

    request_id = response.json()["error"]["request_id"]
    assert request_id != "req-1<script>"
    assert _GENERATED_REQUEST_ID.match(request_id)
    assert response.headers["X-Request-ID"] == request_id


def test_missing_request_id_gets_generated_uuid():
    response = TestClient(_boom_app()).get("/boom")

    request_id = response.json()["error"]["request_id"]
    assert _GENERATED_REQUEST_ID.match(request_id)
    assert response.headers["X-Request-ID"] == request_id


def test_unexpected_exception_returns_safe_500_envelope():
    app = create_app()

    @app.get("/explode")
    async def explode():
        raise RuntimeError(
            "boom SELECT secret FROM internal_table path=/etc/campusquest"
        )

    response = TestClient(app, raise_server_exceptions=False).get(
        "/explode", headers={"X-Request-ID": "req-2"}
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": ErrorCode.INTERNAL_ERROR,
            "message": "服务器内部错误",
            "details": None,
            "request_id": "req-2",
        }
    }
    assert response.headers["X-Request-ID"] == "req-2"
    assert "SELECT" not in response.text
    assert "/etc/campusquest" not in response.text
    assert "Traceback" not in response.text


def test_unexpected_exception_is_logged_with_traceback(caplog):
    app = create_app()

    @app.get("/explode")
    async def explode():
        raise RuntimeError("kaboom")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.core.errors"):
        client.get("/explode")

    assert any(record.exc_info for record in caplog.records)


def test_health_live_still_ok_and_carries_request_id_header():
    response = TestClient(create_app()).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert _GENERATED_REQUEST_ID.match(response.headers["X-Request-ID"])


# --- Framework errors: the §29 envelope must cover framework failures too ---


class _EchoIn(BaseModel):
    name: str
    limit: int


def _schema_app() -> FastAPI:
    """App with a request-schema-bearing route, like /boom for BusinessError."""

    app = create_app()

    @app.post("/echo")
    async def echo(payload: _EchoIn) -> dict[str, str]:
        return {"name": payload.name}

    return app


def test_request_validation_error_returns_422_envelope():
    response = TestClient(_schema_app()).post(
        "/echo",
        json={"limit": "not-a-number-xyz"},
        headers={"X-Request-ID": "req-422"},
    )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == ErrorCode.VALIDATION_ERROR
    assert body["request_id"] == "req-422"
    assert response.headers["X-Request-ID"] == "req-422"
    # Compact field -> error mapping only: pydantic field names as keys,
    # message lists as values; the offending input value never echoes back.
    assert set(body["details"]) == {"name", "limit"}
    for messages in body["details"].values():
        assert isinstance(messages, list) and messages
        assert all(isinstance(message, str) for message in messages)
    assert "not-a-number-xyz" not in response.text


def test_malformed_json_body_returns_422_envelope():
    response = TestClient(_schema_app()).post(
        "/echo",
        content="{not json",
        headers={"X-Request-ID": "req-422b", "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == ErrorCode.VALIDATION_ERROR
    assert body["request_id"] == "req-422b"
    assert response.headers["X-Request-ID"] == "req-422b"


def test_unknown_path_returns_404_envelope_with_request_id():
    response = TestClient(create_app()).get(
        "/definitely-not-a-route", headers={"X-Request-ID": "req-404"}
    )

    assert response.status_code == 404
    body = response.json()["error"]
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == ErrorCode.NOT_FOUND
    assert body["details"] is None
    assert body["request_id"] == "req-404"
    assert response.headers["X-Request-ID"] == "req-404"


def test_wrong_method_returns_405_envelope():
    response = TestClient(create_app()).post(
        "/health/live", headers={"X-Request-ID": "req-405"}
    )

    assert response.status_code == 405
    body = response.json()["error"]
    assert body["code"] == ErrorCode.METHOD_NOT_ALLOWED
    assert body["request_id"] == "req-405"
    assert response.headers["X-Request-ID"] == "req-405"


def test_other_framework_http_exception_maps_to_http_error_without_detail_leak():
    app = create_app()

    @app.get("/teapot")
    async def teapot():
        raise FastAPIHTTPException(
            status_code=418, detail="teapot internals path=/etc/campusquest"
        )

    response = TestClient(app).get("/teapot", headers={"X-Request-ID": "req-http"})

    assert response.status_code == 418
    body = response.json()["error"]
    assert body["code"] == ErrorCode.HTTP_ERROR
    assert body["request_id"] == "req-http"
    assert response.headers["X-Request-ID"] == "req-http"
    # `detail` is deliberately not echoed: raisers may put internals in it.
    assert "/etc/campusquest" not in response.text
