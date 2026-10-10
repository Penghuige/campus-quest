# backend/tests/e2e/test_browser_world_mint_clock.py
"""CQ_E2E_FIXED_LABELS also pins the world's MINT clock (root fix for
the cross-period baseline drift).

The plan-12 walkthrough measured the fixed-labels world still drifting
between a baseline capture and a re-shoot (student-rewards ~173k px,
student-dashboard ~48k): the flag froze every rendered STRING but no
timestamp, so date text, review-queue stamps, and deadline snapshots
rode the seed-time wall clock. The fix pins the seed-time now to a
FUTURE anchor (mint_now in factories): future, because the functional
specs sharing this world submit against the seeded claims — a past
anchor would close every submission window against the real clock the
orchestrated app runs on (the pyjwt wall-clock lesson: pin the world,
never the app).

The anchor is pinned HERE as an independent contract (the
contract-freeze discipline, per the test_declared_type_content_types
precedent): the implementation must match, not alias, this constant.

These tests drive the REAL _seed/_clean pair (the entries the Node
global-setup shells) and pin:

- fixed mode: every world-minted timestamp family (task published_at,
  claim claimed_at/deadlines, TOTP confirmed_at, review-queue stamps,
  user created_at) lands on the anchor plus whole one-minute steps,
  strictly increasing along the seed order (a single shared instant
  would tie published_at values and leave the task list to the uuid
  tiebreak), and every stamp stays AHEAD of the real clock;
- the ONE documented exception: seed_points_balance's ledger
  ranking_effective_at stays on the REAL clock — the board window key
  is a storage address the live ranking reads derive from real now, and
  pinning it would empty the dashboard's monthly widget (and the
  month-filtered wallet figures) instead of freezing it;
- two independent fixed-mode seeds produce byte-identical timestamp
  tuples (the drift fix's acceptance);
- default mode keeps the real clock (the pre-flag behavior every other
  e2e module relies on).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.models import TotpCredential, User
from app.modules.points.models import PointsLedger
from app.modules.tasks.models import AssignmentClaim, Task
from tests.e2e import browser_world

pytestmark = pytest.mark.e2e

#: The frozen mint anchor (the contract the implementation must match).
_ANCHOR = datetime(2030, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=1)
#: How close a REAL-clock stamp may sit to this test's own now.
_REAL_CLOCK_TOLERANCE = timedelta(minutes=10)


class WorldStamps(TypedDict):
    published_at: list[datetime]  # task order = world task_ids order
    claimed_at: datetime  # claim_a
    deadline_at: datetime  # claim_a
    grace_deadline_at: datetime  # claim_a
    totp_confirmed_at: datetime  # the world teacher's credential
    reward_locked_at: datetime  # the review queue's UNDER_REVIEW claim
    student_created_at: datetime
    now: datetime  # snapshot instant, for the real-clock assertions


async def _world_stamps(
    db_factory: async_sessionmaker[AsyncSession], world: dict
) -> WorldStamps:
    """Every world-minted timestamp family a screenshot can render,
    snapped from the committed rows in the seed's own order."""
    task_ids = [uuid.UUID(t) for t in world["task_ids"]]
    async with db_factory() as db:
        published_rows = (
            await db.execute(
                select(Task.id, Task.published_at).where(Task.id.in_(task_ids))
            )
        ).all()
        by_id = {row.id: row.published_at for row in published_rows}
        claim_a = await db.get(AssignmentClaim, uuid.UUID(world["claim_a"]))
        assert claim_a is not None
        review_claim = (
            await db.scalars(
                select(AssignmentClaim).where(
                    AssignmentClaim.task_id == uuid.UUID(world["review_task_id"])
                )
            )
        ).one()
        totp = await db.scalar(
            select(TotpCredential.confirmed_at).where(
                TotpCredential.user_id == uuid.UUID(world["teacher_id"])
            )
        )
        student = await db.get(User, uuid.UUID(world["student"]["id"]))
        assert student is not None
    return {
        "published_at": [by_id[task_id] for task_id in task_ids],
        "claimed_at": claim_a.claimed_at,
        "deadline_at": claim_a.deadline_at,
        "grace_deadline_at": claim_a.grace_deadline_at,
        "totp_confirmed_at": totp,
        "reward_locked_at": review_claim.reward_locked_at,
        "student_created_at": student.created_at,
        "now": datetime.now(UTC),
    }


