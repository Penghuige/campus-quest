"""Local recruitment-only worker/Beat profile; production app stays complete."""

from functools import lru_cache
from typing import Any

from celery import Celery  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings
from app.workers.celery_app import create_celery_app


def create_innovation_dev_app(settings: Settings) -> Celery:
    app = create_celery_app(settings)
    schedules: dict[str, Any] = app.conf.beat_schedule
    scan = schedules["workers.schedule_recruitments"]
    scan["options"] = {"queue": "innovation-dev"}
    dispatch = schedules["workers.dispatch_due_notifications"]
    dispatch["options"] = {"queue": "innovation-dev"}
    dispatch["kwargs"] = {
        "event_types": ("IE_RECRUITMENT_CONFIRMATION_DUE",),
        "queue": "innovation-dev",
    }
    app.conf.update(
        task_default_queue="innovation-dev",
        beat_schedule={
            "workers.schedule_recruitments": scan,
            "workers.dispatch_due_notifications": dispatch,
        },
    )
    return app


@lru_cache
def get_innovation_dev_app() -> Celery:
    app = create_innovation_dev_app(get_settings())
    app.set_default()
    return app


def __getattr__(name: str) -> Celery:
    if name == "celery_app":
        return get_innovation_dev_app()
    raise AttributeError(name)
