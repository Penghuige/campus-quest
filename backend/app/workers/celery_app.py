# backend/app/workers/celery_app.py
"""Celery application wiring (docs/quality/backend-engineering.md §12).

Jobs are orchestration shells that construct dependencies and call
application services; this module only provides the app they run on.

Construction is lazy (same PEP 562 pattern as `app.db.session`):
importing this module never touches the environment. Only building the
app calls `get_settings()`, which reads the required deployment variables
(REDIS_URL among them) and fails fast with a pydantic validation error
naming the missing variable — that is the intended clear-error path for a
misconfigured worker.

Local development uses Redis as both broker and result backend (design
spec §3: Redis carries cache/queue traffic only, never business truth).

The wire format is JSON only: task and result serializers are pinned to
json and `accept_content` rejects every other content type, so no pickle
payload can enter the queue even if a producer tried.

Eager execution (`task_always_eager`) is TEST-ONLY and is never set here;
tests enable it in their fixture. `task_eager_propagates=True` is safe
app-wide because it only changes how eager (inline) executions surface
exceptions.

MERGE CARRIES: the plan-07 merge checklist (the former
app/workers/MERGE_CARRIES.md, deleted once every item closed at the PR
#2 hardening merge — G17) is DONE: the cleanup job registration + beat
schedules (items 3/4), the VALIDATED-reading expiry inspector (item
1), the producer-side NotificationPort wiring (item 2), the expiry-scan
partial index (migration 0013, item 4/N5), the 0010 reparent (item 5,
pre-existing), and the Gate-1 re-validation test (item 6) all live in
the code and its tests now.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings

# Job modules a real worker imports at startup. `@shared_task` decorators
# only register a task on the apps that exist after their module is
# imported — a worker process imports no test or caller module, so the
# modules must be named here explicitly (deterministic and reviewable;
# no autodiscovery). Each new job module appends itself to this list in
# its own task. Alphabetical order.
JOB_MODULES = (
    "app.workers.jobs.cleanup_files",
    "app.workers.jobs.dispatch_due_notifications",
    "app.workers.jobs.expire_claims",
    "app.workers.jobs.health",
    "app.workers.jobs.project_ranking_update",
    "app.workers.jobs.rebuild_rankings",
    "app.workers.jobs.requeue_stale_validating",
    "app.workers.jobs.send_notification",
    "app.workers.jobs.validate_submission",
)


def _beat_schedule(settings: Settings) -> dict[str, Any]:
    """One beat entry per scheduled scan (MERGE_CARRIES item 4; spec
    §25.4 / §26 / §27 / §32).

    Every entry's cadence comes from settings so a deployment can tune
    or effectively disable a scan by widening the interval; batch limits
    (the scans' LIMIT constants / *_batch_limit settings) stay the burst
    bound either way. ``request_id`` is a STABLE per-schedule tag (the
    tasks receive it as an explicit argument, §15): it identifies the
    beat schedule as the correlation source; the per-run identity is
    the Celery task id the jobs already log.
    """
    return {
        "workers.cleanup_expired_files": {
            "task": "workers.cleanup_expired_files",
            "schedule": float(settings.file_cleanup_scan_interval_seconds),
            "args": ("beat.workers.cleanup_expired_files",),
        },
        "workers.dispatch_due_notifications": {
            "task": "workers.dispatch_due_notifications",
            "schedule": float(settings.notification_dispatch_scan_interval_seconds),
            "args": ("beat.workers.dispatch_due_notifications",),
        },
        "workers.expire_claims_scan": {
            "task": "workers.expire_claims_scan",
            "schedule": float(settings.claim_expiry_scan_interval_seconds),
            "args": ("beat.workers.expire_claims_scan",),
        },
        "workers.requeue_stale_validating": {
            "task": "workers.requeue_stale_validating",
            "schedule": float(settings.stale_validating_scan_interval_seconds),
            "args": ("beat.workers.requeue_stale_validating",),
        },
    }


def create_celery_app(settings: Settings) -> Celery:
    """Build a Celery app from typed settings (pure factory).

    Broker and result backend both come from `settings.redis_url`. Job
    registration runs through `include`: the `celery` CLI imports the
    listed modules at startup (app.loader.import_default_modules), and
    their `shared_task` decorators then bind to this app — the factory
    itself still imports no job module, staying cheap to call from tests.
    The beat schedule carries one entry per scheduled scan (cadence from
    settings, `_beat_schedule`). Start a worker with

        celery -A app.workers.celery_app:celery_app worker
    """
    app = Celery(
        "campusquest",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=list(JOB_MODULES),
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_eager_propagates=True,
        # Plan-04 amendment 1 (watchdog/self-heal scope): acknowledge
        # LATE and reject on worker loss, so a worker dying mid-task
        # redelivers instead of silently dropping the job. Delivery
        # becomes at-least-once, which every job must absorb
        # idempotently: expire_claim replays answer ALREADY_TERMINAL
        # and write nothing, expire_claims_scan re-discovers only what
        # is still due, and send_notification_delivery collapses
        # duplicates claim-before-send (health is pure).
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        beat_schedule=_beat_schedule(settings),
    )
    return app


@lru_cache
def get_celery_app() -> Celery:
    """Process-wide app, constructed lazily from environment settings.

    Also set as the default app so `shared_task` proxies and the Celery
    CLI resolve to this instance.
    """
    app = create_celery_app(get_settings())
    app.set_default()
    return app


def __getattr__(name: str) -> Celery:
    """Expose `celery_app` lazily (PEP 562, mirrors app.db.session)."""
    if name == "celery_app":
        return get_celery_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
