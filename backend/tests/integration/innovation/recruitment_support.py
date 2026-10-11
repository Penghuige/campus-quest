"""Real sessions and private project fixtures for R3 tests."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.core.clock import FrozenClock
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from tests.integration.innovation.test_achievement_review_operations import account


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
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def fields():
    return {
        "project_title": "模拟学习项目",
        "direction": "学习",
        "stage": "构想",
        "introduction": "模拟资料：计划开展学习实验",
        "idea_stage": True,
        "goal": "验证学习方法",
        "initial_plan": "先做访谈",
        "role_name": "研究伙伴",
        "headcount": 1,
        "duties": "参与访谈",
        "requirements": "愿意学习",
        "time_commitment": "每周两小时",
        "beginner_friendly": True,
        "deadline_at": None,
        "contacts": [{"channel": "WECHAT", "value": "demo_partner"}],
    }


async def world(db, clock, *, qualified=True):
    user, headers = await account(db, clock)
    if qualified:
        db.add(
            OwnerQualification(
                user_id=user.id,
                status="APPROVED",
                version=1,
                profile_version=1,
                name="模拟负责人",
                student_no="123456",
                major="示例",
                grade="2026",
                requested_at=clock.now(),
                approved_at=clock.now(),
                approved_by=user.id,
            )
        )
    project = ProjectDraft(
        owner_user_id=user.id,
        creation_request_id=uuid4(),
        creation_payload_fingerprint="a" * 64,
        title="私有项目",
        summary="PRIVATE_PROJECT_SENTINEL",
        direction="",
        stage="",
        team_status="",
        version=1,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    db.add(project)
    await db.flush()
    return (
        user,
        project,
        headers,
        f"/api/v1/ie/me/project-drafts/{project.id}/recruitments",
    )


async def created(client, db, clock, **options):
    user, project, headers, base = await world(db, clock, **options)
    response = await client.post(
        base, headers=headers, json={"request_id": str(uuid4()), "fields": fields()}
    )
    assert response.status_code == 201, response.text
    row = response.json()
    return user, project, headers, f"{base}/{row['id']}", row


def command(row):
    return {"version": row["version"], "request_id": str(uuid4())}
