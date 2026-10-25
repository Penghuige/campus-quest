"""Production registration of the recruitment scan job."""

from app.core.config import get_settings
from app.workers.celery_app import JOB_MODULES, create_celery_app


def test_production_registers_scan_without_losing_original_jobs():
    settings = get_settings()
    assert "app.workers.jobs.schedule_recruitments" in JOB_MODULES
    app = create_celery_app(settings)
    entry = app.conf.beat_schedule["workers.schedule_recruitments"]
    assert entry["task"] == "workers.schedule_recruitments"
    assert entry["schedule"] == 60
    assert "workers.expire_claims_scan" in app.conf.beat_schedule
