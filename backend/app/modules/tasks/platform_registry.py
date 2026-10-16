# backend/app/modules/tasks/platform_registry.py
"""The platform registry's seed data and query seam (V1.1 spec 2026-10-16).

The seven shipped platforms are the migration's bulk-insert source AND
the constant the tests pin, so the DB and the code cannot drift apart.
Admin-created platforms live ONLY as rows — that is the entire point of
the registry (zero-migration growth).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tasks.models import Platform

#: The shipped platforms (spec §1). Sort order groups the recruitment
#: cluster after the original three; display names are what the student
#: UI renders.
SEED_PLATFORMS: tuple[dict[str, Any], ...] = (
    {"code": "xiaohongshu", "display_name": "小红书", "sort_order": 10},
    {"code": "douyin", "display_name": "抖音", "sort_order": 20},
    {"code": "zhihu", "display_name": "知乎", "sort_order": 30},
    {"code": "boss-zhipin", "display_name": "BOSS直聘", "sort_order": 40},
    {"code": "zhaopin", "display_name": "智联招聘", "sort_order": 50},
    {"code": "58city", "display_name": "58同城", "sort_order": 60},
    {"code": "lietou", "display_name": "猎聘", "sort_order": 70},
)


async def enabled_codes(db: AsyncSession) -> set[str]:
    """Codes currently usable for NEW tasks/imports (spec §1 enabled)."""
    return set(
        await db.scalars(select(Platform.code).where(Platform.enabled.is_(True)))
    )
