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
"""

from __future__ import annotations

from functools import lru_cache

from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings

# Job modules a real worker imports at startup. `@shared_task` decorators
# only register a task on the apps that exist after their module is
# imported — a worker process imports no test or caller module, so the
# modules must be named here explicitly (deterministic and reviewable;
# no autodiscovery). Each new job module appends itself to this list in
# its own task.
JOB_MODULES = (
    "app.workers.jobs.health",
    "app.workers.jobs.send_notification",
)


def create_celery_app(settings: Settings) -> Celery:
    """Build a Celery app from typed settings (pure factory).

    Broker and result backend both come from `settings.redis_url`. Job
    registration runs through `include`: the `celery` CLI imports the
    listed modules at startup (app.loader.import_default_modules), and
    their `shared_task` decorators then bind to this app — the factory
    itself still imports no job module, staying cheap to call from tests.
    Start a worker with

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
