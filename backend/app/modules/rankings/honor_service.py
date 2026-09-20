# backend/app/modules/rankings/honor_service.py
"""Fixed-rule honor evaluation, grants, and display selection (spec §18;
plan 05 task 7).

The first-version auto honor types are FROZEN RULES, not configurable
data: ``FIXED_HONOR_DEFINITIONS`` is the catalog, ``evaluate_honors`` is
the matcher-plus-grant. The frozen rule set (spec §18 example list):

- TOTAL_COMPLETED 1 / 10 / 50 completed claims (首次完成 / 累计 10 /
  累计 50);
- ON_TIME_STREAK 10 consecutive on-time completions;
- DAILY_RANK rank == 1 for the day (今日卷王, period YYYY-MM-DD);
- MONTHLY_RANK rank == 1 (本月卷王) and rank <= 3 (月度 Top 3), period
  YYYY-MM;
- TOTAL_EARNED_POINTS 500 / 2000 / 10000 cumulative earned points — a
  documented first-version ruling (the spec names the type, not the
  thresholds; 500 is reachable within a term, 10000 is a long-run
  ceiling).

Facts, not deltas: the event names the trigger (claim_completed /
points_changed / daily_rank_known / monthly_rank_known); lifetime facts
(completed count, on-time streak, earned points) are recomputed from
the claim and ledger tables inside this service, READ-ONLY, through the
sanctioned cross-module seams (rankings already reads ``PointsLedger``
in ``repository.py``; claim reads mirror the ``_USERS_LOCK`` light-table
precedent). Because every lifetime rule is "current fact >= threshold",
a lost trigger heals on any later evaluation — no running totals to
drift. Rank facts (rank + period) ride on the event because they are
projection knowledge only the caller has.

On-time semantics (spec §19): a completed claim counts as on-time iff
its FINAL valid reward lock opened from a submission with
``submitted_at <= deadline_at``. The claim's ``reward_tier_locked``
projection is exactly that judgment: the §9.3 ladder awards tier 100
only on the on-time arm, and the §11.3 re-lock clamp forces a re-locked
claim to <= 20, so ``reward_tier_locked == 100`` is a faithful proxy
computed from one column.

ONE HONOR ROW PER PERIOD: periodic honors (DAILY_RANK/MONTHLY_RANK)
create their own Honor row per (definition, period) — lazily, inside
``_ensure_definition_row`` — because ``user_honors`` is
UNIQUE(user_id, honor_id) and "the September top-3" must not collide
with October's. The partial unique indexes on ``honors`` arbitrate the
create race; a loser adopts the winner's row. Lifetime definition rows
are created the same way, once, with ``period IS NULL``.

Idempotency: re-evaluation never double-grants — the friendly
UserHonor pre-check makes the common replay a no-op, and
UNIQUE(user_id, honor_id) inside a savepoint absorbs the concurrent
race (both sides of the spec §32 retry discipline).

Display honor (spec §18: 只能设置一个 display_honor_id):
``set_display_honor`` requires the user to OWN the honor (a UserHonor
row) and stores it on ``users.display_honor_id`` (migration 0008).
``None`` clears the choice. The users write goes through a typed Core
light table — rankings may not import identity ORM models (interfaces.md
"Identity directory"), the same seam discipline as the claim service's
``_USERS_LOCK`` reads.

Admin commemorative honors (spec §18: Admin 可以人工创建纪念 Honor,
不允许该操作自动篡改排行榜积分): ``create_commemorative_honor`` /
``grant_commemorative_honor`` are admin-only (RBAC on the Actor) and
touch ONLY ``honors`` / ``user_honors`` — by construction no code path
here writes ``points_ledger`` or Redis, so a commemorative grant cannot
alter ranking points (the integration test pins the ledger row count).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Uuid, column, func, select, table, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.events import Actor
from app.modules.rankings.honor_models import Honor, HonorType, UserHonor
from app.modules.rankings.repository import RankingRepository
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import AssignmentClaim

__all__ = [
    "CommemorativeAdminOnlyError",
    "CommemorativeHonorRequiredError",
    "FIXED_HONOR_DEFINITIONS",
    "HonorDefinition",
    "HonorEvaluationEvent",
    "HonorFacts",
    "HonorNotFoundError",
    "HonorNotOwnedError",
    "HonorService",
    "HonorTrigger",
    "UserNotFoundError",
    "matching_honor_definitions",
]


# --- the frozen rule vocabulary -------------------------------------------------------


class HonorTrigger(StrEnum):
    """What happened when an evaluation was requested.

    The two lifetime triggers both recompute every lifetime fact: the
    trigger records which facts PLAUSIBLY changed, not which honors may
    be awarded. The rank triggers carry their rank + period on the
    event (projection knowledge the caller holds).
    """

    CLAIM_COMPLETED = "claim_completed"
    DAILY_RANK_KNOWN = "daily_rank_known"
    MONTHLY_RANK_KNOWN = "monthly_rank_known"
    POINTS_CHANGED = "points_changed"


_LIFETIME_TRIGGERS: frozenset[HonorTrigger] = frozenset(
    {HonorTrigger.CLAIM_COMPLETED, HonorTrigger.POINTS_CHANGED}
)
_RANK_TRIGGERS: frozenset[HonorTrigger] = frozenset(
    {HonorTrigger.DAILY_RANK_KNOWN, HonorTrigger.MONTHLY_RANK_KNOWN}
)

# Period shapes (rankings/periods.py key grammar): daily YYYY-MM-DD,
# monthly YYYY-MM.
_DAILY_PERIOD = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTHLY_PERIOD = re.compile(r"\d{4}-\d{2}")

# Spec §19 on-time judgment expressed as the claim's lock tier (see
# module docstring): the §9.3 ladder's 100% arm is exactly
# submitted_at <= deadline_at.
_ON_TIME_TIER = 100


@dataclass(frozen=True, slots=True)
class HonorEvaluationEvent:
    """One evaluation request: the trigger plus its facts.

    Rank triggers REQUIRE ``rank`` (>= 1) and a well-formed ``period``
    (``YYYY-MM-DD`` for daily, ``YYYY-MM`` for monthly — the same key
    grammar as the Redis boards); lifetime triggers reject both, so a
    malformed event fails at construction instead of granting garbage.
    """

    trigger: HonorTrigger
    rank: int | None = None
    period: str | None = None

    def __post_init__(self) -> None:
        if self.trigger in _LIFETIME_TRIGGERS:
            if self.rank is not None:
                raise ValueError(
                    f"trigger {self.trigger.value} carries no rank fact"
                )
            if self.period is not None:
                raise ValueError(
                    f"trigger {self.trigger.value} carries no period fact"
                )
            return
        if self.trigger not in _RANK_TRIGGERS:  # pragma: no cover - exhaustiveness
            raise ValueError(f"unknown trigger {self.trigger!r}")
        if self.rank is None:
            raise ValueError(f"trigger {self.trigger.value} requires the rank fact")
        if self.rank < 1:
            raise ValueError(f"rank must be >= 1, got {self.rank}")
        if self.period is None:
            raise ValueError(f"trigger {self.trigger.value} requires the period fact")
        pattern = (
            _DAILY_PERIOD
            if self.trigger is HonorTrigger.DAILY_RANK_KNOWN
            else _MONTHLY_PERIOD
        )
        if pattern.fullmatch(self.period) is None:
            raise ValueError(
                f"period {self.period!r} is not a "
                f"{'YYYY-MM-DD' if pattern is _DAILY_PERIOD else 'YYYY-MM'} key"
            )


@dataclass(frozen=True, slots=True)
class HonorFacts:
    """The lifetime fact state an evaluation is judged against."""

    completed_count: int = 0
    on_time_streak: int = 0
    earned_points: int = 0


@dataclass(frozen=True, slots=True)
class HonorDefinition:
    """One fixed-rule catalog entry (code-owned, never a table row).

    ``threshold`` is the lifetime cutoff (count / streak / points);
    ``best_rank`` the periodic cutoff (qualify while event rank <= it).
    Exactly one is set; ``periodic`` marks the rank-based definitions
    whose Honor rows carry a period.
    """

    honor_type: HonorType
    name: str
    description: str
    threshold: int | None = None
    best_rank: int | None = None
    periodic: bool = False


FIXED_HONOR_DEFINITIONS: tuple[HonorDefinition, ...] = (
    HonorDefinition(
        honor_type=HonorType.TOTAL_COMPLETED,
        name="首次完成任务",
        description="完成了第一个任务。",
        threshold=1,
    ),
    HonorDefinition(
        honor_type=HonorType.TOTAL_COMPLETED,
        name="累计完成 10 个任务",
        description="累计完成了 10 个任务。",
        threshold=10,
    ),
    HonorDefinition(
        honor_type=HonorType.TOTAL_COMPLETED,
        name="累计完成 50 个任务",
        description="累计完成了 50 个任务。",
        threshold=50,
    ),
    HonorDefinition(
        honor_type=HonorType.ON_TIME_STREAK,
        name="连续 10 个任务按时",
        description="连续 10 个任务在截止时间前完成。",
        threshold=10,
    ),
    HonorDefinition(
        honor_type=HonorType.DAILY_RANK,
        name="今日卷王",
        description="单日排行榜第一名。",
        best_rank=1,
        periodic=True,
    ),
    HonorDefinition(
        honor_type=HonorType.MONTHLY_RANK,
        name="本月卷王",
        description="月度排行榜第一名。",
        best_rank=1,
        periodic=True,
    ),
    HonorDefinition(
        honor_type=HonorType.MONTHLY_RANK,
        name="月度 Top 3",
        description="月度排行榜前三名。",
        best_rank=3,
        periodic=True,
    ),
    HonorDefinition(
        honor_type=HonorType.TOTAL_EARNED_POINTS,
        name="累计获得 500 积分",
        description="任务贡献累计获得 500 积分。",
        threshold=500,
    ),
    HonorDefinition(
        honor_type=HonorType.TOTAL_EARNED_POINTS,
        name="累计获得 2000 积分",
        description="任务贡献累计获得 2000 积分。",
        threshold=2000,
    ),
    HonorDefinition(
        honor_type=HonorType.TOTAL_EARNED_POINTS,
        name="累计获得 10000 积分",
        description="任务贡献累计获得 10000 积分。",
        threshold=10_000,
    ),
)


def matching_honor_definitions(
    event: HonorEvaluationEvent, facts: HonorFacts
) -> tuple[HonorDefinition, ...]:
    """The pure matcher: which catalog definitions the event + fact
    state qualifies for right now (threshold rules are inclusive from
    current facts, so missed intermediate events self-heal)."""
    if event.trigger in _LIFETIME_TRIGGERS:
        return tuple(
            definition
            for definition in FIXED_HONOR_DEFINITIONS
            if definition.threshold is not None
            and (
                (
                    definition.honor_type is HonorType.TOTAL_COMPLETED
                    and facts.completed_count >= definition.threshold
                )
                or (
                    definition.honor_type is HonorType.ON_TIME_STREAK
                    and facts.on_time_streak >= definition.threshold
                )
                or (
                    definition.honor_type is HonorType.TOTAL_EARNED_POINTS
                    and facts.earned_points >= definition.threshold
                )
            )
        )
    wanted = (
        HonorType.DAILY_RANK
        if event.trigger is HonorTrigger.DAILY_RANK_KNOWN
        else HonorType.MONTHLY_RANK
    )
    rank = event.rank
    assert rank is not None  # construction guarantee
    return tuple(
        definition
        for definition in FIXED_HONOR_DEFINITIONS
        if definition.honor_type is wanted
        and definition.best_rank is not None
        and rank <= definition.best_rank
    )


# --- typed exceptions (router-mapped) -------------------------------------------------

_USER_NOT_FOUND_MESSAGE = "用户不存在"
_HONOR_NOT_FOUND_MESSAGE = "荣誉不存在"
_HONOR_NOT_OWNED_MESSAGE = "只能选择已获得的荣誉作为展示荣誉"
_ADMIN_ONLY_MESSAGE = "只有管理员可以创建或发放纪念荣誉"
_COMMEMORATIVE_REQUIRED_MESSAGE = "只能人工发放纪念荣誉"

# Lock/verify seam for the users table (interfaces.md: rankings may not
# import identity ORM models): the two columns the display-honor write
# touches ride this typed Core light table, same shape as the claim
# service's _USERS_LOCK.
_USERS_DISPLAY = table(
    "users",
    column("id", Uuid),
    column("display_honor_id", Uuid),
)


class UserNotFoundError(BusinessError):
    """No User row for the id (same shape as the claim service's)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _USER_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"user_id": str(user_id)},
        )


