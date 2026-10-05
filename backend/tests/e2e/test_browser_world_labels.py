# backend/tests/e2e/test_browser_world_labels.py
"""Plan-12 task 8: CQ_E2E_FIXED_LABELS deterministic world labels.

The Playwright pixel-regression suite (plan-12 task 9) diffs full-page
screenshots across runs, so the seeded browser world's VISIBLE strings
must be byte-identical across seeds. With CQ_E2E_FIXED_LABELS=1,
browser_world._seed freezes task titles, reward names, nicknames AND
the usernames (the admin user table sorts BY username, so row order is
only stable when the sort key is) at a fixed hex marker; unset, every
string keeps embedding the unique run id exactly as before.

These tests drive the REAL _seed/_clean pair (the same entries the
Node global-setup shells) against the shared test stack, seed ->
snapshot -> clean twice per mode:

- fixed mode: two independent runs produce byte-identical label sets,
  and the pinned fixed strings are the documented ones;
- default mode: labels still embed the run id (the pre-flag behavior
  the other suites rely on for leftover tolerance).

Cleanup runs through the real _clean both modes, so the test also
proves teardown behavior is unchanged by the flag.
"""

from __future__ import annotations

import uuid
from typing import TypedDict

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.models import User
from app.modules.points.models import RewardItem
from app.modules.tasks.models import Task
from tests.e2e import browser_world

pytestmark = pytest.mark.e2e

#: The frozen marker from browser_world (hex — the student factories
#: fold it into student numbers and phones).
FIXED = browser_world.FIXED_LABEL_RUN


class WorldLabels(TypedDict):
    usernames: list[str]
    nicknames: list[str]
    task_titles: list[str]
    reward_name: str | None


async def _world_labels(
    db_factory: async_sessionmaker[AsyncSession], world: dict
) -> WorldLabels:
    """Every seeded label/identifier a screenshot could render, snapped
    from the committed rows (sorted so the comparison is order-free —
    the admin table's by-username row order is part of what the fixed
    mode freezes, and fixed strings make that order deterministic)."""
    user_ids = [
        world["student"]["id"],
        world["author"]["id"],
        world["teacher_id"],
        world["admin_id"],
        world["reveal_admin_id"],
        world["browser_teacher_id"],
        world["browser_teacher2_id"],
        world["browser_admin_id"],
        world["redeemer_id"],
        world["suspended_student_id"],
    ]
    async with db_factory() as db:
        users = (
            await db.execute(
                select(User.username, User.nickname).where(
                    User.id.in_([uuid.UUID(u) for u in user_ids])
                )
            )
        ).all()
        tasks = (
            (
                await db.execute(
                    select(Task.title).where(
                        Task.id.in_([uuid.UUID(t) for t in world["task_ids"]])
                    )
                )
            )
            .scalars()
            .all()
        )
        reward_name = await db.scalar(
            select(RewardItem.name).where(
                RewardItem.id == uuid.UUID(world["reward_item_id"])
            )
        )
    return {
        "usernames": sorted(username for username, _ in users),
        "nicknames": sorted(nickname for _, nickname in users),
        "task_titles": sorted(tasks),
        "reward_name": reward_name,
    }


async def test_fixed_labels_byte_identical_across_runs(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CQ_E2E_FIXED_LABELS", "1")
    labels: list[WorldLabels] = []
    runs: list[str] = []
    for _ in range(2):
        world = await browser_world._seed()
        try:
            runs.append(world["run"])
            labels.append(await _world_labels(db_factory, world))
        finally:
            await browser_world._clean(world["world_file"])

    # Two genuinely independent seeds (distinct run ids and row UUIDs)…
    assert runs[0] != runs[1]
    # …produce byte-identical visible labels.
    assert labels[0] == labels[1]

    # The frozen strings are the documented fixed-marker ones. All four
    # seeded task titles are equal (the factories slice the run marker's
    # first 6 chars, so the a/b/c/r suffixes never reach the title) —
    # the fixed mode inherits that shape unchanged.
    assert labels[0]["task_titles"] == [f"端到端数据采集任务{FIXED[:6]}"] * 4
    assert labels[0]["reward_name"] == f"端到端奖励卡{FIXED[:6]}"
    assert f"端到端同学{FIXED[:4]}" in labels[0]["nicknames"]
    assert f"停用目标同学{FIXED[:4]}" in labels[0]["nicknames"]
    assert f"e2e-teacher-{FIXED}@school.edu" in labels[0]["usernames"]


async def test_default_mode_still_embeds_run_id(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CQ_E2E_FIXED_LABELS", raising=False)
    world = await browser_world._seed()
    try:
        run = world["run"]
        labels = await _world_labels(db_factory, world)
        # Default behavior is byte-identical to the pre-flag world: every
        # label family carries THIS run's marker…
        assert labels["task_titles"] == [f"端到端数据采集任务{run[:6]}"] * 4
        assert labels["reward_name"] == f"端到端奖励卡{run[:6]}"
        assert f"端到端同学{run[:4]}" in labels["nicknames"]
        assert f"e2e-teacher-{run}@school.edu" in labels["usernames"]
        # …and never leaks the fixed marker.
        assert not any(FIXED[:6] in title for title in labels["task_titles"])
    finally:
        await browser_world._clean(world["world_file"])
