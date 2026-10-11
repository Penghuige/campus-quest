"""Production registration and isolated local Beat profile."""

from app.core.config import get_settings
from app.workers.celery_app import JOB_MODULES, create_celery_app
from app.workers.innovation_dev import create_innovation_dev_app


def test_production_registers_scan_without_losing_original_jobs():
    settings = get_settings()
    assert "app.workers.jobs.schedule_recruitments" in JOB_MODULES
    app = create_celery_app(settings)
    entry = app.conf.beat_schedule["workers.schedule_recruitments"]
    assert entry["task"] == "workers.schedule_recruitments"
    assert entry["schedule"] == 60
    assert "workers.expire_claims_scan" in app.conf.beat_schedule


def test_local_profile_only_consumes_recruitment_work():
    app = create_innovation_dev_app(get_settings())
    assert app.conf.task_default_queue == "innovation-dev"
    assert set(app.conf.beat_schedule) == {
        "workers.schedule_recruitments",
        "workers.dispatch_due_notifications",
    }
    dispatch = app.conf.beat_schedule["workers.dispatch_due_notifications"]
    assert dispatch["kwargs"]["event_types"] == ("IE_RECRUITMENT_CONFIRMATION_DUE",)
    assert dispatch["kwargs"]["queue"] == "innovation-dev"
    assert app.conf.task_acks_late and app.conf.task_reject_on_worker_lost
