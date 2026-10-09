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
    OwnerQualification,
    ProjectDraft,
)
from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementRevision,
    AchievementWorkflow,
    ReviewConflict,
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
            seed.add_all(
                [
                    OwnerQualification(
                        user_id=user_id,
                        status="PENDING",
                        version=1,
                        profile_version=1,
                        name="测试同学",
                        student_no="00123",
                        major="测试",
                        grade="大一",
                        requested_at=datetime.now(UTC),
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
            revisions = [
                AchievementRevision(
                    achievement_id=item.id,
                    number=1,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="0" * 64,
                    operation="SUBMIT",
                    project_version=1,
                    achievement_version=1,
                    project_content={},
                    achievement_content={},
                    owner_profile={},
                    created_at=datetime.now(UTC),
                )
                for item in achievements
            ]
            seed.add_all(revisions)
            await seed.flush()
            seed.add_all(
                [AchievementWorkflow(achievement_id=item.id) for item in achievements]
            )
            seed.add_all(
                [
                    AchievementReviewCase(
                        achievement_id=item.id,
                        revision_id=revision.id,
                        assigned_user_id=user_id,
                        submitted_at=datetime.now(UTC),
                    )
                    for item, revision, user_id in zip(
                        achievements, revisions, user_ids, strict=True
                    )
                ]
            )
            seed.add_all(
                [
                    ReviewConflict(
                        project_id=draft_id,
                        user_id=user_id,
                        created_at=datetime.now(UTC),
                    )
                    for draft_id, user_id in zip(draft_ids, user_ids, strict=True)
                ]
            )
            await seed.commit()

        # clean_world opens and commits another independent session.
        await clean_world(sessions, user_ids=[user_ids[0]])
        async with sessions() as reader:
            assert await reader.get(ProjectDraft, draft_ids[0]) is None
            assert await reader.get(AchievementDraft, achievement_ids[0]) is None
            assert await reader.get(AchievementWorkflow, achievement_ids[0]) is None
            assert await reader.get(AchievementRevision, revisions[0].id) is None
            assert await reader.get(OwnerProfile, user_ids[0]) is None
            assert await reader.get(OwnerQualification, user_ids[0]) is None
            assert await reader.get(OperationsGrant, user_ids[0]) is None
            assert await reader.get(User, user_ids[0]) is None
            assert await reader.get(ProjectDraft, draft_ids[1]) is not None
            assert await reader.get(AchievementDraft, achievement_ids[1]) is not None
            assert await reader.get(AchievementWorkflow, achievement_ids[1]) is not None
            assert await reader.get(AchievementRevision, revisions[1].id) is not None
            assert await reader.get(OwnerProfile, user_ids[1]) is not None
            assert await reader.get(OwnerQualification, user_ids[1]) is not None
            assert await reader.get(OperationsGrant, user_ids[1]) is not None
            assert await reader.get(User, user_ids[1]) is not None
    finally:
        # This independent test fixture removes only its own random seeds,
        # even while the teardown under test is still broken (the red run).
        async with sessions() as cleanup:
            achievement_scope = select(AchievementDraft.id).where(
                AchievementDraft.project_id.in_(
                    select(ProjectDraft.id).where(
                        ProjectDraft.owner_user_id.in_(user_ids)
                    )
                )
            )
            await cleanup.execute(
                delete(AchievementReviewCase).where(
                    AchievementReviewCase.achievement_id.in_(achievement_scope)
                )
            )
            await cleanup.execute(
                delete(AchievementWorkflow).where(
                    AchievementWorkflow.achievement_id.in_(achievement_scope)
                )
            )
            await cleanup.execute(
                delete(AchievementRevision).where(
                    AchievementRevision.achievement_id.in_(achievement_scope)
                )
            )
            await cleanup.execute(
                delete(ReviewConflict).where(
                    ReviewConflict.project_id.in_(
                        select(ProjectDraft.id).where(
                            ProjectDraft.owner_user_id.in_(user_ids)
                        )
                    )
                )
            )
            await cleanup.execute(
                delete(OwnerQualification).where(
                    OwnerQualification.user_id.in_(user_ids)
                )
            )
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


@pytest.mark.parametrize("cross_world", [False, True])
async def test_world_cleanup_handles_approved_qualification_scope(
    db_engine, cross_world
):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    ids = []
    try:
        async with sessions() as seed:
            admin = User(
                username=f"cleanup-qa-{uuid4().hex}",
                password_hash="unused",
                nickname="管理员",
                role="ADMIN",
                status="ACTIVE",
            )
            student = User(
                username=f"cleanup-qs-{uuid4().hex}",
                password_hash="unused",
                nickname="学生",
                role="STUDENT",
                status="ACTIVE",
            )
            seed.add_all([admin, student])
            await seed.flush()
            ids = [admin.id, student.id]
            seed.add(
                OwnerQualification(
                    user_id=ids[1],
                    status="APPROVED",
                    version=2,
                    profile_version=1,
                    name="测试",
                    student_no="001",
                    major="测试",
                    grade="大一",
                    requested_at=datetime.now(UTC),
                    approved_at=datetime.now(UTC),
                    approved_by=ids[0],
                )
            )
            await seed.commit()
        if cross_world:
            with pytest.raises(RuntimeError, match="outside this world"):
                await clean_world(sessions, user_ids=[ids[0]])
            async with sessions() as reader:
                assert await reader.get(User, ids[0]) is not None
                assert await reader.get(User, ids[1]) is not None
                assert await reader.get(OwnerQualification, ids[1]) is not None
        else:
            await clean_world(sessions, user_ids=ids)
            async with sessions() as reader:
                assert await reader.get(OwnerQualification, ids[1]) is None
                assert await reader.get(User, ids[0]) is None
                assert await reader.get(User, ids[1]) is None
    finally:
        await clean_world(sessions, user_ids=ids)


async def test_world_cleanup_rejects_cross_world_grant_before_any_mutation(db_engine):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    ids = []
    try:
        async with sessions() as seed:
            admin = User(
                username=f"cleanup-ga-{uuid4().hex}",
                password_hash="unused",
                nickname="管理员",
                role="ADMIN",
                status="ACTIVE",
            )
            student = User(
                username=f"cleanup-gs-{uuid4().hex}",
                password_hash="unused",
                nickname="学生",
                role="STUDENT",
                status="ACTIVE",
            )
            seed.add_all([admin, student])
            await seed.flush()
            ids = [admin.id, student.id]
            seed.add(
                OperationsGrant(
                    user_id=student.id,
                    enabled=True,
                    version=3,
                    changed_by=admin.id,
                    updated_at=datetime.now(UTC),
                )
            )
            seed.add(
                ProjectDraft(
                    owner_user_id=student.id,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="0" * 64,
                    title="另一个world的项目",
                )
            )
            await seed.commit()
        with pytest.raises(RuntimeError, match="outside this world"):
            await clean_world(sessions, user_ids=[ids[0]])
        async with sessions() as reader:
            assert await reader.get(User, ids[0]) is not None
            assert await reader.get(User, ids[1]) is not None
            grant = await reader.get(OperationsGrant, ids[1])
            assert (
                grant is not None
                and grant.enabled
                and grant.version == 3
                and grant.changed_by == ids[0]
            )
            assert (
                await reader.scalar(
                    select(ProjectDraft.id).where(ProjectDraft.owner_user_id == ids[1])
                )
                is not None
            )
    finally:
        await clean_world(sessions, user_ids=ids)
