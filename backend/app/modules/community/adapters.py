# backend/app/modules/community/adapters.py
"""Community-owned implementations of other modules' read ports.

- ``CommunityRatingSummaryAdapter`` — the concrete ``RatingSummaryPort``
  that plan 03 sketched in ``tasks.query_service`` and shipped as the
  interim ``NullRatingSummaryPort``: the tasks module's statistics read
  needs the TaskRating aggregate but must not import community
  internals, so the community module owns the adapter and hands it
  over. The port is IMPORTED from ``tasks.query_service``, never
  redefined — one Protocol shape, checked structurally by the
  ``TYPE_CHECKING`` conformance pin below; defining a twin interface
  here is the drift bug this file exists to prevent.

  The session arrives at construction, like every per-request-scoped
  port (the query_service docstring's contract), and the adapter holds
  no query logic of its own: it delegates to
  ``RatingService.rating_summary`` so the aggregate the community
  surfaces read and the one task statistics reads can never diverge.
  ``summary`` is read-only and None-safe by delegation (spec §20: 前台
  只展示聚合分和数量 — the adapter answers exactly average + count, or
  None when nobody rated).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.community.rating_service import RatingService
from app.modules.tasks.query_service import RatingSummary, RatingSummaryPort

__all__ = ["CommunityRatingSummaryAdapter"]


class CommunityRatingSummaryAdapter:
    """The TaskRating-backed ``RatingSummaryPort`` (see module docstring);
    construct one per request with that request's session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def summary(self, task_id: UUID) -> RatingSummary | None:
        return await RatingService().rating_summary(self._db, task_id)


if TYPE_CHECKING:
    # Static conformance pin (mypy checks the Protocol structurally at
    # this assignment): the adapter IS the port, or the build fails.
    _PORT_CONFORMANCE: type[RatingSummaryPort] = CommunityRatingSummaryAdapter
