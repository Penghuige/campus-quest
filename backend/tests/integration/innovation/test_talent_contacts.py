"""Contact disclosure is deliberate, versioned, rate limited and audited."""

import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditLog
from tests.integration.innovation.talent_support import BASE, command
from tests.integration.innovation.test_achievement_review_operations import account
from tests.integration.innovation.test_public_talents import PUBLIC, published
from tests.integration.innovation.test_public_talents import client as client
from tests.integration.innovation.test_public_talents import clock as clock
from tests.integration.innovation.test_public_talents import file_client as file_client
from tests.integration.innovation.test_public_talents import scanner as scanner
from tests.integration.innovation.test_public_talents import storage as storage

pytestmark = pytest.mark.integration


async def test_contact_limit_is_independent_account_scoped_thirty_per_minute(
    client, db_session, clock
):
    _, _, row = await published(
        client,
        db_session,
        clock,
        {"contacts": [{"channel": "WECHAT", "value": "demo_contact"}]},
    )
    _, viewer = await account(db_session, clock)
    path = PUBLIC + "/" + row["id"] + "/contacts"
    for _ in range(30):
        response = await client.post(
            path,
            headers=viewer,
            json={"expected_public_version": row["public_version"]},
        )
        assert response.status_code == 200
    rejected = await client.post(
        path, headers=viewer, json={"expected_public_version": row["public_version"]}
    )
    assert rejected.status_code == 429 and "demo_contact" not in rejected.text
    audits = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ie.talent.contacts-read")
        )
    )
    assert len(audits) == 30 and all(
        "demo_contact" not in str(a.details) for a in audits
    )


@pytest.mark.parametrize("failure", ["redis", "audit"])
async def test_contact_dependency_failure_discloses_no_values(
    client, db_session, clock, storage, failure
):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.modules.audit.service import AuditLogWriter
    from app.modules.innovation.public_talent_router import (
        get_public_talent_service,
        get_talent_contact_rate_limiter,
    )
    from app.modules.innovation.public_talent_service import PublicTalentService

    _, _, row = await published(
        client,
        db_session,
        clock,
        {"contacts": [{"channel": "WECHAT", "value": "demo_contact"}]},
    )
    _, viewer = await account(db_session, clock)

    class UnavailableLimiter:
        async def check(self, **kwargs):
            raise RedisConnectionError("controlled unavailable Redis")

    class UnavailableAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("controlled unavailable audit")

    app = client._transport.app
    if failure == "redis":
        app.dependency_overrides[get_talent_contact_rate_limiter] = lambda: (
            UnavailableLimiter()
        )
    else:
        app.dependency_overrides[get_public_talent_service] = lambda: (
            PublicTalentService(clock=clock, storage=storage, audit=UnavailableAudit())
        )
    response = await client.post(
        PUBLIC + "/" + row["id"] + "/contacts",
        headers=viewer,
        json={"expected_public_version": row["public_version"]},
    )
    assert response.status_code == (503 if failure == "redis" else 500)
    if failure == "redis":
        assert response.json()["error"]["code"] == "TALENT_CONTACTS_UNAVAILABLE"
    assert "demo_contact" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert (
        list(
            await db_session.scalars(
                select(AuditLog).where(AuditLog.action == "ie.talent.contacts-read")
            )
        )
        == []
    )


async def test_revoking_last_contact_keeps_profile_visible_and_empty_authorization(
    client, db_session, clock
):
    _, headers, row = await published(
        client,
        db_session,
        clock,
        {"contacts": [{"channel": "WECHAT", "value": "demo_contact"}]},
    )
    _, viewer = await account(db_session, clock)
    response = await client.post(
        BASE + "/revoke-contact",
        headers=headers,
        json={**command(row), "channel": "WECHAT"},
    )
    current = response.json()["current"]
    path = PUBLIC + "/" + row["id"]
    detail = await client.get(path, headers=viewer)
    assert detail.status_code == 200 and detail.json()["contact_channels"] == []
    contacts = await client.post(
        path + "/contacts",
        headers=viewer,
        json={"expected_public_version": current["public_version"]},
    )
    assert contacts.status_code == 200 and contacts.json()["contacts"] == []