def _assert_on_grid(stamps: list[datetime]) -> None:
    """Every stamp sits on the anchor plus whole one-minute steps, each
    DISTINCT (one step per mint call — the uuid-tiebreak antidote).
    Strict increase is only meaningful within a family that follows the
    seed order (the published_at loop below); cross-family positions
    interleave by construction."""
    offsets = [stamp - _ANCHOR for stamp in stamps]
    for offset in offsets:
        assert offset >= timedelta(0), stamps
        assert offset.total_seconds() % 60 == 0, stamps
    assert len(set(offsets)) == len(offsets), stamps


async def test_fixed_mode_pins_mint_stamps(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CQ_E2E_FIXED_LABELS", "1")
    world = await browser_world._seed()
    try:
        stamps = await _world_stamps(db_factory, world)
        now = stamps["now"]

        # Functional invariant first: the anchor is FUTURE, so seeded
        # claims stay on-time and windows stay open against the real
        # clock the app runs on (the specs sharing this world submit
        # against claim_a for years).
        for stamp in stamps["published_at"] + [
            stamps["claimed_at"],
            stamps["deadline_at"],
            stamps["grace_deadline_at"],
            stamps["totp_confirmed_at"],
            stamps["reward_locked_at"],
            stamps["student_created_at"],
        ]:
            assert stamp > now, stamp

        # The non-published families ride the anchor's stepped sequence…
        _assert_on_grid(
            [
                stamps["claimed_at"],
                stamps["totp_confirmed_at"],
                stamps["reward_locked_at"],
                stamps["student_created_at"],
            ]
        )
        # …while published_at keeps the factory's "mint minus one day"
        # relationship: each is the anchor minus a day plus a whole
        # stepped minute, still increasing along the seed order.
        published_offsets = [
            stamp + timedelta(days=1) for stamp in stamps["published_at"]
        ]
        for mint in published_offsets:
            assert (mint - _ANCHOR) > timedelta(0), stamps["published_at"]
            assert (mint - _ANCHOR) % _STEP == timedelta(0), stamps["published_at"]
        assert published_offsets == sorted(published_offsets), stamps["published_at"]

        # The claim's deadline snapshots derive from its minted
        # claimed_at exactly as compute_claim_deadlines computes them
        # (3-day duration + the pinned V1 24h grace).
        assert stamps["deadline_at"] - stamps["claimed_at"] == timedelta(days=3)
        assert stamps["grace_deadline_at"] - stamps["deadline_at"] == timedelta(days=1)

        # The documented exception: the ledger's ranking effective time
        # rides the REAL clock (the board window is a storage address
        # the live ranking reads derive from now).
        async with db_factory() as db:
            effective_at = await db.scalar(
                select(PointsLedger.ranking_effective_at).where(
                    PointsLedger.user_id == uuid.UUID(world["student"]["id"]),
                    PointsLedger.source_id == uuid.UUID(world["claim_a"]),
                )
            )
        assert effective_at is not None
        assert abs(effective_at - now) < _REAL_CLOCK_TOLERANCE
    finally:
        await browser_world._clean(world["world_file"])


async def test_fixed_mode_mint_stamps_byte_identical_across_runs(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CQ_E2E_FIXED_LABELS", "1")
    runs: list[WorldStamps] = []
    for _ in range(2):
        world = await browser_world._seed()
        try:
            runs.append(await _world_stamps(db_factory, world))
        finally:
            await browser_world._clean(world["world_file"])
    first, second = runs
    assert first["published_at"] == second["published_at"]
    assert first["claimed_at"] == second["claimed_at"]
    assert first["deadline_at"] == second["deadline_at"]
    assert first["grace_deadline_at"] == second["grace_deadline_at"]
    assert first["reward_locked_at"] == second["reward_locked_at"]
    assert first["student_created_at"] == second["student_created_at"]
    assert first["totp_confirmed_at"] == second["totp_confirmed_at"]


async def test_default_mode_keeps_real_clock(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CQ_E2E_FIXED_LABELS", raising=False)
    world = await browser_world._seed()
    try:
        stamps = await _world_stamps(db_factory, world)
        now = stamps["now"]
        # Default mode is byte-identical to the pre-flag world: minted
        # stamps ride the real clock (published yesterday, claimed now)
        # and never reach the anchor.
        for published in stamps["published_at"]:
            assert abs((published + timedelta(days=1)) - now) < _REAL_CLOCK_TOLERANCE
        assert abs(stamps["claimed_at"] - now) < _REAL_CLOCK_TOLERANCE
        assert stamps["claimed_at"] < _ANCHOR
    finally:
        await browser_world._clean(world["world_file"])