class HonorNotFoundError(BusinessError):
    """No Honor row for the id."""

    def __init__(self, honor_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _HONOR_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"honor_id": str(honor_id)},
        )


class HonorNotOwnedError(BusinessError):
    """The user tried to display a honor they do not own (spec §18)."""

    def __init__(self, honor_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _HONOR_NOT_OWNED_MESSAGE,
            status_code=400,
            details={"honor_id": str(honor_id)},
        )


class CommemorativeAdminOnlyError(BusinessError):
    """A non-admin tried to create or grant a commemorative honor."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _ADMIN_ONLY_MESSAGE,
            status_code=403,
        )


class CommemorativeHonorRequiredError(BusinessError):
    """Manual grant attempted on a fixed-rule (auto) honor: those are
    evaluate_honors territory only."""

    def __init__(self, honor_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _COMMEMORATIVE_REQUIRED_MESSAGE,
            status_code=400,
            details={"honor_id": str(honor_id)},
        )


# --- the service ----------------------------------------------------------------------


class HonorService:
    """``evaluate_honors`` / ``set_display_honor`` / the admin
    commemorative pair; every write joins the caller's transaction
    (``session`` in, answers out)."""

    def __init__(self, repository: RankingRepository | None = None) -> None:
        self._repository: RankingRepository = (
            repository if repository is not None else RankingRepository()
        )

    # -- evaluation -------------------------------------------------------------------

    async def evaluate_honors(
        self, session: AsyncSession, user_id: UUID, event: HonorEvaluationEvent
    ) -> list[UserHonor]:
        """Grant every fixed-rule honor the event + current facts qualify
        for; returns only the NEWLY granted rows (a replay yields [])."""
        await self._require_user(session, user_id)
        facts = HonorFacts()
        if event.trigger in _LIFETIME_TRIGGERS:
            facts = HonorFacts(
                completed_count=await self._completed_count(session, user_id),
                on_time_streak=await self._on_time_streak(session, user_id),
                earned_points=await self._repository.user_score(
                    session, user_id, None, None
                ),
            )
        granted: list[UserHonor] = []
        for definition in matching_honor_definitions(event, facts):
            honor = await self._ensure_definition_row(
                session, definition, event.period
            )
            user_honor = await self._grant_once(session, user_id, honor)
            if user_honor is not None:
                granted.append(user_honor)
        return granted

    async def _completed_count(
        self, session: AsyncSession, user_id: UUID
    ) -> int:
        """Total COMPLETED claims (spec §19 累计完成任务数口径)."""
        stmt = (
            select(func.count())
            .select_from(AssignmentClaim)
            .where(
                AssignmentClaim.user_id == user_id,
                AssignmentClaim.status == ClaimStatus.COMPLETED.value,
            )
        )
        return int(await session.scalar(stmt) or 0)

    async def _on_time_streak(
        self, session: AsyncSession, user_id: UUID
    ) -> int:
        """Current consecutive on-time completions, newest first
        (spec §19 当前连续按时数): walk the COMPLETED claims from the most
        recent and stop at the first non-tier-100 one."""
        stmt = (
            select(AssignmentClaim.reward_tier_locked)
            .where(
                AssignmentClaim.user_id == user_id,
                AssignmentClaim.status == ClaimStatus.COMPLETED.value,
            )
            .order_by(
                func.coalesce(
                    AssignmentClaim.terminal_at, AssignmentClaim.claimed_at
                ).desc()
            )
        )
        streak = 0
        for tier in (await session.execute(stmt)).scalars():
            if tier != _ON_TIME_TIER:
                break
            streak += 1
        return streak

    async def _ensure_definition_row(
        self,
        session: AsyncSession,
        definition: HonorDefinition,
        period: str | None,
    ) -> Honor:
        """The (lazily created) Honor row backing one catalog definition —
        per period for the periodic types, once for lifetime types. The
        partial unique index arbitrates a create race; the loser adopts
        the winner's row."""
        period_value = period if definition.periodic else None
        existing = await self._find_definition_row(
            session, definition, period_value
        )
        if existing is not None:
            return existing
        honor = Honor(
            honor_type=definition.honor_type.value,
            name=definition.name,
            description=definition.description,
            period=period_value,
            is_auto=True,
        )
        try:
            async with session.begin_nested():
                session.add(honor)
                await session.flush()
            return honor
        except IntegrityError:
            adopted = await self._find_definition_row(
                session, definition, period_value
            )
            if adopted is None:
                # Not the definition race we meant to absorb.
                raise
            return adopted

    async def _find_definition_row(
        self,
        session: AsyncSession,
        definition: HonorDefinition,
        period: str | None,
    ) -> Honor | None:
        stmt = select(Honor).where(
            Honor.honor_type == definition.honor_type.value,
            Honor.name == definition.name,
            Honor.is_auto.is_(True),
        )
        if period is None:
            stmt = stmt.where(Honor.period.is_(None))
        else:
            stmt = stmt.where(Honor.period == period)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _grant_once(
        self, session: AsyncSession, user_id: UUID, honor: Honor
    ) -> UserHonor | None:
        """Insert the UserHonor unless it already exists; ``None`` means
        "already granted" (the friendly replay path). UNIQUE(user_id,
        honor_id) inside a savepoint absorbs the concurrent race."""
        existing = await session.scalar(
            select(UserHonor)
            .where(UserHonor.user_id == user_id, UserHonor.honor_id == honor.id)
            .limit(1)
        )
        if existing is not None:
            return None
        user_honor = UserHonor(user_id=user_id, honor_id=honor.id)
        try:
            async with session.begin_nested():
                session.add(user_honor)
                await session.flush()
            return user_honor
        except IntegrityError:
            # A concurrent evaluation granted first; the row exists, so
            # this call grants nothing new.
            return None

    async def _require_user(self, session: AsyncSession, user_id: UUID) -> None:
        if (
            await session.scalar(
                select(_USERS_DISPLAY.c.id).where(
                    _USERS_DISPLAY.c.id == user_id
                )
            )
            is None
        ):
            raise UserNotFoundError(user_id)

    # -- display selection ------------------------------------------------------------

    async def set_display_honor(
        self, session: AsyncSession, user_id: UUID, honor_id: UUID | None
    ) -> None:
        """Set the user's single display honor, or clear it with ``None``
        (spec §18). The honor must be OWNED — a UserHonor row must exist
        — or the choice is rejected."""
        await self._require_user(session, user_id)
        if honor_id is None:
            await session.execute(
                update(_USERS_DISPLAY)
                .where(_USERS_DISPLAY.c.id == user_id)
                .values(display_honor_id=None)
            )
            return
        owned = await session.scalar(
            select(UserHonor.id)
            .where(UserHonor.user_id == user_id, UserHonor.honor_id == honor_id)
            .limit(1)
        )
        if owned is None:
            raise HonorNotOwnedError(honor_id)
        await session.execute(
            update(_USERS_DISPLAY)
            .where(_USERS_DISPLAY.c.id == user_id)
            .values(display_honor_id=honor_id)
        )

    # -- admin commemorative honors ---------------------------------------------------

    async def create_commemorative_honor(
        self,
        session: AsyncSession,
        actor: Actor,
        *,
        name: str,
        description: str | None,
    ) -> Honor:
        """Admin-only creation of a纪念 honor (spec §18). Writes exactly
        one ``honors`` row (is_auto = false, COMMEMORATIVE, no period);
        ranking points are untouched by construction."""
        self._require_admin(actor)
        honor = Honor(
            honor_type=HonorType.COMMEMORATIVE.value,
            name=name,
            description=description,
            period=None,
            is_auto=False,
        )
        session.add(honor)
        await session.flush()
        return honor

    async def grant_commemorative_honor(
        self,
        session: AsyncSession,
        actor: Actor,
        *,
        user_id: UUID,
        honor_id: UUID,
    ) -> UserHonor:
        """Admin-only grant of a commemorative honor; idempotent (a
        second grant returns the existing UserHonor row)."""
        self._require_admin(actor)
        await self._require_user(session, user_id)
        honor = await session.get(Honor, honor_id)
        if honor is None:
            raise HonorNotFoundError(honor_id)
        if honor.is_auto or honor.honor_type != HonorType.COMMEMORATIVE.value:
            raise CommemorativeHonorRequiredError(honor_id)
        granted = await self._grant_once(session, user_id, honor)
        if granted is not None:
            return granted
        existing = await session.scalar(
            select(UserHonor)
            .where(UserHonor.user_id == user_id, UserHonor.honor_id == honor_id)
            .limit(1)
        )
        if existing is None:  # pragma: no cover - _grant_once just proved it
            raise HonorNotFoundError(honor_id)
        return existing

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if not is_admin(actor.role):
            raise CommemorativeAdminOnlyError()
