# backend/app/workers/jobs/health.py
"""Foundation job demonstrating the worker pattern (§12 of
docs/quality/backend-engineering.md).

A job is an orchestration shell: it (1) receives IDs and parameters,
(2) constructs its dependencies, (3) calls a service, (4) logs or returns
the result. Workers must not carry an alternate version of domain rules
(design spec §3: Worker must not bypass the Domain/Service layers).

Correlation (§15): jobs RECEIVE `request_id` as an explicit Celery task
argument and thread it through unchanged — they never re-derive or
regenerate it. Celery retries re-invoke the task with the same arguments,
so every retry attempt of one logical operation runs and logs under the
same request_id; the job and the service it calls must therefore be
idempotent so re-execution is safe.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def run_health_check(request_id: str) -> dict[str, Any]:
    """Service call behind `health_job` — the "call a service" step.

    Stand-in for a real application service: once services exist, the job
    constructs its dependencies (session factory, adapters from
    `app.integrations`) and calls the service method. The result here is
    deliberately trivial because this job only proves the wiring and the
    request-id threading.
    """
    return {"ok": True, "request_id": request_id}


@shared_task(bind=True, name="workers.health_job")  # type: ignore[untyped-decorator]
def health_job(self: Any, request_id: str) -> dict[str, Any]:
    """Orchestration shell: params in -> dependencies -> service -> result out.

    `request_id` arrives as a task argument from the caller and is passed
    to the service unchanged; retries reuse it because Celery retries
    replay the original arguments.
    """
    job_id = self.request.id
    logger.info(
        "health_job.start",
        extra={"request_id": request_id, "job_id": job_id},
    )
    result = run_health_check(request_id)
    logger.info(
        "health_job.end",
        extra={"request_id": request_id, "job_id": job_id, "ok": result["ok"]},
    )
    return result
