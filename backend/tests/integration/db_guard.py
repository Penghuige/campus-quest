# backend/tests/integration/db_guard.py
"""Shared guard and environment bootstrap for real-PostgreSQL integration tests.

Importing this module installs process-wide env defaults that point at the
disposable integration test database; real environment variables always win.
`require_test_database` refuses any URL whose database name is not an
explicit test database, which is the last line of defense before an
integration test touches a real PostgreSQL instance.
"""

from __future__ import annotations

import os

from sqlalchemy.engine import make_url

TEST_DATABASE_MARKER = "campusquest_test"

INTEGRATION_ENV_DEFAULTS: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:15432/campusquest_test",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_BUCKET": "campusquest-test",
    "BUSINESS_TIMEZONE": "Asia/Shanghai",
}


def apply_integration_env_defaults() -> None:
    """Install test env defaults; existing real env vars are preserved."""
    for name, value in INTEGRATION_ENV_DEFAULTS.items():
        os.environ.setdefault(name, value)


def require_test_database(database_url: str) -> str:
    """Return the URL after asserting its database name carries the marker."""
    database = make_url(database_url).database or ""
    if TEST_DATABASE_MARKER not in database:
        raise RuntimeError(
            f"Refusing to run integration tests against non-test database "
            f"{database!r} (DATABASE_URL={database_url!r}): the database name "
            f"must contain {TEST_DATABASE_MARKER!r}."
        )
    return database_url


apply_integration_env_defaults()
