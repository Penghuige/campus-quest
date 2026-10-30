"""Unit tests for the loadtest harness (plan Task 3; Review Focus 4/5).

Pins the two behaviors every headline number depends on: 429s from our own
limiter count as their own class (never as scenario failure), and a 401
mid-run triggers exactly one refresh-and-retry with token rotation —
access tokens live 15 minutes, escalation legs run longer.
"""

from __future__ import annotations

from typing import Any

from loadtest.common import (
    Identity,
    Tracker,
    classify_business_status,
    classify_status,
    request_with_refresh,
)


def test_classify_business_status_separates_rejections() -> None:
    # business 4xx (deadline passed, in-flight conflict, slots gone) is a
    # LEGITIMATE scenario outcome, not an error — the S1/S2 story depends
    # on telling them apart from real failures
    assert classify_business_status(200) == "ok"
    assert classify_business_status(201) == "ok"
    assert classify_business_status(409) == "rejected"
    assert classify_business_status(422) == "rejected"
    # infrastructure 4xx is NEVER a legitimate business outcome: a 404 on
    # complete means the object never landed (disk!), 401/403 are auth
    # trouble — collapsing them into "rejected" hides the breaking point
    assert classify_business_status(401) == "err"
    assert classify_business_status(403) == "err"
    assert classify_business_status(404) == "err"
    assert classify_business_status(429) == "rate_limited"
    assert classify_business_status(500) == "err"
    # the strict read-path view keeps 404 an error
    assert classify_status(404) == "err"


def test_status_zero_is_a_transport_failure_never_ok() -> None:
    # locust's safe mode returns status_code=0 for RequestException
    # (connection reset/refused/timeout) — counting that as ok made the
    # headline error rate a lie exactly at the breaking point (review C1)
    assert classify_status(0) == "err"
    assert classify_business_status(0) == "err"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    """Scripts responses per call; records every request for assertions."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "path": path, **kwargs})
        if not self._responses:
            raise AssertionError("FakeClient ran out of scripted responses")
        return self._responses.pop(0)


def test_classify_status_buckets() -> None:
    assert classify_status(200) == "ok"
    assert classify_status(204) == "ok"
    assert classify_status(304) == "ok"
    assert classify_status(404) == "err"
    assert classify_status(422) == "err"
    assert classify_status(500) == "err"
    assert classify_status(503) == "err"
    # the limiter's 429 is its own class — never folded into err (RF 5)
    assert classify_status(429) == "rate_limited"


def test_tracker_counts_and_p99() -> None:
    tracker = Tracker()
    for ms in (100, 200, 300, 400, 500):
        tracker.record("s3:tasks", "ok", ms)
    tracker.record("s3:tasks", "rate_limited", 10)
    tracker.record("s3:tasks", "err", 20)

    summary = tracker.summary()
    assert summary["ok_count"] == 5
    assert summary["rate_limited_429"] == 1
    assert summary["err_count"] == 1
    assert summary["total"] == 7
    # p99 computed over all recorded latencies
    assert summary["p99_ms"] == 500

    tracker.record("s1:intent", "rejected", 25)
    summary = tracker.summary()
    assert summary["rejected_count"] == 1
    assert summary["total"] == 8


def test_tracker_api_p99_excludes_upload_leg() -> None:
    # spec pass line is API-leg P99; the MinIO PUT leg rides separately
    tracker = Tracker()
    for ms in (100, 200, 300):
        tracker.record("s1:intent", "ok", ms)
    for ms in (900, 1000):
        tracker.record("s1:put", "ok", ms)

    summary = tracker.summary()
    assert summary["p99_ms"] == 1000  # overall includes the PUT leg
    assert summary["p99_api_ms"] == 300  # API view does not


def test_401_triggers_one_refresh_with_rotation_and_retry() -> None:
    identity = Identity(
        username="31000001",
        access_token="old-access",
        refresh_token="old-refresh",
        claim_id=None,
    )
    client = FakeClient(
        [
            FakeResponse(401),  # initial GET — token expired
            FakeResponse(  # refresh rotates both tokens
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "role": "STUDENT",
                },
            ),
            FakeResponse(200),  # retried GET succeeds
        ]
    )

    outcome = request_with_refresh(
        client, identity, "GET", "/api/v1/tasks", name="s3:tasks"
    )

    assert outcome == "ok"
    assert identity.access_token == "new-access"
    assert identity.refresh_token == "new-refresh"
    refreshes = [c for c in client.calls if c["path"] == "/api/v1/auth/refresh"]
    assert len(refreshes) == 1
    # the retried call carried the rotated token
    assert "Bearer new-access" in client.calls[-1]["headers"]["Authorization"]


def test_401_with_failing_refresh_reports_err() -> None:
    identity = Identity(
        username="u",
        access_token="dead",
        refresh_token="dead",
        claim_id=None,
    )
    client = FakeClient([FakeResponse(401), FakeResponse(401)])

    outcome = request_with_refresh(client, identity, "GET", "/api/v1/me", name="s3:me")

    assert outcome == "err"
    # exactly one refresh attempt, no infinite loop
    refreshes = [c for c in client.calls if c["path"] == "/api/v1/auth/refresh"]
    assert len(refreshes) == 1


def test_429_and_5xx_never_trigger_refresh() -> None:
    identity = Identity(
        username="u", access_token="t", refresh_token="r", claim_id=None
    )
    client = FakeClient([FakeResponse(429)])
    outcome = request_with_refresh(
        client, identity, "GET", "/api/v1/tasks", name="s3:tasks"
    )
    assert outcome == "rate_limited"

    client = FakeClient([FakeResponse(500)])
    outcome = request_with_refresh(
        client, identity, "GET", "/api/v1/tasks", name="s3:tasks"
    )
    assert outcome == "err"
    assert not any("refresh" in c["path"] for c in client.calls)


def test_refresh_flow_tallies_final_only_and_own_refresh_stat() -> None:
    """One logical request = one scenario tally (the FINAL response);
    the refresh call tallies under its own name — never the scenario's
    (review I1: double-counting poisoned ok/rejected and check_s1)."""

    identity = Identity(
        username="u", access_token="old", refresh_token="r", claim_id=None
    )
    client = FakeClient(
        [
            FakeResponse(401),
            FakeResponse(200, {"access_token": "new", "refresh_token": "r2"}),
            FakeResponse(200),
        ]
    )
    finals: list[int] = []
    refreshes: list[int] = []

    outcome = request_with_refresh(
        client,
        identity,
        "POST",
        "/api/v1/submissions/upload-complete",
        name="s1:complete",
        on_response=lambda r: finals.append(r.status_code),
        on_refresh=lambda r: refreshes.append(r.status_code),
    )

    assert outcome == "ok"
    assert finals == [200]  # exactly one scenario tally — the retry
    assert refreshes == [200]  # the refresh has its own stat hook


def test_request_kwargs_forwarded_to_client() -> None:
    identity = Identity(
        username="u", access_token="t", refresh_token="r", claim_id=None
    )
    client = FakeClient([FakeResponse(201)])

    outcome = request_with_refresh(
        client,
        identity,
        "POST",
        "/api/v1/submissions/upload-intent",
        name="s1:intent",
        json={"claim_id": "c1", "size": 123},
    )

    assert outcome == "ok"
    sent = client.calls[0]
    assert sent["json"] == {"claim_id": "c1", "size": 123}
