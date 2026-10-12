"""Real unqualified student sessions; a separate R4 database, no project needed."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.core.clock import FrozenClock
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from tests.integration.innovation.test_achievement_review_operations import account

BASE = "/api/v1/ie/me/talent-profile"


@pytest.fixture
def clock():
    return FrozenClock(datetime.now(UTC))


@pytest.fixture
async def client(db_session, clock):
    app = create_app()

    async def db():
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as http:
        yield http


def fields(tag_id):
    return {
        "name": "模拟新生",
        "faculty": "模拟学院",
        "grade": "2026级",
        "introduction": "模拟资料：希望参加学习实践",
        "advantages": "能制作清晰的PPT，愿意从小任务开始",
        "tag_ids": [str(tag_id)],
        "cooperation_state": "LOOKING",
    }


def command(row):
    return {"request_id": str(uuid4()), "version": row["version"]}


async def saved(client, db, clock, *, patch=None):
    user, headers = await account(db, clock)
    tags = await client.get("/api/v1/ie/talent-tags", headers=headers)
    assert tags.status_code == 200, tags.text
    tag_id = tags.json()["items"][0]["id"]
    payload = {
        "request_id": str(uuid4()),
        "version": 0,
        "fields": fields(tag_id) | (patch or {}),
    }
    response = await client.put(BASE, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return user, headers, response.json()["current"], payload
