"""API fixture clocks must mint usable tokens after a slow test collection."""

import importlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import AccessTokenCodec


@pytest.mark.parametrize(
    "module_name",
    [
        "tests.integration.submissions.test_submission_api",
        "tests.integration.system.test_system_settings_api",
        "tests.integration.tasks.test_task_api",
        "tests.integration.tasks.test_abandon_settings_composition",
    ],
)
def test_api_clock_survives_collection_older_than_token_ttl(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "_T0", datetime.now(UTC) - timedelta(minutes=30))
    clock = module.api_clock.__wrapped__()
    codec = AccessTokenCodec(
        secret="api-fixture-clock-regression-secret", ttl_minutes=15
    )
    token = codec.encode(
        user_id=uuid.uuid4(), session_id=uuid.uuid4(), role="STUDENT", now=clock.now()
    )
    assert codec.decode(token).iat == int(clock.now().timestamp())
    assert clock.now() == module._T0
