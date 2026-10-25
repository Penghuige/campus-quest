"""A published recruitment must leave no owned rows after real browser teardown."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.clock import SystemClock
from app.modules.audit.context import AuditContext
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from app.modules.innovation.recruitment_models import RecruitmentDraft
from app.modules.innovation.recruitment_schemas import (
    RecruitmentDraftCreate,
    RecruitmentFields,
    RecruitmentVersionCommand,
)
from app.modules.innovation.recruitment_service import RecruitmentService
from tests.e2e import browser_world
from tests.integration.innovation.recruitment_support import fields

pytestmark = pytest.mark.e2e


async def test_cleanup_removes_published_recruitment(db_factory):
    world = await browser_world._seed()
    user_id = UUID(world["student"]["id"])
    clock = SystemClock()
    project_id = uuid4()
    try:
        async with db_factory() as db:
            db.add(
                OwnerQualification(
                    user_id=user_id,
                    status="APPROVED",
                    version=1,
                    profile_version=1,
                    name="模拟负责人",
                    student_no="123456",
                    major="模拟",
                    grade="2026",
                    requested_at=clock.now(),
                    approved_at=clock.now(),
                    approved_by=user_id,
                )
            )
            db.add(
                ProjectDraft(
                    id=project_id,
                    owner_user_id=user_id,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="a" * 64,
                    title="模拟清理项目",
                    summary="",
                    direction="",
                    stage="",
                    team_status="",
                    version=1,
                    created_at=clock.now(),
                    updated_at=clock.now(),
                )
            )
            await db.commit()
            service = RecruitmentService(clock=clock)
            args = {
                "actor": Actor(user_id=user_id, role=Role.STUDENT),
                "project_id": project_id,
                "context": AuditContext(None, None),
            }
            draft, _ = await service.create(
                db,
                payload=RecruitmentDraftCreate(
                    request_id=uuid4(), fields=RecruitmentFields(**fields())
                ),
                **args,
            )
            await service.publish(
                db,
                recruitment_id=draft.id,
                payload=RecruitmentVersionCommand(
                    request_id=uuid4(), version=draft.version
                ),
                **args,
            )
    finally:
        await browser_world._clean(world["world_file"])
    async with db_factory() as db:
        assert await db.get(ProjectDraft, project_id) is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RecruitmentDraft)
                .where(RecruitmentDraft.project_id == project_id)
            )
            == 0
        )
