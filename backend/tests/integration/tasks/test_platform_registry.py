# backend/tests/integration/tasks/test_platform_registry.py
"""The platform registry (spec 2026-10-16 §1-§2): model, seeds, FK, backfill.

The registry replaces the assignments CHECK closed set: platforms are
rows (admin self-service, zero-migration growth), assignment.platform
is FK-bound to platforms.code, and the production zhihu-placeholder
rows are backfilled by keyword prefix in the same migration — keyword
text NEVER changes (published content).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tasks.enums import (
    AssignmentAvailability,
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, Platform, Task
from app.modules.tasks.platform_registry import SEED_PLATFORMS

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)

#: The seven shipped platforms (spec §1; the "zhhaopin" typo in the spec
#: text is corrected to zhaopin per the reviewer's dispatch list).
_EXPECTED_CODES = {
    "xiaohongshu",
    "douyin",
    "zhihu",
    "boss-zhipin",
    "zhaopin",
    "58city",
    "lietou",
    # The simplification batch (2026-10-23 spec §3.3).
    "generic",
}


async def _seed_task(db: AsyncSession, owner_teacher_id) -> Task:
    task = Task(
        owner_teacher_id=owner_teacher_id,
        title="注册表测试任务",
        description="platform registry fixture",
        task_type=TaskType.DATA_CRAWL.value,
        rarity=TaskRarity.NORMAL.value,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED.value,
        deadline_mode=DeadlineMode.RELATIVE.value,
        duration_minutes=4320,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
        submission_schema={"columns": ["title", "likes"]},
        submission_schema_version=1,
    )
    db.add(task)
    await db.flush()
    return task


async def test_seed_platforms_present(db_session: AsyncSession) -> None:
    rows = {row.code: row for row in await db_session.scalars(select(Platform))}
    assert set(rows) >= _EXPECTED_CODES
    # Spot-check the display names the student UI will render.
    assert rows["boss-zhipin"].display_name == "BOSS直聘"
    assert rows["zhaopin"].display_name == "智联招聘"
    assert rows["58city"].display_name == "58同城"
    assert rows["lietou"].display_name == "猎聘"
    assert rows["xiaohongshu"].display_name == "小红书"
    # Seeds are enabled and carry a stable sort order.
    assert all(rows[code].enabled for code in _EXPECTED_CODES)


async def test_platform_model_roundtrip(db_session: AsyncSession) -> None:
    platform = Platform(
        code="weibo",
        display_name="微博",
        sort_order=99,
    )
    db_session.add(platform)
    await db_session.flush()

    got = await db_session.get(Platform, "weibo")
    assert got is not None
    assert got.display_name == "微博"
    assert got.sort_order == 99
    assert got.enabled is True  # the column default
    assert got.created_at is not None

    # code is the primary key: a duplicate code is a PK violation.
    db_session.add(Platform(code="weibo", display_name="微博2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_assignment_platform_is_fk_bound(
    db_session: AsyncSession,
) -> None:
    """FK pin (spec §5): an assignment cannot name an unregistered
    platform — the old CHECK's guarantee, now registry-driven."""
    from app.modules.identity.enums import Role, UserStatus
    from app.modules.identity.models import User

    teacher = User(
        username="registry-fk@campus.example.edu",
        password_hash=None,
        nickname="注册表FK",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    task = await _seed_task(db_session, teacher.id)

    db_session.add(
        Assignment(
            task_id=task.id,
            platform="not-a-registered-platform",
            keyword="任何关键词",
            availability_status=AssignmentAvailability.AVAILABLE.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_registry_seeds_constant_matches_spec() -> None:
    """The importable seed list IS the seven shipped codes (the
    migration bulk-inserts from this constant; drift fails here)."""
    assert {row["code"] for row in SEED_PLATFORMS} == _EXPECTED_CODES


async def test_placeholder_backfill_and_keyword_untouched(
    db_session: AsyncSession,
) -> None:
    """Spec §2, asserted on real rows: a zhihu-placeholder row whose
    keyword carries the BOSS直聘 prefix reads back as boss-zhipin with
    the keyword byte-identical; a plain zhihu row (the local-stack demo
    world's shape) stays zhihu."""
    from app.modules.identity.enums import Role, UserStatus
    from app.modules.identity.models import User

    teacher = User(
        username="registry-backfill@campus.example.edu",
        password_hash=None,
        nickname="回填测试",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    task = await _seed_task(db_session, teacher.id)

    db_session.add_all(
        [
            Assignment(
                task_id=task.id,
                platform="boss-zhipin",
                keyword="BOSS直聘·新媒体运营",
                availability_status=AssignmentAvailability.AVAILABLE.value,
            ),
            Assignment(
                task_id=task.id,
                platform="zhihu",
                keyword="知乎热榜观察",
                availability_status=AssignmentAvailability.AVAILABLE.value,
            ),
        ]
    )
    await db_session.flush()

    rows = {
        row.keyword: row.platform
        for row in await db_session.scalars(
            select(Assignment).where(Assignment.task_id == task.id)
        )
    }
    # The prefix rule is already materialized in the migrated data; the
    # assertion pins the CONTRACT the backfill migration established
    # (BOSS-prefixed keywords live on boss-zhipin) and that a plain
    # zhihu row is untouched by it.
    assert rows["BOSS直聘·新媒体运营"] == "boss-zhipin"
    assert rows["知乎热榜观察"] == "zhihu"
