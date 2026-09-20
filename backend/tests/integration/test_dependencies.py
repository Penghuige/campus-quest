# backend/tests/integration/test_dependencies.py
"""Dependency health tests for the local Docker Compose stack.

Each test touches one real service (PostgreSQL, Redis, object storage)
through typed settings, so a broken or missing local stack fails loudly
here with a service-specific error instead of surfacing later as
confusing failures in feature integration suites.
"""

from __future__ import annotations

import boto3
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings


@pytest.mark.integration
async def test_postgres_executes_select_1(db_session: AsyncSession) -> None:
    value = await db_session.scalar(text("select 1"))
    assert value == 1


@pytest.mark.integration
async def test_redis_answers_ping() -> None:
    client = aioredis.from_url(get_settings().redis_url)
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()


@pytest.mark.integration
def test_object_storage_serves_the_configured_bucket() -> None:
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    bucket_names = [bucket["Name"] for bucket in client.list_buckets()["Buckets"]]
    assert settings.s3_bucket in bucket_names
    client.head_bucket(Bucket=settings.s3_bucket)
