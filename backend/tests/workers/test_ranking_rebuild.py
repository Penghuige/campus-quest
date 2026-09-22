# backend/tests/workers/test_ranking_rebuild.py
"""Worker-side tests for the ranking jobs (spec §17.3, §32; backend-
engineering §12 worker rules; plan 05 task 6).

Both jobs are orchestration shells: IDs and parameters in -> construct
dependencies -> call the projection -> JSON summary out with
``request_id`` threaded through. These tests are DATABASE-FREE — the
projection is a recording fake and the session/redis sources are null
context managers — pinning the wiring, the registration through
JOB_MODULES (so a real worker CLI sees the tasks), the IDs-strings-only
task signatures, the transient retry policy, and the post-commit
dispatcher's serialization.

The PostgreSQL+Redis behavior behind the shells (aggregation,
convergence, rebuild) lives in
``tests/integration/rankings/test_rankings.py``.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

_REQUIRED_ENV_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "BUSINESS_TIMEZONE",
)


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Values only need to satisfy Settings validation — every external
    # touchpoint is an injected fake in these tests.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def _direct_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build Settings DIRECTLY (never through the ``get_settings``
    lru_cache): these tests' dummy environment must not poison the
    process-wide snapshot a later integration test in the same process
    relies on."""
    from app.core.config import Settings

    return Settings()


class _NullSource:
    """A session/redis source that yields an opaque object — the shells
    only pass the resource through to the (faked) projection."""

    def __call__(self) -> _NullSource:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


class _RecordingProjection:
    """Stands in for ``RankingRedisProjection``; records the calls."""

    def __init__(self) -> None:
        self.rebuild_calls = 0
        self.apply_calls: list[tuple[UUID, datetime]] = []

    async def rebuild_all(self, session: Any, redis: Any) -> dict[str, Any]:
        self.rebuild_calls += 1
        return {
            "keys_rebuilt": ["ranking:all"],
            "members_written": 1,
            "stale_keys_deleted": [],
        }

    async def apply_ranking_update(
        self, session: Any, redis: Any, user_id: UUID, ranking_effective_at: datetime
    ) -> dict[str, Any]:
        self.apply_calls.append((user_id, ranking_effective_at))
        return {
            "user_id": str(user_id),
            "updated_keys": ["ranking:all"],
            "scores": {"ranking:all": 10},
        }


# --- the rebuild job -----------------------------------------------------------------


def test_rebuild_job_registered_through_job_modules() -> None:
    # A job visible solely via in-process shared_task registration would
    # be invisible to a real worker CLI; JOB_MODULES is the contract.
    from app.workers.celery_app import JOB_MODULES

    assert "app.workers.jobs.rebuild_rankings" in JOB_MODULES
    assert "app.workers.jobs.project_ranking_update" in JOB_MODULES


def test_rebuild_job_signature_receives_ids_and_params_only() -> None:
    # §12: the dispatch surface is exactly the correlation id — no
    # dependency object ever travels through the task signature (bind's
    # self is not part of the dispatch surface, same as the plan-04 job).
    from app.workers.jobs.rebuild_rankings import rebuild_all_rankings_job

    parameters = inspect.signature(rebuild_all_rankings_job).parameters
    assert list(parameters) == ["request_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in parameters.values()
    )


def test_run_rebuild_threads_request_id_and_returns_json_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    from app.workers.jobs.rebuild_rankings import run_rebuild_all_rankings

    projection = _RecordingProjection()

    result = run_rebuild_all_rankings(
        request_id="req-rebuild-1",
        settings=_direct_settings(monkeypatch),
        session_source=_NullSource(),
        redis_source=_NullSource(),
        projection=projection,
    )

    assert projection.rebuild_calls == 1
    assert result == {
        "request_id": "req-rebuild-1",
        "keys_rebuilt": ["ranking:all"],
        "members_written": 1,
        "stale_keys_deleted": [],
    }
    json.dumps(result)  # job results stay JSON-serializable (§15)


# --- the incremental projection job ---------------------------------------------------


def test_projection_job_signature_receives_ids_and_params_only() -> None:
    from app.workers.jobs.project_ranking_update import project_ranking_update_job

    parameters = inspect.signature(project_ranking_update_job).parameters
    assert list(parameters) == ["user_id", "ranking_effective_at", "request_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in parameters.values()
    )


def test_run_projection_parses_ids_and_calls_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    from app.workers.jobs.project_ranking_update import run_ranking_projection

    projection = _RecordingProjection()
    user_id = uuid4()
    effective_at = datetime(2026, 9, 20, 16, 30, tzinfo=UTC)

    result = run_ranking_projection(
        str(user_id),
        effective_at.isoformat(),
        request_id="req-proj-7",
        settings=_direct_settings(monkeypatch),
        session_source=_NullSource(),
        redis_source=_NullSource(),
        projection=projection,
    )

    assert projection.apply_calls == [(user_id, effective_at)]
    assert result["request_id"] == "req-proj-7"
    assert result["user_id"] == str(user_id)
    assert result["scores"] == {"ranking:all": 10}
    json.dumps(result)


def test_projection_job_autoretries_only_transients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    import redis.exceptions

    from app.workers.jobs import project_ranking_update as job_module

    # Redis/OS transients retry with backoff; convergence makes the
    # retry safe (§32). Content-level outcomes are results, not raises.
    assert (OSError, redis.exceptions.RedisError) == job_module._RETRYABLE_TRANSIENTS
    assert job_module._MAX_RETRIES == 5
    assert OSError in job_module.project_ranking_update_job.autoretry_for


def test_run_projection_rejects_naive_effective_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    from app.workers.jobs.project_ranking_update import run_ranking_projection

    with pytest.raises(ValueError, match="timezone-aware"):
        run_ranking_projection(
            str(uuid4()),
            "2026-09-20T16:30:00",  # naive — persisted instants are UTC-aware
            request_id="req-proj-8",
            settings=_direct_settings(monkeypatch),
            session_source=_NullSource(),
            redis_source=_NullSource(),
            projection=_RecordingProjection(),
        )


# --- the post-commit dispatcher -------------------------------------------------------


def test_dispatcher_enqueues_serialized_ids_and_generated_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_env(monkeypatch)
    from app.workers.jobs import project_ranking_update as job_module

    calls: list[tuple[str, str, str]] = []

    class _FakeTask:
        @staticmethod
        def delay(user_id: str, ranking_effective_at: str, request_id: str) -> None:
            calls.append((user_id, ranking_effective_at, request_id))

    monkeypatch.setattr(job_module, "project_ranking_update_job", _FakeTask())

    user_id = uuid4()
    effective_at = datetime(2026, 8, 15, 2, 0, tzinfo=UTC)
    job_module.CeleryRankingDispatcher().enqueue_ranking_update(user_id, effective_at)
    job_module.CeleryRankingDispatcher().enqueue_ranking_update(
        user_id, effective_at, request_id="req-known"
    )

    assert calls[0] == (str(user_id), effective_at.isoformat(), calls[0][2])
    assert len(calls[0][2]) == 32  # generated hex correlation id
    assert calls[1] == (str(user_id), effective_at.isoformat(), "req-known")


def test_jobs_import_lazily_without_database() -> None:
    # The wiring-discipline twin of the validation job: importing the
    # job modules must not touch settings, a database, or Redis.
    import subprocess
    import sys

    code = (
        "import app.workers.jobs.rebuild_rankings, "
        "app.workers.jobs.project_ranking_update; print('ok')"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": ".",
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=".",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
