# backend/tests/workers/test_submission_validation_job.py
"""Worker-side tests for the submission validation job (spec §10 step 8,
§11.1 UPLOADED->VALIDATING->VALIDATED/VALIDATION_FAILED, §12.4, §32,
§33.3, §37.3; backend-engineering §12 worker rules, §14 file
processing; plan 04 task 7).

Two layers live here, database-free except for the one default-path
regression test noted at the bottom:

- **The sandboxed validator runner** (spec §33.3 解析器隔离): validator
  execution happens in a SUBPROCESS under hard wall-clock, memory
  (RLIMIT_AS), and CPU (RLIMIT_CPU) limits. A validator that sleeps
  past the deadline is killed -> ``VALIDATION_TIMED_OUT``; one that
  exhausts the address space or crashes -> ``VALIDATION_WORKER_CRASHED``
  — in every case the parent (the worker) survives and the run becomes
  a terminal validation outcome, never a hung worker
  (backend-engineering §14: resource-limit failures must not
  repeatedly kill the worker pool). The fixture entries under
  ``sandbox_fixtures/`` speak the same stdin/stdout protocol as the
  real child entry.
- **The job wiring** (the plan-01 health pattern): eager-mode task
  execution, request_id threading, JSON-only results, the orchestration
  shell shape, and registration through JOB_MODULES so a real worker
  CLI process sees the task.

The database-backed behavior (state machine, idempotency, report
persistence, storage retry classification end-to-end) is covered by
``tests/integration/submissions/test_validation_worker.py`` against
real PostgreSQL (§37.2/§37.3). The one exception here is the default
session source regression test: the production default cannot be
exercised through an injected fake (that is the blind spot that hid
the bug), so it opens a single connection to the same disposable test
database the integration suite uses.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from app.modules.submissions.enums import FileType
from app.modules.submissions.validators.common import ValidationCode

_FIXTURES = Path(__file__).resolve().parent / "sandbox_fixtures"
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

_CSV_SCHEMA = {
    "required_columns": [
        {"name": "url", "type": "string", "unique": True},
        {"name": "title", "type": "string"},
    ]
}

_CSV_REQUEST = {
    "schema": _CSV_SCHEMA,
    "preview": {"max_rows": 10, "max_value_length": 200},
}

_XLSX_REQUEST = {
    "schema": {"required_columns": [{"name": "url", "type": "string"}]},
    "preview": {"max_rows": 10, "max_value_length": 200},
}


def _manifest_only_xlsx() -> bytes:
    """The T5 carry shape: a real ZIP with a valid [Content_Types].xml
    but no workbook part — openpyxl answers ``OSError('File contains
    no valid workbook part')``, which the validators deliberately
    re-raise."""
    import io
    import zipfile

    manifest = (
        b'<?xml version="1.0"?><Types xmlns='
        b'"http://schemas.openxmlformats.org/package/2006/content-types"/>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", manifest)
    return buffer.getvalue()


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


# --- the sandboxed runner (spec §33.3) ----------------------------------------------


def _runner(entrypoint: list[str] | None = None, **limits: Any):
    from app.modules.submissions.validation_runner import (
        SandboxLimits,
        ValidatorSandbox,
    )

    return ValidatorSandbox(
        limits=SandboxLimits(**limits),
        entrypoint=entrypoint,
    )


def _default_limits() -> dict[str, Any]:
    return {
        "wall_timeout_seconds": 60.0,
        "memory_limit_bytes": 1024 * 1024 * 1024,
        "cpu_seconds": 60,
    }


def test_sandbox_runs_real_entry_and_returns_csv_report(tmp_path: Path) -> None:
    # The full child protocol end-to-end: real entry module, real CSV,
    # detection + validation inside the subprocess, report JSON back.
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"url,title\nhttps://a.com,t\nhttps://b.com,t2\n")
    outcome = _runner(**_default_limits()).run(
        path=upload, request=_CSV_REQUEST, declared_type=FileType.CSV
    )
    assert outcome.detected_type is FileType.CSV
    assert outcome.failure_code is None
    assert outcome.report is not None
    assert outcome.report.passed
    assert outcome.report.row_count == 2
    assert outcome.report.parser_version == "csv-1"
    assert outcome.report.preview_rows == (
        ("https://a.com", "t"),
        ("https://b.com", "t2"),
    )


def test_sandbox_reports_unrecognized_content_without_running_a_parser(
    tmp_path: Path,
) -> None:
    # Binary garbage: detection answers None and the child does NOT run
    # any format validator (the service layer turns this into the
    # FILE_TYPE_NOT_ALLOWED verdict).
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"\x7fELF\x02\x01\x01\x00" + bytes(range(64)))
    outcome = _runner(**_default_limits()).run(
        path=upload, request=_CSV_REQUEST, declared_type=FileType.CSV
    )
    assert outcome.detected_type is None
    assert outcome.report is None
    assert outcome.failure_code is None


def test_sandbox_wall_deadline_kills_sleeper_and_reports_timeout() -> None:
    # The hard wall-clock bound (T4 carry: cooperative checkpoints have
    # gaps; T5 parked ruling sizing). Deadline 1s against a 30s sleep:
    # the child is killed, the parent classifies VALIDATION_TIMED_OUT,
    # and this test process obviously survives to assert it.
    started = _elapsed_marker()
    outcome = _runner(
        entrypoint=[sys.executable, str(_FIXTURES / "sleep_entry.py")],
        wall_timeout_seconds=1.0,
        memory_limit_bytes=256 * 1024 * 1024,
        cpu_seconds=30,
    ).run(
        path=Path("/nonexistent-but-unused.bin"),
        request={},
        declared_type=FileType.CSV,
    )
    assert outcome.failure_code is ValidationCode.VALIDATION_TIMED_OUT
    assert outcome.report is not None
    assert outcome.report.errors[0].code is ValidationCode.VALIDATION_TIMED_OUT
    assert outcome.detected_type is None
    assert _elapsed_marker() - started < 10.0  # killed at ~1s, not 30s


def test_sandbox_memory_cap_kills_hog_and_reports_crash() -> None:
    # RLIMIT_AS (T5 parked ruling in miniature): a child allocating
    # 256 MB under a 128 MB address-space cap dies with MemoryError ->
    # VALIDATION_WORKER_CRASHED, parent intact.
    outcome = _runner(
        entrypoint=[sys.executable, str(_FIXTURES / "hog_entry.py")],
        wall_timeout_seconds=30.0,
        memory_limit_bytes=128 * 1024 * 1024,
        cpu_seconds=30,
    ).run(
        path=Path("/nonexistent-but-unused.bin"),
        request={},
        declared_type=FileType.CSV,
    )
    assert outcome.failure_code is ValidationCode.VALIDATION_WORKER_CRASHED
    assert outcome.report is not None
    assert outcome.report.errors[0].code is ValidationCode.VALIDATION_WORKER_CRASHED


def test_sandbox_crashing_child_reports_crash_with_stderr_detail() -> None:
    # Non-zero exit / unparseable stdout: crash taxonomy, and the
    # stderr tail is kept for SERVER-SIDE logs only — never inside the
    # persisted report (no parser internals to students).
    outcome = _runner(
        entrypoint=[sys.executable, str(_FIXTURES / "garbage_entry.py")],
        **_default_limits(),
    ).run(
        path=Path("/nonexistent-but-unused.bin"),
        request={},
        declared_type=FileType.CSV,
    )
    assert outcome.failure_code is ValidationCode.VALIDATION_WORKER_CRASHED
    assert "fixture: simulated parser crash" in (outcome.stderr_tail or "")
    serialized = json.dumps(outcome.report.errors[0].message) if outcome.report else ""
    assert "fixture" not in serialized  # report carries the stable code only


def test_sandbox_converts_child_parse_oserror_into_malformed_outcome(
    tmp_path: Path,
) -> None:
    # F1 regression: the T5 carry shape (openpyxl answers OSError on a
    # manifest-valid archive with no workbook part) must come back
    # through the FULL subprocess path as a structured MALFORMED_XLSX
    # report — exit 0, no crash classification. The crash class stays
    # reserved for actual crashes (non-zero exit / unparseable stdout /
    # timeout / OOM), and no terminal report ever promises a retry the
    # terminal replay path cannot deliver.
    upload = tmp_path / "upload.bin"
    upload.write_bytes(_manifest_only_xlsx())
    outcome = _runner(**_default_limits()).run(
        path=upload, request=_XLSX_REQUEST, declared_type=FileType.XLSX
    )
    assert outcome.failure_code is None
    assert outcome.detected_type is FileType.XLSX
    assert outcome.report is not None
    assert [error.code.value for error in outcome.report.errors] == ["MALFORMED_XLSX"]
    assert outcome.report.parser_version == "xlsx-1"
    serialized = json.dumps(outcome.report.errors[0].message)
    assert "重试" not in serialized  # no retry promise on a terminal verdict


def _elapsed_marker() -> float:
    import time

    return time.monotonic()


# --- the job wiring (the plan-01 health pattern, DB-free) -----------------------------


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch):
    """TEST-ONLY eager-mode app (the test_celery_wiring.py fixture
    pattern); the service seam is stubbed so the wiring runs without a
    database. The DB-backed eager end-to-end lives in the integration
    suite.

    Constructing a Celery app binds this thread's current-app local
    and the process default; both are restored on teardown so later
    test files resolve ``current_app`` to whatever THEY install (the
    tests/unit/test_app_wiring.py lifespan assertion depends on it).
    """
    import celery._state as celery_state

    from app.core.config import Settings
    from app.workers.celery_app import create_celery_app

    _set_required_env(monkeypatch)
    previous_current = getattr(celery_state._tls, "current_app", None)
    previous_default = celery_state.default_app
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    app.set_default()
    app.loader.import_default_modules()
    yield app
    celery_state.default_app = previous_default
    celery_state._tls.current_app = previous_current


def test_validate_submission_job_runs_and_threads_request_id(
    celery_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers.jobs import validate_submission as job_module

    calls: list[dict[str, object]] = []

    def _stub(submission_id: str, *, request_id: str, **_: object):
        calls.append({"submission_id": submission_id, "request_id": request_id})
        return {
            "submission_id": submission_id,
            "request_id": request_id,
            "validation_status": "VALIDATED",
            "passed": True,
            "row_count": 3,
            "parser_version": "csv-1",
            "detected_type": "CSV",
            "already_terminal": False,
        }

    monkeypatch.setattr(job_module, "run_submission_validation", _stub)
    result = job_module.validate_submission_job.delay("sub-1", "req-abc").get(timeout=5)
    assert result["request_id"] == "req-abc"
    assert result["validation_status"] == "VALIDATED"
    assert calls == [{"submission_id": "sub-1", "request_id": "req-abc"}]


def test_validate_submission_job_result_is_json_serializable(
    celery_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers.jobs import validate_submission as job_module

    monkeypatch.setattr(
        job_module,
        "run_submission_validation",
        lambda submission_id, *, request_id, **_: {
            "submission_id": submission_id,
            "request_id": request_id,
            "validation_status": "VALIDATION_FAILED",
            "passed": False,
            "row_count": 0,
            "parser_version": "detect-1",
            "detected_type": None,
            "already_terminal": False,
        },
    )
    payload = job_module.validate_submission_job.delay("s", "r").get(timeout=5)
    assert json.loads(json.dumps(payload))["validation_status"] == "VALIDATION_FAILED"


def test_validate_submission_job_signature_receives_ids_and_params_only() -> None:
    # §12: the dispatch surface is exactly the id plus the correlation
    # id — no dependency object ever travels through the task signature.
    import inspect

    from app.workers.jobs.validate_submission import validate_submission_job

    parameters = inspect.signature(validate_submission_job).parameters
    assert list(parameters) == ["submission_id", "request_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in parameters.values()
    )


def test_validate_submission_job_registered_through_job_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real worker imports job modules ONLY through the celery_app
    # include list; a job visible solely via in-process shared_task
    # registration would not exist for `celery -A ... worker`.
    from app.workers.celery_app import JOB_MODULES

    assert "app.workers.jobs.validate_submission" in JOB_MODULES


def test_default_session_source_is_callable_and_yields_usable_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (parallel-stream review): the job consumes the default
    # exactly like an injected factory — ``session_source =
    # _default_session_source()`` then ``async with session_source() as
    # session`` inside ``asyncio.run``. The default used to return the
    # context-manager INSTANCE, so that production-only call path
    # crashed with a TypeError while every test injected a fake factory
    # and never noticed. Only exercising the REAL default (against the
    # disposable integration test database) can pin the contract; the
    # fake's shape is ``_session_ctx`` in test_validation_worker.py.
    from sqlalchemy import text

    from app.core.config import get_settings
    from app.workers.jobs import validate_submission as job_module
    from tests.integration import db_guard

    # Capture BEFORE _set_required_env clobbers it: importing db_guard
    # installs the integration-stack defaults (setdefault), a real
    # DATABASE_URL always wins, and require_test_database keeps the
    # test off any non-test database (same guard as the integration
    # suite).
    database_url = db_guard.require_test_database(
        os.environ.get(
            "DATABASE_URL", db_guard.INTEGRATION_ENV_DEFAULTS["DATABASE_URL"]
        )
    )
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        source = job_module._default_session_source()
        assert callable(source)

        async def _use_default_the_way_the_job_does() -> int:
            async with source() as session:
                return (await session.execute(text("select 1"))).scalar_one()

        assert asyncio.run(_use_default_the_way_the_job_does()) == 1
    finally:
        get_settings.cache_clear()
