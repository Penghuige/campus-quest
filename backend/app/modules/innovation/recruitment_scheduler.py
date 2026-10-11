"""Clock-based recruitment maintenance; jobs only discover and orchestrate."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from app.modules.innovation.public_recruitment_service import PublicRecruitmentService
from app.modules.innovation.recruitment_models import (
    RecruitmentDraft,
    RecruitmentRevision,
)
from app.modules.innovation.recruitment_service import (
    REMINDER_PERIOD,
    RecruitmentService,
    mutation,
)
from app.modules.notifications.enums import NotificationEventType
from app.modules.notifications.port import NotificationPort


def next_reminder_due(due: datetime, now: datetime) -> datetime:
    if now < due:
        return due
    return due + ((now - due) // REMINDER_PERIOD + 1) * REMINDER_PERIOD


@dataclass(frozen=True)
class RecruitmentScanResult:
    expired: int
    reminded: int
    skipped: int


class RecruitmentScheduler:
    def __init__(
        self, *, clock: Clock, notifications: NotificationPort | None = None
    ) -> None:
        self._clock = clock
        self._notifications = notifications or NotificationPort(clock=clock)
        self._commands = RecruitmentService(clock=clock)

    async def _lock(
        self, db: AsyncSession, recruitment_id: UUID
    ) -> RecruitmentDraft | None:
        parent = (
            await db.execute(
                select(ProjectDraft.id, ProjectDraft.owner_user_id)
                .join(RecruitmentDraft, RecruitmentDraft.project_id == ProjectDraft.id)
                .where(RecruitmentDraft.id == recruitment_id)
            )
        ).one_or_none()
        if parent is None:
            return None
        # Same user -> qualification -> project -> recruitment order as commands.
        # Expiry remains valid even when the owner is no longer active.
        await db.execute(
            select(User.__table__.c.id)
            .where(User.__table__.c.id == parent.owner_user_id)
            .with_for_update()
        )
        await db.execute(
            select(OwnerQualification)
            .where(OwnerQualification.user_id == parent.owner_user_id)
            .with_for_update()
        )
        await db.execute(
            select(ProjectDraft).where(ProjectDraft.id == parent.id).with_for_update()
        )
        return cast(
            RecruitmentDraft | None,
            await db.scalar(
                select(RecruitmentDraft)
                .where(RecruitmentDraft.id == recruitment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    async def _effective(self, db: AsyncSession, recruitment_id: UUID) -> bool:
        return (
            await db.execute(
                PublicRecruitmentService(clock=self._clock)
                .query()
                .where(RecruitmentDraft.id == recruitment_id)
            )
        ).one_or_none() is not None

    async def _audit(
        self, db: AsyncSession, row: RecruitmentDraft, action: str
    ) -> None:
        owner_id = await db.scalar(
            select(ProjectDraft.owner_user_id).where(ProjectDraft.id == row.project_id)
        )
        assert owner_id is not None
        await self._commands.audit(
            db,
            actor=Actor(user_id=owner_id, role=Role.STUDENT),
            row=row,
            action=action,
            context=AuditContext(None, None),
        )

    async def expire_if_due(self, db: AsyncSession, *, recruitment_id: UUID) -> bool:
        async with mutation(db):
            row = await self._lock(db, recruitment_id)
            if (
                row is None
                or row.status != "OPEN"
                or row.deadline_at is None
                or self._clock.now() < row.deadline_at
            ):
                return False
            row.status = "EXPIRED"
            row.version += 1
            row.updated_at = self._clock.now()
            await self._audit(db, row, "expire")
            return True

    async def remind_if_due(self, db: AsyncSession, *, recruitment_id: UUID) -> bool:
        async with mutation(db):
            row = await self._lock(db, recruitment_id)
            now = self._clock.now()
            if (
                row is None
                or row.deadline_at is not None
                or row.next_remind_at is None
                or now < row.next_remind_at
                or row.cycle_id is None
                or not await self._effective(db, recruitment_id)
            ):
                return False
            revision = await db.get(RecruitmentRevision, row.public_revision_id)
            assert revision is not None
            due = row.next_remind_at.astimezone(UTC).isoformat()
            await self._notifications.record_event(
                db,
                event_key=f"ie-recruitment:{row.id}:{row.cycle_id}:{due}",
                event_type=NotificationEventType.IE_RECRUITMENT_CONFIRMATION_DUE,
                user_id=(
                    await db.execute(
                        select(ProjectDraft.owner_user_id).where(
                            ProjectDraft.id == row.project_id
                        )
                    )
                ).scalar_one(),
                payload={
                    "role_name": revision.content["role_name"],
                    "manage_path": (
                        f"/profile/project-drafts/{row.project_id}/recruitments"
                    ),
                    "recruitment_id": str(row.id),
                    "cycle_id": str(row.cycle_id),
                    "reminder_due_at": due,
                },
            )
            row.next_remind_at = next_reminder_due(row.next_remind_at, now)
            row.version += 1
            row.updated_at = now
            await self._audit(db, row, "remind")
            return True

    async def is_current_reminder(
        self, db: AsyncSession, *, recruitment_id: UUID, cycle_id: UUID
    ) -> bool:
        async with mutation(db):
            return await self.lock_current_reminder(
                db, recruitment_id=recruitment_id, cycle_id=cycle_id
            )

    async def lock_current_reminder(
        self, db: AsyncSession, *, recruitment_id: UUID, cycle_id: UUID
    ) -> bool:
        """Hold scope locks until the caller commits the visibility decision.

        Dispatch owns this transaction; checking must not commit independently.
        Acquire domain locks before notification locks, as intent creation does.
        """
        row = await self._lock(db, recruitment_id)
        return bool(
            row is not None
            and row.cycle_id == cycle_id
            and row.deadline_at is None
            and await self._effective(db, recruitment_id)
        )

    async def scan_due(
        self, db: AsyncSession, *, limit: int, request_id: str
    ) -> RecruitmentScanResult:
        if limit < 1:
            raise ValueError("limit must be positive")
        now = self._clock.now()
        visible_ids = (
            PublicRecruitmentService(clock=self._clock)
            .query()
            .with_only_columns(RecruitmentDraft.id)
        )
        async with mutation(db):
            ids = list(
                await db.scalars(
                    select(RecruitmentDraft.id)
                    .where(
                        RecruitmentDraft.status == "OPEN",
                        or_(
                            and_(
                                RecruitmentDraft.deadline_at.is_not(None),
                                RecruitmentDraft.deadline_at <= now,
                            ),
                            and_(
                                RecruitmentDraft.deadline_at.is_(None),
                                RecruitmentDraft.next_remind_at <= now,
                                RecruitmentDraft.id.in_(visible_ids),
                            ),
                        ),
                    )
                    .order_by(RecruitmentDraft.updated_at, RecruitmentDraft.id)
                    .limit(limit)
                )
            )
        expired = reminded = 0
        for recruitment_id in ids:
            if await self.expire_if_due(db, recruitment_id=recruitment_id):
                expired += 1
            elif await self.remind_if_due(db, recruitment_id=recruitment_id):
                reminded += 1
        return RecruitmentScanResult(expired, reminded, len(ids) - expired - reminded)
