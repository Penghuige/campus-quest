"""Bounded recruitment scan; domain transactions own every state change."""

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy.exc import DBAPIError, OperationalError

from app.workers.session_source import run_with_session

logger = logging.getLogger(__name__)


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.schedule_recruitments",
    autoretry_for=(OperationalError, DBAPIError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def schedule_recruitments(self: Any, request_id: str) -> dict[str, int]:
    from app.core.clock import SystemClock
    from app.core.config import get_settings
    from app.modules.innovation.recruitment_scheduler import RecruitmentScheduler

    async def scan(db: Any) -> Any:
        return await RecruitmentScheduler(clock=SystemClock()).scan_due(
            db, limit=get_settings().recruitment_scan_batch_limit, request_id=request_id
        )

    result = asdict(asyncio.run(run_with_session(scan)))
    logger.info(
        "schedule_recruitments.end",
        extra={**result, "request_id": request_id, "job_id": self.request.id},
    )
    return result
