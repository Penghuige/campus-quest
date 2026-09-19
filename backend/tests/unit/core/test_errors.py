# backend/tests/unit/core/test_errors.py
"""BusinessError envelope and request-id propagation (spec §29, interfaces.md)."""

import logging
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import BusinessError
from app.main import create_app

_GENERATED_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


def _boom_app() -> FastAPI:
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise BusinessError(
            code="NO_ASSIGNMENT_AVAILABLE",
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
            code="NO_ASSIGNMENT_AVAILABLE",
            message="当前没有可领取的任务",
            status_code=409,
            details={"task_id": "abc"},
        )

    response = TestClient(app).get("/boom", headers={"X-Request-ID": "req-1"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_ASSIGNMENT_AVAILABLE"
    assert response.json()["error"]["request_id"] == "req-1"


def test_business_error_envelope_has_frozen_shape_and_header():
    response = TestClient(_boom_app()).get("/boom", headers={"X-Request-ID": "req-1"})

    assert response.json() == {
        "error": {
            "code": "NO_ASSIGNMENT_AVAILABLE",
            "message": "当前没有可领取的任务",
            "details": {"task_id": "abc"},
            "request_id": "req-1",
        }
    }
    assert response.headers["X-Request-ID"] == "req-1"


def test_business_error_defaults_to_400_and_null_details():
    exc = BusinessError(code="VALIDATION_ERROR", message="参数错误")
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
            "code": "INTERNAL_ERROR",
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
