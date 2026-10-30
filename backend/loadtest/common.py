"""Pure harness logic: identities, classification, refresh-retry, tallies.

Deliberately locust-free: ``import locust`` gevent-monkeypatches the whole
process at import time, so anything pytest imports must stay here while
the locust-dependent pieces (the user base class, the summary listener)
live in ``loadtest.runner`` and are imported only inside locust processes.

Every headline number flows through these functions, and the two behaviors
the plan's Review Focus pins are unit-tested right here:

- ``classify_status`` — a 429 from our own limiter is its own class,
  never folded into scenario failures (RF 5);
- ``request_with_refresh`` — a 401 mid-run triggers exactly one
  refresh-and-retry, rotating BOTH tokens (access tokens live 15
  minutes; escalation legs run longer — RF 4).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_PREFIX = "/api/v1"
REFRESH_PATH = f"{API_PREFIX}/auth/refresh"

_WORLD_ENV = "CQ_LOADTEST_WORLD"
_RUNS_DIR = Path(__file__).resolve().parent / "runs"


def classify_status(status_code: int) -> str:
    """Bucket one HTTP status: ok | rate_limited | err."""

    if status_code == 429:
        return "rate_limited"
    if status_code < 400:
        return "ok"
    return "err"


@dataclass(slots=True)
class Identity:
    """One world.json user; tokens rotate in place on refresh."""

    username: str
    access_token: str
    refresh_token: str
    claim_id: str | None


def load_identities(
    world_path: Path,
) -> tuple[list[Identity], dict[str, Any]]:
    world = json.loads(world_path.read_text())
    identities = [
        Identity(
            username=entry["username"],
            access_token=entry["access_token"],
            refresh_token=entry["refresh_token"],
            claim_id=entry.get("claim_id"),
        )
        for entry in world["users"]
    ]
    return identities, world


def latest_world_path() -> Path:
    """The newest seeded world (override with CQ_LOADTEST_WORLD)."""

    override = os.environ.get(_WORLD_ENV)
    if override:
        return Path(override)
    candidates = sorted(_RUNS_DIR.glob("*/world.json"))
    if not candidates:
        raise SystemExit(
            "no world.json under loadtest/runs/ — run 'make loadtest-seed' first"
        )
    return candidates[-1]


def elapsed_ms(response: Any) -> float | None:
    """Best-effort latency from a locust (or test-fake) response."""

    elapsed = getattr(response, "elapsed", None)
    if elapsed is not None:
        return elapsed.total_seconds() * 1000
    return getattr(response, "elapsed_ms", None)


@dataclass
class Tracker:
    """Run-wide tallies; the summary.json source of truth."""

    ok_count: int = 0
    err_count: int = 0
    rate_limited_429: int = 0
    _latencies: list[float] = field(default_factory=list)
    _by_name: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, name: str, outcome: str, response_time_ms: float | None) -> None:
        if outcome == "rate_limited":
            self.rate_limited_429 += 1
        elif outcome == "ok":
            self.ok_count += 1
        else:
            self.err_count += 1
        if response_time_ms is not None:
            self._latencies.append(float(response_time_ms))
        bucket = self._by_name.setdefault(name, {"ok": 0, "err": 0, "rate_limited": 0})
        bucket[outcome] = bucket.get(outcome, 0) + 1

    def summary(self) -> dict[str, Any]:
        total = self.ok_count + self.err_count + self.rate_limited_429
        return {
            "ok_count": self.ok_count,
            "err_count": self.err_count,
            "rate_limited_429": self.rate_limited_429,
            "total": total,
            "p99_ms": _percentile(self._latencies, 0.99),
            "by_name": self._by_name,
        }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


#: Module-level on purpose: one process = one run = one tally.
TRACKER = Tracker()


def request_with_refresh(
    client: Any,
    identity: Identity,
    method: str,
    path: str,
    *,
    name: str,
    base_headers: dict[str, str] | None = None,
    on_response: Callable[[Any], None] | None = None,
) -> str:
    """One authenticated request; on 401 refresh once, retry once.

    The refresh call goes through the same client so locust's stats see
    it under ``auth:refresh``. Rotation updates both tokens in place —
    ``rotate_refresh`` invalidates the old refresh token.
    """

    headers = dict(base_headers or {})
    headers.setdefault("Authorization", f"Bearer {identity.access_token}")
    response = client.request(method, path, name=name, headers=headers)
    if on_response is not None:
        on_response(response)
    if response.status_code != 401:
        return classify_status(response.status_code)

    refreshed = client.request(
        "POST",
        REFRESH_PATH,
        name="auth:refresh",
        json={"refresh_token": identity.refresh_token},
    )
    if on_response is not None:
        on_response(refreshed)
    if refreshed.status_code != 200:
        return "err"
    try:
        payload = refreshed.json()
        identity.access_token = payload["access_token"]
        identity.refresh_token = payload["refresh_token"]
    except (KeyError, ValueError, TypeError):
        return "err"

    retry_headers = dict(base_headers or {})
    retry_headers["Authorization"] = f"Bearer {identity.access_token}"
    retried = client.request(method, path, name=name, headers=retry_headers)
    if on_response is not None:
        on_response(retried)
    return classify_status(retried.status_code)
