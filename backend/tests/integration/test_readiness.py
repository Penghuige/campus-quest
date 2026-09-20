# backend/tests/integration/test_readiness.py
"""Readiness endpoint behavior (spec §34; docs/quality/backend-engineering.md §15).

`/health/ready` reports per-component availability with a machine-readable,
stable body shape; `/health/live` never consults dependencies. The 200 case
runs against the live compose stack through the real registry. The
unavailable cases inject fake components through the FastAPI dependency, so
they prove translation logic without breaking real services.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.readiness import ReadinessRegistry, get_readiness_registry
from app.main import create_app


class _StaticCheck:
    """Test double reporting a fixed availability without touching services."""

    def __init__(self, name: str, available: bool) -> None:
        self.name = name
        self._available = available

    async def check(self) -> bool:
        return self._available


class _ExplodingCheck:
    """Test double whose probe fails like a real refused connection would."""

    name = "redis"

    async def check(self) -> bool:
        raise RuntimeError("connection refused")


def _client_with_registry(registry: ReadinessRegistry) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_readiness_registry] = lambda: registry
    return TestClient(app)


@pytest.mark.integration
def test_ready_returns_200_when_live_stack_is_available() -> None:
    client = TestClient(create_app())
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "components": {"postgres": "ok", "redis": "ok"},
    }


@pytest.mark.integration
def test_ready_returns_503_with_component_list_when_a_dependency_is_down() -> None:
    client = _client_with_registry(
        ReadinessRegistry(
            [
                _StaticCheck("postgres", available=True),
                _StaticCheck("redis", available=False),
            ]
        )
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "components": {"postgres": "ok", "redis": "down"},
    }


@pytest.mark.integration
def test_ready_reports_down_when_a_probe_raises() -> None:
    client = _client_with_registry(
        ReadinessRegistry([_StaticCheck("postgres", available=True), _ExplodingCheck()])
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["components"]["redis"] == "down"


@pytest.mark.integration
def test_live_stays_200_even_when_every_component_is_down() -> None:
    client = _client_with_registry(
        ReadinessRegistry(
            [
                _StaticCheck("postgres", available=False),
                _StaticCheck("redis", available=False),
            ]
        )
    )
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    # The same app reports ready=503, proving the fakes are actually wired
    # and liveness is not passing merely because nothing was consulted.
    assert client.get("/health/ready").status_code == 503
