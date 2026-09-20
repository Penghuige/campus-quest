# backend/app/workers/jobs/validate_submission.py
"""Submission validation job (spec §10 step 8: Worker 异步验证; §32
worker idempotency; docs/quality/backend-engineering.md §12 worker
rules; plan 04 task 7).

An orchestration shell, nothing more: IDs and parameters in ->
construct dependencies -> call ``ValidationService.validate_submission``
-> summary dict out. Every domain rule (the state machine, detection
gates, report assembly) lives in the service; this module must never
grow an alternate version of it (design §3).

Correlation (§15): ``request_id`` arrives as an explicit task argument
and is threaded through unchanged — start/end logs carry it together
with the Celery job id. Celery retries replay the original arguments,
so every attempt of one logical validation logs under the same
request_id; the service is idempotent (terminal-state replay returns
the persisted report, stale VALIDATING is re-run-safe), which is what
makes the bounded autoretry below safe.

Retry policy: storage transients (``OSError`` — including the port's
missing-object FileNotFoundError — and the adapter taxonomy's
temporary/unknown classes) retry with backoff, at most
``max_retries`` times; after that the job fails and the submission
stays VALIDATING, which the next manual or scheduled re-enqueue can
resume. Content-level outcomes (VALIDATED / VALIDATION_FAILED) are
RESULTS, never exceptions.

Import discipline (pinned by tests/workers/test_celery_wiring.py):
importing this module must stay lazy and database-free — the service
and the session factory are imported INSIDE ``run_submission_validation``.
The default per-job engine is created and disposed per invocation
because each job runs ``asyncio.run`` on a fresh event loop; reusing a
process-wide pooled engine across loops would hand asyncpg connections
bound to a dead loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from celery import shared_task  # type: ignore[import-untyped]

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.integrations.errors import TemporaryProviderError, UnknownOutcomeError
from app.integrations.object_storage import ObjectStorage
from app.modules.submissions.validation_runner import SandboxLimits, ValidatorSandbox
from app.modules.submissions.validators.common import PreviewSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The transient classes the bounded autoretry covers (§13 adapter
#: taxonomy: temporary and unknown-outcome; ``OSError`` covers raw
#: storage failures and the port's missing-object FileNotFoundError).
_RETRYABLE_TRANSIENTS = (OSError, TemporaryProviderError, UnknownOutcomeError)

_MAX_RETRIES = 5


def _default_storage() -> ObjectStorage:
    """Storage adapter seam: the real S3/MinIO adapter arrives with the
    plan-07 provider wiring. Until then the job is only reachable with
    an explicitly injected adapter (tests pass ``FakeObjectStorage``);
    failing loudly beats silently talking to nothing."""
    raise NotImplementedError(
        "no production ObjectStorage adapter is wired yet (plan 07 provider "
        "wiring); inject one through run_submission_validation(storage=...)"
    )


def _default_session_source() -> Any:
    """One engine + session per job invocation, disposed afterward.

    A fresh event loop per ``asyncio.run`` cannot safely reuse a
    process-wide engine's pooled connections, so the default source
    builds a private engine and tears it down in a finally.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import create_db_engine

    settings = get_settings()

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        engine = create_db_engine(settings)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                yield session
        finally:
            await engine.dispose()

    return _session()


def _sandbox_from_settings(settings: Settings) -> ValidatorSandbox:
    return ValidatorSandbox(
        limits=SandboxLimits(
            wall_timeout_seconds=float(settings.validation_wall_timeout_seconds),
            memory_limit_bytes=settings.validation_memory_limit_mb * 1024 * 1024,
            cpu_seconds=settings.validation_cpu_seconds,
        )
    )


def run_submission_validation(
    submission_id: str,
    *,
    request_id: str,
    clock: Clock | None = None,
    storage: ObjectStorage | None = None,
    session_source: Any = None,
    sandbox: ValidatorSandbox | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Construct the dependencies and call the service (the §12 shell).

    Every dependency is injectable; each ``None`` falls back to the
    worker default (settings-derived sandbox/preview bounds, system
    clock, per-job session, storage seam). Returns a JSON-serializable
    summary — row data never travels through job results (§15).
    """
    from uuid import UUID as _UUID

    from app.modules.submissions.validation_service import ValidationService

    if settings is None:
        settings = get_settings()
    if clock is None:
        clock = SystemClock()
    if storage is None:
        storage = _default_storage()
    if session_source is None:
        session_source = _default_session_source()
    if sandbox is None:
        sandbox = _sandbox_from_settings(settings)
    service = ValidationService(
        clock=clock,
        storage=storage,
        sandbox=sandbox,
        preview=PreviewSpec(
            max_rows=settings.validation_preview_rows,
            max_value_length=settings.validation_preview_value_chars,
        ),
    )

    async def _call() -> Any:
        async with session_source() as session:
            return await service.validate_submission(session, _UUID(submission_id))

    result = asyncio.run(_call())
    return {
        "submission_id": submission_id,
        "request_id": request_id,
        "validation_status": result.status.value,
        "passed": result.report.passed,
        "row_count": result.report.row_count,
        "parser_version": result.report.parser_version,
        "detected_type": (
            result.detected_type.value if result.detected_type is not None else None
        ),
        "already_terminal": result.already_terminal,
    }


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.validate_submission",
    autoretry_for=_RETRYABLE_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def validate_submission_job(
    self: Any, submission_id: str, request_id: str
) -> dict[str, Any]:
    """Validate one uploaded submission asynchronously.

    Parameters are IDs/correlation strings only; dependencies are
    constructed per invocation inside ``run_submission_validation``.
    Retries replay the same arguments under the same request_id.
    """
    job_id = self.request.id
    logger.info(
        "validate_submission.start",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "submission_id": submission_id,
        },
    )
    result = run_submission_validation(submission_id, request_id=request_id)
    logger.info(
        "validate_submission.end",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "submission_id": submission_id,
            "validation_status": result["validation_status"],
            "already_terminal": result["already_terminal"],
        },
    )
    return result
