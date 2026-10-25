# backend/tests/unit/submissions/test_rate_limit_envelope.py
"""The submissions module's 429 envelope handler, pinned directly.

The endpoint path can surface a throttled request through more than one
registered renderer (identity/tasks register their own), so the
handler's own contract — 429 + RATE_LIMITED envelope + request-id
header — is pinned here rather than inferred from an end-to-end 429.
"""

import pytest
from starlette.requests import Request

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.integrations.rate_limit import RateLimitExceededError

_SETTINGS_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:15432/none",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_BUCKET": "rate-envelope-test",
    "S3_ACCESS_KEY": "rate-envelope",
    "S3_SECRET_KEY": "rate-envelope",
    "BUSINESS_TIMEZONE": "Asia/Shanghai",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    for key, value in _SETTINGS_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _request(request_id: str | None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/submissions/upload-intent",
        "headers": [],
    }
    request = Request(scope)
    if request_id is not None:
        request.state.request_id = request_id
    return request


@pytest.mark.parametrize("request_id", ["rid-123", None])
async def test_the_submissions_rate_limit_handler_renders_the_envelope(request_id):
    from fastapi import FastAPI

    from app.modules.submissions import router as submissions_router

    # Register on a bare app: create_app()'s shared handler map keeps only
    # the LAST module's RateLimitExceededError renderer (community), so the
    # submissions module's own renderer is reachable through its own
    # registration entry only.
    app = FastAPI()
    submissions_router.register_submissions_exception_handlers(app)
    handler = app.exception_handlers[RateLimitExceededError]
    response = await handler(_request(request_id), RateLimitExceededError("b"))
    assert response.status_code == 429
    body = response.body.decode()
    assert ErrorCode.RATE_LIMITED.value in body
    if request_id is not None:
        assert response.headers.get("X-Request-ID") == request_id
