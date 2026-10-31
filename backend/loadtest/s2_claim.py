"""S2 claim rush (spec §Scenarios S2): N racers, S slots, one gate.

The open-gate lives here, not in the seed: every racer greenlet waits on
a common t0 (set by the first user to start, +10 s) and then fires
``POST /tasks/{task_id}/claim`` exactly once — one retry only on 5xx.
Losers must land in the ``rejected`` bucket (clean 4xx business
rejections, never 5xx); check_s2 asserts no oversell and fairness.
"""

from __future__ import annotations

import os
import time
from typing import Any

from locust import task
from locust.exception import StopUser

from loadtest.common import (
    API_PREFIX,
    TRACKER,
    Identity,
    classify_business_status,
    classify_status,
    elapsed_ms,
    request_with_refresh,
)
from loadtest.runner import CampusQuestUser

CLAIM_RETRY_5XX_ENV = "CQ_S2_RETRY_5XX"
_GATE_LEAD_SECONDS = 10.0

_T0: float | None = None


def _gate_t0() -> float:
    """Common open instant; set once by the first user (gevent makes the
    check-then-set between awaits atomic)."""

    global _T0
    if _T0 is None:
        _T0 = time.time() + _GATE_LEAD_SECONDS
    return _T0


class ClaimRushUser(CampusQuestUser):
    @classmethod
    def identity_pool(cls) -> list[Identity]:
        # only the racers (claim_id null); spawned count == real concurrency
        return [i for i in CampusQuestUser.identities if i.claim_id is None]

    def on_start(self) -> None:
        super().on_start()
        wait = _gate_t0() - time.time()
        if wait > 0:
            time.sleep(wait)  # gevent-patched: everyone releases together

    @task
    def claim_once(self) -> None:
        path = f"{API_PREFIX}/tasks/{self.world['task_id']}/claim"
        captured: list[Any] = []

        def _tally(response: Any) -> None:
            captured.append(response)
            TRACKER.record(
                "s2:claim",
                classify_business_status(response.status_code),
                elapsed_ms(response),
            )
            # pin the stable BusinessError codes (plan RF3): a loser must
            # be a clean business rejection with a known code, never a
            # bare 4xx — the checker asserts on exactly these
            code = _error_code(response)
            if code is not None:
                TRACKER.record_code("s2:claim", code)

        def _tally_refresh(response: Any) -> None:
            TRACKER.record(
                "auth:refresh",
                classify_status(response.status_code),
                elapsed_ms(response),
            )

        request_with_refresh(
            self.client,
            self.identity,
            "POST",
            path,
            name="s2:claim",
            on_response=_tally,
            on_refresh=_tally_refresh,
        )
        response = captured[-1] if captured else None
        if (
            response is not None
            and response.status_code >= 500
            and os.environ.get(CLAIM_RETRY_5XX_ENV, "1") == "1"
        ):
            request_with_refresh(
                self.client,
                self.identity,
                "POST",
                path,
                name="s2:claim-retry",
                on_response=_tally,
                on_refresh=_tally_refresh,
            )
        # each racer fires once, then leaves the swarm
        raise StopUser()


def _error_code(response: Any) -> str | None:
    """The BusinessError envelope's stable code, when present."""

    try:
        payload = response.json()
        code = payload.get("error", {}).get("code")
        return code if isinstance(code, str) else None
    except (ValueError, TypeError, AttributeError):
        return None
