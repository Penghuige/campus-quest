# backend/tests/workers/test_celery_wiring.py
"""Eager-mode tests for the Celery wiring and the service-layer job pattern.

Eager mode is TEST-ONLY: the `celery_app` fixture sets `task_always_eager`,
which makes `.delay()` execute inline and return an `EagerResult` whose
`.get()` reads the local value — no broker or result backend is contacted.
`test_eager_execution_needs_no_live_broker` pins that property by pointing
the broker at a port nothing listens on.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from celery import Celery
from celery.result import EagerResult

from app.core.config import Settings
from app.workers.celery_app import create_celery_app
from app.workers.jobs.health import health_job, run_health_check

_BACKEND_DIR = Path(__file__).resolve().parents[2]

_REQUIRED_ENV_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "BUSINESS_TIMEZONE",
)


def _set_required_env(
    monkeypatch: pytest.MonkeyPatch, redis_url: str = "redis://redis:6379/0"
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Celery:
    """TEST-ONLY eager-mode app; production config never sets eager mode.

    `task_always_eager` is set here and nowhere else. With it the task runs
    inline and `.get()` reads the EagerResult, so the Redis URLs from
    settings are only configuration strings, never opened sockets.
    Constructing the app also makes it the current app, so the
    `shared_task` proxy in `app.workers.jobs.health` resolves to it.
    """
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


def test_worker_health_job_runs_once(celery_app):
    result = health_job.delay("req-123").get(timeout=5)
    assert result == {"ok": True, "request_id": "req-123"}


def test_request_id_is_threaded_through_not_rederived(celery_app) -> None:
    # A value no id generator would produce proves the job echoes the
    # caller-supplied correlation id instead of minting a fresh one.
    result = health_job.delay("opaque-correlation-id-42").get(timeout=5)
    assert result == {"ok": True, "request_id": "opaque-correlation-id-42"}


def test_celery_wiring_is_json_only(celery_app) -> None:
    # JSON-only wire format: tasks and results serialize as json, and any
    # other content type (e.g. pickle) is rejected on receipt.
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


def test_broker_and_result_backend_come_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch, redis_url="redis://queue.example:6379/1")
    app = create_celery_app(Settings())
    assert app.conf.broker_url == "redis://queue.example:6379/1"
    assert app.conf.result_backend == "redis://queue.example:6379/1"


def test_eager_mode_is_not_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    assert app.conf.task_always_eager is False
    # Propagates is safe app-wide: it only changes how eager (inline)
    # executions surface exceptions, and eager mode itself stays test-only.
    assert app.conf.task_eager_propagates is True


def test_health_job_result_is_json_serializable(celery_app) -> None:
    payload = health_job.delay("req-json").get(timeout=5)
    assert json.loads(json.dumps(payload)) == {"ok": True, "request_id": "req-json"}


def test_job_signature_shape_receives_ids_and_params_only() -> None:
    # §12: a job loads IDs and parameters, constructs dependencies, then
    # calls a service. The service call behind this job is a plain callable
    # over those parameters — no session, engine, or adapter is held as job
    # state, and the parameter surface is exactly the correlation id.
    assert list(inspect.signature(run_health_check).parameters) == ["request_id"]

    # The task's own dispatch surface is pinned the same way: bind=True
    # consumes `self` (used only for §15 job-id logging), so producers pass
    # exactly one argument — request_id, positionally or by keyword — and
    # no dependency object ever travels through the task signature.
    task_parameters = inspect.signature(health_job).parameters
    assert list(task_parameters) == ["request_id"]
    assert task_parameters["request_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_worker_cli_style_load_registers_health_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: a real worker process imports no test module, so tasks
    # must NOT become visible only through in-process shared_task
    # registration. Build the app CLI-style — factory only, never importing
    # the job module directly — then run the conf.include import the
    # `celery` CLI performs at startup (celery/bin/celery.py calls
    # app.loader.import_default_modules()).
    _set_required_env(monkeypatch)
    code = textwrap.dedent(
        """
        from app.core.config import Settings
        from app.workers.celery_app import create_celery_app

        app = create_celery_app(Settings())
        app.loader.import_default_modules()
        user_tasks = sorted(
            name for name in app.tasks if not name.startswith("celery.")
        )
        print("TASKS=" + ",".join(user_tasks))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # Every JOB_MODULES entry must appear: a job module that only
    # registers via in-process import would be invisible to a real
    # worker startup.
    assert (
        completed.stdout.strip().splitlines()[-1]
        == "TASKS=workers.expire_claim,workers.expire_claims_scan,"
        "workers.health_job,workers.send_notification_delivery"
    )


def test_eager_execution_needs_no_live_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Port 9 on localhost: nothing listens there. If eager execution ever
    # tried to reach the broker, this test would fail on connection setup.
    _set_required_env(monkeypatch, redis_url="redis://localhost:9/0")
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    eager_result = health_job.delay("req-isolated")
    assert isinstance(eager_result, EagerResult)
    assert eager_result.get(timeout=5) == {"ok": True, "request_id": "req-isolated"}


def test_worker_imports_stay_lazy_and_database_free() -> None:
    # §12 / design §3: workers are orchestration shells and must not reach
    # into persistence. Importing the worker modules must (a) succeed with
    # no deployment environment at all (lazy construction, like
    # app.db.session) and (b) not even transitively load sqlalchemy or
    # app.db.
    code = textwrap.dedent(
        """
        import sys

        import app.workers.celery_app  # noqa: F401  (module attribute stays lazy)
        import app.workers.jobs.health  # noqa: F401

        leaks = sorted(
            module
            for module in sys.modules
            if module == "sqlalchemy" or module.startswith("app.db")
        )
        print("IMPORT_OK_WITHOUT_ENV")
        print("LEAKS=" + ",".join(leaks))
        """
    )
    clean_env = {
        key: value for key, value in os.environ.items() if key not in _REQUIRED_ENV_VARS
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        env=clean_env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert lines[0] == "IMPORT_OK_WITHOUT_ENV"
    assert lines[1] == "LEAKS="
