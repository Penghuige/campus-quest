"""The shared e2e teardown owns only the users in its seeded world."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.modules.identity.models import User
from app.modules.innovation.models import (
    AchievementDraft,
    OperationsGrant,
    OwnerProfile,
    ProjectDraft,
)
from tests.e2e.factories import clean_world

pytestmark = pytest.mark.integration


async def test_world_cleanup_removes_owned_drafts_and_preserves_other_world(
    db_engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    user_ids = []
    try:
        async with sessions() as seed:
            users = [
                User(
                    username=f"cleanup-ie-{uuid4().hex}",
                    password_hash="unused-test-password-hash",
                    nickname="清理测试同学",
                    role="STUDENT",
                    status="ACTIVE",
                )
                for _ in range(2)
            ]
            seed.add_all(users)
            await seed.flush()
            user_ids = [user.id for user in users]
            drafts = [
                ProjectDraft(
                    owner_user_id=user_id,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="0" * 64,
                    title="仅本测试种子",
                )
                for user_id in user_ids
            ]
            seed.add_all(drafts)
            seed.add_all(
                [
                    OperationsGrant(
                        user_id=user_id,
                        enabled=True,
                        version=1,
                        changed_by=user_id,
                        updated_at=datetime.now(UTC),
                    )
                    for user_id in user_ids
                ]
            )
            seed.add_all(
                [
                    OwnerProfile(
                        user_id=user_id,
                        name="测试同学",
                        student_no="00123",
                        major="测试",
                        grade="大一",
                    )
                    for user_id in user_ids
                ]
            )
            await seed.flush()
            draft_ids = [draft.id for draft in drafts]
            achievements = [
                AchievementDraft(
                    project_id=draft_id,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="0" * 64,
                    title="世界私有成果",
                )
                for draft_id in draft_ids
            ]
            seed.add_all(achievements)
            await seed.flush()
            achievement_ids = [item.id for item in achievements]
            await seed.commit()

        # clean_world opens and commits another independent session.
        await clean_world(sessions, user_ids=[user_ids[0]])
        async with sessions() as reader:
            assert await reader.get(ProjectDraft, draft_ids[0]) is None
            assert await reader.get(AchievementDraft, achievement_ids[0]) is None
            assert await reader.get(OwnerProfile, user_ids[0]) is None
            assert await reader.get(OperationsGrant, user_ids[0]) is None
            assert await reader.get(User, user_ids[0]) is None
            assert await reader.get(ProjectDraft, draft_ids[1]) is not None
            assert await reader.get(AchievementDraft, achievement_ids[1]) is not None
            assert await reader.get(OwnerProfile, user_ids[1]) is not None
            assert await reader.get(OperationsGrant, user_ids[1]) is not None
            assert await reader.get(User, user_ids[1]) is not None
    finally:
        # This independent test fixture removes only its own random seeds,
        # even while the teardown under test is still broken (the red run).
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AchievementDraft).where(
                    AchievementDraft.project_id.in_(
                        select(ProjectDraft.id).where(
                            ProjectDraft.owner_user_id.in_(user_ids)
                        )
                    )
                )
            )
            await cleanup.execute(
                delete(OperationsGrant).where(OperationsGrant.user_id.in_(user_ids))
            )
            await cleanup.execute(
                delete(OwnerProfile).where(OwnerProfile.user_id.in_(user_ids))
            )
            await cleanup.execute(
                delete(ProjectDraft).where(ProjectDraft.owner_user_id.in_(user_ids))
            )
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()
